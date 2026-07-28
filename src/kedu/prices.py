"""get_price 从 bar_1d / bar_1m 读取行情, 支持 fq=None/'pre'/'post' 复权.

存储为原始 OHLCV 与累计因子 factor. factor 直接取自聚宽
get_price(fq='post').factor, 见 scripts/rebuild_from_jq.py, 故基准与聚宽后复权一致.
- 不复权, fq=None: 原样返回, factor 列为 1.
- 后复权, fq='post': adj=raw*factor, 量=raw/factor, factor 列为 factor.
  基准同聚宽, 绝对值与聚宽一致, 价格列 round 可能引入舍入差.
- 前复权, fq='pre': adj=raw*factor/factor_last, 量=raw*factor_last/factor,
  factor 列为 factor/factor_last. 最新交易日为原始价, 与聚宽一致.
- 复权价/均价 round 位数:股票 2 位、基金 3 位(对齐聚宽, 实测 P0d);量取整。
  基金身份由 securities.type ∈ 场内基金类型即时判定(无长缓存, 避免 securities 刷新后 stale)。
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import numpy as np
import pandas as pd

from .db import DATABASE, get_client, query_df
from .finance_schema import FUND_ONEXCHANGE_TYPES

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


def _fund_codes(cli, codes: Sequence[str]) -> set[str]:
    """即时查 securities,返回 codes 中属于场内基金(type ∈ FUND_ONEXCHANGE_TYPES)的代码集。

    无长生命周期缓存,避免 securities 刷新后判定 stale。仅在复权 round 时调用一次。
    """
    if not codes:
        return set()
    inlist = ", ".join("'" + str(c).replace("'", "") + "'" for c in codes)
    tin = ", ".join("'" + t + "'" for t in FUND_ONEXCHANGE_TYPES)
    rows = cli.query(
        f"SELECT instrument_id FROM {DATABASE}.securities "
        f"WHERE instrument_id IN ({inlist}) AND type IN ({tin})").result_rows
    return {r[0] for r in rows}


def get_price(security: str | Sequence[str], start_date: str | dt.date | None = None,
              end_date: str | dt.date | None = None, frequency: str = "daily",
              fields: Sequence[str] | None = None, skip_paused: bool = False,
              fq: str | None = "pre", count: int | None = None,
              panel: bool = True, fill_paused: bool = True,
              round: bool = True) -> pd.DataFrame:
    """本地复刻 jqdatasdk.get_price, 签名与聚宽原生一致.

    security 可为单代码 str 或代码 list.
    单代码返回窄表, index 为时间, 列为 fields, 与聚宽单标的一致.
    代码 list 返回对齐聚宽 panel=False 的长表, 列为 time, code 与 fields.

    与聚宽对齐的参数语义:
    - fq: 'pre'/'post'/'none'/None, 'none' 等价 None(不复权).
    - skip_paused=True: 剔除停牌行(paused==1).
    - fill_paused=False: 停牌行价格置 NaN、量/额置 0(默认 True 用停牌前价填充, 即存储口径).
    - round=False: 复权价/量不做 round(默认 True 对齐聚宽).
    - panel: 仅签名兼容; pandas>=0.25 恒返回 DataFrame.
    - 未给 start_date 且未给 count 时, 默认窗口 2015-01-01..2015-12-31(对齐聚宽).
    """
    if fq == "none":                      # 聚宽用字符串 'none' 表示不复权
        fq = None
    if count is None:                     # 默认窗口对齐聚宽(count 路径不套用 start 默认)
        if start_date is None:
            start_date = "2015-01-01"
        if end_date is None:
            end_date = "2015-12-31"
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

    if skip_paused:                        # 剔除停牌行(对齐聚宽 skip_paused)
        df = df[df["paused"].astype("float64") != 1.0].reset_index(drop=True)
        if df.empty:
            return _empty()

    f = df["factor"].astype("float64")
    if fq is None:
        adj = pd.Series(1.0, index=df.index)
    elif fq == "post":
        adj = f
    elif fq == "pre":
        adj = f / df["last_factor"].astype("float64")
    else:
        raise ValueError(f"bad fq: {fq}")

    # 复权时聚宽把价格/量 round 到固定位数/整股;不复权(fq=None)则原样返回存储精度
    # (OHLC 原生 2 位,avg 为成交均价原生 3 位 —— 不能强行 round 到 2 位)。
    # round 位数:股票 2 位、基金 3 位(逐行按 securities.type 判定,即时查无 stale)。
    adjust = fq is not None
    fund_mask = (df["instrument_id"].isin(_fund_codes(cli, codes)).to_numpy()
                 if (adjust and round) else None)
    cols: dict[str, object] = {}
    for fld in fields:
        if fld in _PRICE_FIELDS:
            v = df[fld].astype("float64") * adj if adjust else df[fld].astype("float64")
            if adjust and round:
                vv = v.to_numpy()
                cols[fld] = np.where(fund_mask, np.round(vv, 3), np.round(vv, 2))
            else:
                cols[fld] = v.to_numpy()
        elif fld == "volume":
            v = df["volume"].astype("float64") / adj if adjust else df["volume"].astype("float64")
            cols[fld] = (v.round() if (adjust and round) else v).to_numpy()
        elif fld == "money":
            cols[fld] = df["money"].astype("float64").to_numpy()
        elif fld == "factor":
            cols[fld] = adj.to_numpy()
        elif fld == "paused":
            cols[fld] = df["paused"].astype("float64").to_numpy()
        else:
            cols[fld] = df[fld].to_numpy()

    if not fill_paused:                    # 停牌行价格置 NaN、量额置 0(对齐聚宽 fill_paused=False)
        pmask = (df["paused"].astype("float64") == 1.0).to_numpy()
        if pmask.any():
            for fld in fields:
                if fld in _PRICE_FIELDS:
                    cols[fld] = np.where(pmask, np.nan, cols[fld])
                elif fld in ("volume", "money"):
                    cols[fld] = np.where(pmask, 0.0, cols[fld])

    if single:
        return pd.DataFrame(cols, index=pd.Index(df[tcol].to_numpy(), name=tcol))
    return pd.DataFrame({"time": df[tcol].to_numpy(),
                         "code": df["instrument_id"].astype(object).to_numpy(), **cols})


def get_fq_anchor(security: str | Sequence[str], frequency: str = "daily") -> pd.DataFrame:
    """返回每票的前复权锚点: 锚定时间与该时点的累计 factor.

    前复权是动态值 —— get_price(fq='pre') 把每票的价格乘以 factor/factor_last, 其中
    factor_last 取自「该票在该表里最后一根 bar」的 factor(见上面 get_price 的 argMax
    子查询, 那个子查询**不带日期过滤**)。所以锚随每次数据更新往后漂。

    锚不能由查询窗口的最后一行推断: 窗口截在锚之前时, 窗口内每一行的 factor 都小于 1,
    看不出锚落在哪天。要在图上或报告里标注复权口径, 就必须回到这一层单独取。

    锚按表分别计算 —— bar_1d 与 bar_1m 的最后一根 bar 可能不是同一天, 故 frequency
    必须与对应的 get_price 调用一致, 否则标注的锚和实际用的锚不是一个。

    返回长表: code / anchor_time / anchor_factor, 按 code 排序; 无数据的代码不出现。
    """
    codes = [security] if isinstance(security, str) else list(security)
    empty = pd.DataFrame(columns=["code", "anchor_time", "anchor_factor"])
    if not codes:
        return empty
    if frequency in _DAILY:
        table, tcol = "bar_1d", "date"
    elif frequency in _MINUTE:
        table, tcol = "bar_1m", "datetime"
    else:
        raise NotImplementedError("仅支持 daily / 1m")

    inlist = ", ".join("'" + str(c).replace("'", "") + "'" for c in codes)
    df = query_df(get_client(),
                  f"SELECT instrument_id AS code, max({tcol}) AS anchor_time, "
                  f"argMax(factor, {tcol}) AS anchor_factor "
                  f"FROM {DATABASE}.{table} WHERE instrument_id IN ({inlist}) "
                  f"GROUP BY instrument_id ORDER BY instrument_id")
    return df if not df.empty else empty
