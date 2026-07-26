"""Corporate-action reference data from local ClickHouse-backed finance tables."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd

from ._jqsdk import query
from .finance import finance

__all__ = ["get_split_dividend", "get_capital_reform_dates"]


def get_split_dividend(
    security: str,
    start_date: str | dt.date,
    end_date: str | dt.date,
) -> list[dict[str, Any]]:
    """Return stock/fund cash-dividend and split records.

    Stock records follow jqboson's ``dividend_store`` mapping:
    ``bonus_pre_tax = bonus_ratio_rmb / 10`` and
    ``scale_factor = (dividend_ratio + transfer_ratio) / 10 + 1``.

    Fund records follow jqboson's fund branch: use ``ex_date`` unless it is the
    sentinel date ``>= 2200-01-01``, then use ``otc_ex_date``; no tax is
    applied, so ``bonus_post_tax == bonus_pre_tax``.
    """
    code = str(security)
    start = _to_date(start_date)
    end = _to_date(end_date)
    if _is_fund_like(code):
        records = _fund_split_dividend(code, start, end)
    else:
        records = _stock_split_dividend(code, start, end)
    return sorted(records, key=lambda item: item["date"])


def get_capital_reform_dates(
    code: str,
    start_date: str | dt.date,
    end_date: str | dt.date,
) -> list[dt.date]:
    """Return 股改 dates from ``STK_CAPITAL_CHANGE`` reason ``306020``."""
    start = _to_date(start_date)
    end = _to_date(end_date)
    table = finance.STK_CAPITAL_CHANGE
    frame = finance.run_query(
        query(table)
        .filter(table.code == str(code))
        .filter(table.change_date >= start)
        .filter(table.change_date <= end)
        .filter(table.change_reason_id == 306020)
    )
    dates: list[dt.date] = []
    for value in frame.get("change_date", []):
        d = _to_date(value)
        if d is not None:
            dates.append(d)
    return sorted(dates)


def _stock_split_dividend(
    code: str,
    start: dt.date,
    end: dt.date,
) -> list[dict[str, Any]]:
    table = finance.STK_XR_XD
    frame = finance.run_query(
        query(table)
        .filter(table.code == code)
        .filter(table.a_xr_date >= start)
        .filter(table.a_xr_date <= end)
    )
    records: list[dict[str, Any]] = []
    for row in frame.to_dict("records"):
        d = _to_date(row.get("a_xr_date"))
        if d is None:
            continue
        dividend_ratio = _float_or_zero(row.get("dividend_ratio"))
        transfer_ratio = _float_or_zero(row.get("transfer_ratio"))
        bonus_ratio_rmb = _float_or_zero(row.get("bonus_ratio_rmb"))
        records.append(
            {
                "date": d,
                "bonus_pre_tax": _stock_bonus_pre_tax(row, bonus_ratio_rmb),
                "scale_factor": (dividend_ratio + transfer_ratio) / 10.0 + 1.0,
            }
        )
    return records


#: Tolerance for treating ``distributed_share_base_implement`` as "unchanged"
#: from the board/shareholders-approved base (see :func:`_stock_bonus_pre_tax`)
#: -- purely a floating-point-compare guard, not a materiality threshold: the
#: two verified cases either match to the reported precision exactly (603798.
#: XSHG, no revision) or differ by double-digit percent (000898.XSHE, a real
#: revision), so any epsilon well under 1% separates them identically.
_BASE_REVISION_EPSILON = 1.0

def _stock_bonus_pre_tax(row: dict[str, Any], bonus_ratio_rmb: float) -> float:
    """每股税前现金分红, 复刻聚宽回测口径.

    ``bonus_amount_rmb``(派现总额)在公司公告时恒等于 ``(bonus_ratio_rmb / 10) ×
    该次分配【当时用的】股本基数`` —— 但这个基数在实施阶段可能被【下修】(比如新纳入
    一个回购专户排除范围), 也可能【从未变过】。两种情形聚宽回测入账的口径不同, 不
    存在能同时套用的单一基数字段:

    * 若 ``distributed_share_base_implement``(实施阶段核定基数)相对
      ``distributed_share_base_shareholders``/``board``(股东大会/董事会阶段基数)
      发生了下修(数值不同), 说明实施时有新的股本变动被计入本次分配的除外范围,
      此时聚宽入账用的是【下修后的实施基数】。
    * 若三个阶段基数完全一致(未下修), 聚宽入账实际用的是
      ``total_capital_before_transfer``(分配前总股本快照), 不是公告基数——两者
      的微小差异(通常 <2%)反映的是分配基准日之后、与本次分配无关的股本变动,
      聚宽仍按全部总股本摊薄计入。

    实证(两个真实持仓, 均逐笔核对到聚宽导出的真实数据, 结论互相矛盾, 说明没有
    "无脑用某个基数"的简单规则; 上面的下修判定是唯一能同时复现两者的规则):

    * 000898.XSHE 2021-06-23 除权, 10派0.84(公告值=>0.084)。
      ``distributed_share_base_board`` == ``_shareholders`` == 939960.0178, 但
      ``distributed_share_base_implement`` = 798806.0178 —— **下修了 15%**。
      聚宽口径用下修后的实施基数: ``67099.7055 / 798806.0178 = 0.084``。对照
      ``tests/strategies/board_continuation/`` 的真实持仓: 2021-06-22 买入、次日
      除权, 导出的 ``开仓均价`` 由 4.76 精确降至 4.676(=4.76-0.084), 现金变动精确
      等于 ``8400 × 0.084 × 0.8 = 564.48`` —— 与 0.084 逐位吻合, 与
      ``total_capital_before_transfer=940525.0201`` 算出的 0.0713 不吻合。
    * 603798.XSHG 2021-06-10 除权, 10派1.2(公告值=>0.12)。
      ``distributed_share_base_board`` == ``_shareholders`` == ``_implement`` =
      19726.8961 —— **三阶段完全一致, 未下修**。聚宽口径改用
      ``total_capital_before_transfer=20000``: ``2367.2275 / 20000 = 0.1184``。
      对照 ``tests/strategies/small_cap/`` 的真实持仓: 2021-06-09 买入 1100 股
      (10.49), 2021-06-10 09:30 卖出(除权后, 持仓已跨过 08:00 分红入账), 成交价
      10.50、导出的平仓盈亏精确等于 141.24 = ``(10.50 - (10.49 - 0.1184)) ×
      1100``——与 0.1184 逐位吻合; 若用未下修的实施基数算出的 0.12, 平仓盈亏应为
      ``(10.50 - (10.49 - 0.12)) × 1100 = 143`` (与真实的 141.24 不符)。

    数据缺失(无派现总额)时回落到公告口径 ``bonus_ratio_rmb / 10``。再按 20% 计税
    (见 jqboson ``account/base.py`` 的 ``bonus_post_tax``)。
    """
    bonus_amount = _float_or_zero(row.get("bonus_amount_rmb"))
    if bonus_amount <= 0.0:
        return bonus_ratio_rmb / 10.0

    implement_base = _float_or_zero(row.get("distributed_share_base_implement"))
    approved_base = _float_or_zero(
        row.get("distributed_share_base_shareholders")
    ) or _float_or_zero(row.get("distributed_share_base_board"))
    if (
        implement_base > 0.0
        and approved_base > 0.0
        and abs(implement_base - approved_base) > _BASE_REVISION_EPSILON
    ):
        return round(bonus_amount / implement_base, 4)

    for base_field in (
        "total_capital_before_transfer",
        "distributed_share_base_implement",
        "distributed_share_base_shareholders",
        "distributed_share_base_board",
    ):
        base = _float_or_zero(row.get(base_field))
        if base > 0.0:
            return round(bonus_amount / base, 4)
    return bonus_ratio_rmb / 10.0


def _fund_split_dividend(
    code: str,
    start: dt.date,
    end: dt.date,
) -> list[dict[str, Any]]:
    table = finance.FUND_DIVIDEND
    frame = finance.run_query(
        query(table)
        .filter(table.code == code)
    )
    records: list[dict[str, Any]] = []
    for row in frame.to_dict("records"):
        d = _fund_dividend_date(row)
        if d is None or d < start or d > end:
            continue
        proportion = _float_or_zero(row.get("proportion"))
        scale = _float_or_zero(row.get("split_ratio")) or 1.0
        records.append(
            {
                "date": d,
                "bonus_pre_tax": proportion,
                "bonus_post_tax": proportion,
                "scale_factor": scale,
            }
        )
    return records


def _is_fund_like(code: str) -> bool:
    return (
        code.endswith(".OF")
        or (code.endswith(".XSHG") and code.startswith(("5", "1")))
        or (code.endswith(".XSHE") and code.startswith(("15", "16", "18")))
    )


def _fund_dividend_date(row: dict[str, Any]) -> dt.date | None:
    ex_date = _to_date(row.get("ex_date"))
    if ex_date is not None and ex_date < dt.date(2200, 1, 1):
        return ex_date
    return _to_date(row.get("otc_ex_date"))


def _to_date(value: Any) -> dt.date | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return pd.Timestamp(value).date()


def _float_or_zero(value: Any) -> float:
    if value is None or pd.isna(value):
        return 0.0
    return float(value)
