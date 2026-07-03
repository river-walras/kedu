"""本地复刻聚宽 get_call_auction(集合竞价 tick)。

数据源为 ClickHouse `jqdata.call_auction`，由 scripts/backfill_call_auction.py 逐票
从 live get_call_auction 拉取(每票每交易日一行 09:25 快照)。

与聚宽对齐的输出口径(经 live 探针确认):
- 支持单代码 str 与代码 list;code 恒在最前一列。
- fields=None 返回全部字段，顺序为 time、current、volume、money、五档交错
  a1_p、a1_v、…、a5_p、a5_v、b1_p、b1_v、…、b5_p、b5_v(不含 code)。
- 显式 fields 时按传入顺序返回，且 time 仅在被请求时出现(聚宽如此)。
- time 为 datetime64[ns];数值列 float64;整数索引。行序 ORDER BY (code, time)。
- 指数无盘口，五档在 live 返回 None(本地为 NaN，值层面等价);本地不施加 10000/5000 行截断。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import pandas as pd

from .db import DATABASE, get_client, query_df, quote_ident

# fields=None 默认返回的字段集与顺序(不含 code;code 恒前置)。
_LADDER = [
    f"{side}{lvl}_{pv}"
    for side in ("a", "b")
    for lvl in range(1, 6)
    for pv in ("p", "v")
]
CALL_AUCTION_FIELDS = ["time", "current", "volume", "money", *_LADDER]
_ALLOWED = {*CALL_AUCTION_FIELDS, "code"}
# 数值列(除 time、code 外全部)。
_NUM_FIELDS = [f for f in CALL_AUCTION_FIELDS if f != "time"]


def _to_date(x: str | dt.date | None) -> dt.date | None:
    """将日期类输入转换为 datetime.date，None 原样返回。"""
    if x is None:
        return None
    if isinstance(x, dt.datetime):
        return x.date()
    if isinstance(x, dt.date):
        return x
    return pd.Timestamp(x).date()


def _q(value: object) -> str:
    """转义为 SQL 单引号字符串字面量。"""
    return "'" + str(value).replace("'", "\\'") + "'"


def _to_datetime_ns(series: pd.Series) -> pd.Series:
    """ClickHouse DateTime via Arrow 可能到达为 Unix 秒;归一到 pandas ns。"""
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_datetime(series, unit="s").astype("datetime64[ns]")
    return pd.to_datetime(series).astype("datetime64[ns]")


def _normalize_fields(fields: str | Sequence[str] | None) -> list[str]:
    """规范化 fields，剔除 code(恒前置)，None 返回全字段序。

    非白名单字段抛错;保留传入顺序、去重。
    """
    if fields is None:
        return list(CALL_AUCTION_FIELDS)
    raw = [fields] if isinstance(fields, str) else list(fields)
    bad = [f for f in raw if f not in _ALLOWED]
    if bad:
        raise Exception(f"get_call_auction fields 包含不支持的字段: {bad}")
    out: list[str] = []
    for f in raw:
        if f == "code" or f in out:
            continue  # code 恒前置;去重保序
        out.append(f)
    return out


def _empty(fields: Sequence[str]) -> pd.DataFrame:
    """空结果 DataFrame，列序 [code, *fields]。time 为 datetime64[ns]，其余 float64。"""
    data: dict[str, pd.Series] = {"code": pd.Series([], dtype=object)}
    for f in fields:
        if f == "time":
            data[f] = pd.Series([], dtype="datetime64[ns]")
        else:
            data[f] = pd.Series([], dtype="float64")
    return pd.DataFrame(data, columns=["code", *fields])


def get_call_auction(security, start_date, end_date, fields=None) -> pd.DataFrame:
    """获取标的在指定日期区间的集合竞价 tick 数据，复刻 jqdatasdk.get_call_auction。

    security 可为单代码 str 或代码 list；返回列序为 code、fields(fields=None 返回全字段)。
    显式 fields 未含 'time' 时不返回 time 列(对齐聚宽)。本地不截断 10000/5000 行。
    """
    flds = _normalize_fields(fields)
    codes = [security] if isinstance(security, str) else list(security)
    if not codes:
        return _empty(flds)

    # 查询列:必取 time、code 以定位/排序，最终按 flds 投影。
    read_cols = ["time", *(f for f in flds if f != "time")]
    inlist = ", ".join(_q(c) for c in codes)
    sel = ", ".join(["code", *(quote_ident(c) for c in read_cols)])
    where = [f"code IN ({inlist})"]
    start = _to_date(start_date)
    end = _to_date(end_date)
    if start is not None:
        where.append(f"time >= toDateTime('{start.isoformat()}')")
    if end is not None:
        # end 当日整天纳入(集合竞价在 09:25，落在 [end, end+1d) 内)。
        where.append(f"time < toDateTime('{(end + dt.timedelta(days=1)).isoformat()}')")
    sql = (
        f"SELECT {sel} FROM {DATABASE}.call_auction FINAL "
        f"WHERE {' AND '.join(where)} ORDER BY code, time"
    )
    raw = query_df(get_client(), sql)
    if raw.empty:
        return _empty(flds)

    out = pd.DataFrame({"code": raw["code"].astype(object)})
    for f in flds:
        if f == "time":
            out[f] = _to_datetime_ns(raw["time"])
        else:
            out[f] = pd.to_numeric(raw[f], errors="coerce").astype("float64")
    return out[["code", *flds]].reset_index(drop=True)
