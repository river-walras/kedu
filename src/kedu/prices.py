"""get_price 从 bar_1d / bar_1m 读取行情, 支持 fq=None/'pre'/'post' 复权.

存储为原始 OHLCV 与累计因子 factor. factor 直接取自聚宽
get_price(fq='post').factor, 见 scripts/rebuild_from_jq.py, 故基准与聚宽后复权一致.
- 不复权, fq=None: 原样返回, factor 列为 1.
- 后复权, fq='post': adj=raw*factor, 量=raw/factor, factor 列为 factor.
  基准同聚宽, 绝对值与聚宽一致, 价格列 round(2) 可能引入约 0.01 舍入差.
- 前复权, fq='pre': adj=raw*factor/factor_last, 量=raw*factor_last/factor,
  factor 列为 factor/factor_last. 最新交易日为原始价, 与聚宽一致.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import pandas as pd

from .db import DATABASE, get_client, query_df

_PRICE_FIELDS = ["open", "close", "high", "low", "pre_close", "high_limit", "low_limit", "avg"]
_DEFAULT_FIELDS = ["open", "close", "high", "low", "volume", "money"]
_ALL_FIELDS = ["open", "close", "high", "low", "volume", "money", "avg",
               "factor", "high_limit", "low_limit", "pre_close", "paused"]

_DAILY = {"daily", "1d"}
_MINUTE = {"minute", "1m"}


def _is_date_only(v) -> bool:
    """判断输入是否为不含时间部分的日期值."""
    s = str(v)
    return len(s) <= 10 and ":" not in s


_SEL_COLS = ["instrument_id", "open", "close", "high", "low", "pre_close",
             "high_limit", "low_limit", "volume", "money", "avg", "factor", "paused"]


def get_price(security: str | Sequence[str], start_date: str | dt.date | None = None,
              end_date: str | dt.date | None = None, frequency: str = "daily",
              fields: Sequence[str] | None = None, fq: str | None = "pre",
              count: int | None = None) -> pd.DataFrame:
    """本地复刻 jqdatasdk.get_price.

    security 可为单代码 str 或代码 list.
    单代码返回窄表, index 为时间, 列为 fields, 与聚宽单标的一致.
    代码 list 返回对齐聚宽 panel=False 的长表, 列为 time, code 与 fields.
    """
    if frequency in _DAILY:
        table, tcol, minute = "bar_1d", "date", False
    elif frequency in _MINUTE:
        table, tcol, minute = "bar_1m", "datetime", True
    else:
        raise NotImplementedError("仅支持 daily / 1m")
    single = isinstance(security, str)
    codes = [security] if single else list(security)
    fields = list(fields) if fields else list(_DEFAULT_FIELDS)
    cli = get_client()

    def _empty():
        """返回与当前 security 形态匹配的空结果."""
        return pd.DataFrame(columns=fields) if single else pd.DataFrame(columns=["time", "code", *fields])

    if not codes:
        return _empty()

    inlist = ", ".join("'" + str(c).replace("'", "") + "'" for c in codes)
    pre = fq == "pre"
    p = "b." if pre else ""           # 前复权走 JOIN,列名带别名前缀
    cast = "toDateTime" if minute else "toDate"
    where = [f"{p}instrument_id IN ({inlist})"]
    if start_date is not None:
        where.append(f"{p}{tcol} >= {cast}('{start_date}')")
    if end_date is not None:
        if minute and _is_date_only(end_date):           # 纯日期当作整天(含 end 当日全部分钟)
            where.append(f"{p}{tcol} < toDateTime('{end_date}') + INTERVAL 1 DAY")
        else:
            where.append(f"{p}{tcol} <= {cast}('{end_date}')")

    if pre:
        # 动态前复权:锚=每票全表最新 factor。用 JOIN 一次取回,避免第二次往返。
        sel = ", ".join(f"b.{c}" for c in [tcol, *_SEL_COLS]) + ", lf.last_factor"
        frm = (f"{DATABASE}.{table} b JOIN (SELECT instrument_id, argMax(factor, {tcol}) AS last_factor "
               f"FROM {DATABASE}.{table} WHERE instrument_id IN ({inlist}) GROUP BY instrument_id) lf "
               f"ON b.instrument_id = lf.instrument_id")
        inst, time_c = "b.instrument_id", f"b.{tcol}"
    else:
        sel = ", ".join([tcol, *_SEL_COLS])
        frm = f"{DATABASE}.{table}"
        inst, time_c = "instrument_id", tcol

    if count:                                            # 每票各自取最近 count 根
        order_lim = f"ORDER BY {inst}, {time_c} DESC LIMIT {int(count)} BY {inst}"
    else:
        order_lim = f"ORDER BY {inst}, {time_c}"
    df = query_df(cli, f"SELECT {sel} FROM {frm} WHERE {' AND '.join(where)} {order_lim}")
    if df.empty:
        return _empty()
    df = df.sort_values(["instrument_id", tcol]).reset_index(drop=True)

    f = df["factor"].astype("float64")
    if fq is None:
        adj = pd.Series(1.0, index=df.index)
    elif fq == "post":
        adj = f
    elif fq == "pre":
        adj = f / df["last_factor"].astype("float64")
    else:
        raise ValueError(f"bad fq: {fq}")

    # 复权时聚宽把价格/量 round 到 2 位/整股;不复权(fq=None)则原样返回存储精度
    # (OHLC 原生 2 位,avg 为成交均价原生 3 位 —— 不能强行 round 到 2 位)。
    adjust = fq is not None
    cols: dict[str, object] = {}
    for fld in fields:
        if fld in _PRICE_FIELDS:
            v = df[fld].astype("float64") * adj if adjust else df[fld].astype("float64")
            cols[fld] = v.round(2).to_numpy() if adjust else v.to_numpy()
        elif fld == "volume":
            v = (df["volume"].astype("float64") / adj).round() if adjust else df["volume"].astype("float64")
            cols[fld] = v.to_numpy()
        elif fld == "money":
            cols[fld] = df["money"].astype("float64").to_numpy()
        elif fld == "factor":
            cols[fld] = adj.to_numpy()
        elif fld == "paused":
            cols[fld] = df["paused"].astype("float64").to_numpy()
        else:
            cols[fld] = df[fld].to_numpy()

    if single:
        return pd.DataFrame(cols, index=pd.Index(df[tcol].to_numpy(), name=tcol))
    return pd.DataFrame({"time": df[tcol].to_numpy(),
                         "code": df["instrument_id"].astype(object).to_numpy(), **cols})
