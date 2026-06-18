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
                "bonus_pre_tax": bonus_ratio_rmb / 10.0,
                "scale_factor": (dividend_ratio + transfer_ratio) / 10.0 + 1.0,
            }
        )
    return records


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
