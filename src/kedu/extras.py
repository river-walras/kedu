"""本地复刻聚宽 get_extras, 目前支持 info='is_st'.

数据源为 ClickHouse `jqdata.is_st`(每交易日每标的的 ST 状态布尔), 由
scripts/backfill_jq.sync_is_st 自聚宽 get_extras('is_st') 同步。is_st 涵盖股改 s、
ST、*ST、退市整理期(任一为 True), 是 get_price 字段白名单之外的独立接口数据。

返回与 jqdatasdk 一致:df=True 时 DataFrame, index 为交易日 datetime、columns 为代码、
值为 bool(未上市/退市后或缺数据的格子为 NaN);df=False 时 dict{code: numpy.ndarray}。
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import pandas as pd

from .calendar import get_trade_days
from .db import DATABASE, get_client


def get_extras(info: str, security_list: str | Sequence[str],
               start_date: str | dt.date | None = None,
               end_date: str | dt.date | None = None,
               df: bool = True, count: int | None = None) -> pd.DataFrame | dict:
    """获取标的的额外数据, 目前仅支持 info='is_st'.

    - security_list 单代码 str 或代码 list, 列序保持输入顺序.
    - 交易日窗口语义同 get_trade_days: count 与 start_date 二选一, count 表示自
      end_date 往前 count 个交易日, end_date 缺省为今日.
    - df=True 返回 DataFrame(index=交易日, columns=代码, 值=bool/NaN);
      df=False 返回 dict{code: numpy.ndarray}.
    """
    if info != "is_st":
        raise NotImplementedError(
            f"get_extras 暂仅支持 info='is_st', 不支持 {info!r}"
            "(acc_net_value/unit_net_value/futures_* 等本地无基金/期货数据源)")
    codes = [security_list] if isinstance(security_list, str) else list(security_list)
    assert codes, "security_list is required"

    days = [d for d in get_trade_days(start_date=start_date, end_date=end_date, count=count)]
    idx = pd.DatetimeIndex(pd.to_datetime(days))
    if not days:
        wide = pd.DataFrame(index=idx, columns=codes, dtype=object)
        return wide if df else {c: wide[c].to_numpy() for c in codes}

    cli = get_client()
    d0, dn = days[0].isoformat(), days[-1].isoformat()
    inlist = ", ".join("'" + str(c).replace("'", "") + "'" for c in codes)
    rows = cli.query(
        f"SELECT instrument_id, date, is_st FROM {DATABASE}.is_st "
        f"WHERE instrument_id IN ({inlist}) "
        f"AND date >= toDate('{d0}') AND date <= toDate('{dn}')"
    ).result_rows
    # 每票在窗口起点之前的最近一条 is_st(用于给退市票/区间整体晚于停更的票播种前向填充)
    seed_rows = cli.query(
        f"SELECT instrument_id, argMax(is_st, date) FROM {DATABASE}.is_st "
        f"WHERE instrument_id IN ({inlist}) AND date < toDate('{d0}') "
        f"GROUP BY instrument_id"
    ).result_rows
    seed = pd.Series({r[0]: float(r[1]) for r in seed_rows}, dtype="float64").reindex(codes)

    raw = pd.DataFrame(rows, columns=["instrument_id", "date", "is_st"])
    if raw.empty:
        pivot = pd.DataFrame(index=idx, columns=codes, dtype="float64")
    else:
        raw["date"] = pd.to_datetime(raw["date"])
        pivot = (raw.pivot_table(index="date", columns="instrument_id", values="is_st")
                 .reindex(index=idx, columns=codes).astype("float64"))

    # 复刻聚宽 get_extras 在请求窗口内的口径(返回值恒为 bool、无 NaN):
    #   - 上市前 -> False;上市中 -> 实际值;退市后 -> 最后状态前向填充(非恒 True)。
    # 实现:用窗口起点前的最近值给首行播种 -> 前向填充(带过退市)-> 剩余(上市前)填 False。
    pivot.iloc[0] = pivot.iloc[0].fillna(seed).to_numpy()
    wide = pivot.ffill().fillna(0.0).astype(bool)
    wide.index.name = None
    wide.columns.name = None

    if df:
        return wide
    return {c: wide[c].to_numpy() for c in codes}
