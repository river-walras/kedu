"""本地复刻聚宽 get_locked_shares —— 指定区间内的限售解禁数据。

经 live 探测确认:get_locked_shares 是聚宽独立维护的解禁数据集,**不可**由本文件夹其余
12 张表(STK_LIMITED_SHARES_LIST / UNLIMIT 等)重算得到 —— 其 num 对部分个股(如送转后的
002345)与实际解禁表不一致、含未来「预计」解禁行、且 rate2 以解禁前流通股本为基。故按本项目
派生接口惯例(同 get_extras('is_st')):由 scripts/backfill_locked_shares.py 自 live
get_locked_shares 全量灌入 ClickHouse `jqdata.locked_shares`,本地仅按 code + 交易日窗口过滤,
逐行与 live 完全一致。

数据源为 ClickHouse `jqdata.locked_shares`(列:code, day, num, rate1, rate2)。
窗口:forward_count 与 start_date 同用时,区间为 [start_date, start_date + forward_count 日历日]
(live 探测确认 forward_count 实为日历日,非文档所称交易日);否则用 [start_date, end_date]。
forward_count 与 end_date 二选一。
fail-fast:未 auth 时 get_client() 直接 raise,不静默回退。
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import pandas as pd

from .db import DATABASE, get_client, query_df

_COLUMNS = ["day", "code", "num", "rate1", "rate2"]


def _to_date(x: str | dt.date | None) -> dt.date | None:
    """将日期类输入转为 datetime.date, None 原样返回。"""
    if x is None:
        return None
    if isinstance(x, dt.datetime):
        return x.date()
    if isinstance(x, dt.date):
        return x
    return pd.Timestamp(x).date()


def _empty() -> pd.DataFrame:
    """空结果:列与 dtype 对齐 live(day/code object, num/rate float64)。"""
    return pd.DataFrame({
        "day": pd.Series([], dtype=object),
        "code": pd.Series([], dtype=object),
        "num": pd.Series([], dtype="float64"),
        "rate1": pd.Series([], dtype="float64"),
        "rate2": pd.Series([], dtype="float64"),
    })


def get_locked_shares(stock_list: str | Sequence[str],
                      start_date: str | dt.date | None = None,
                      end_date: str | dt.date | None = None,
                      forward_count: int | None = None) -> pd.DataFrame:
    """获取指定日期区间内的限售解禁数据, 复刻聚宽 get_locked_shares。

    - stock_list: 单代码 str 或代码 list。
    - start_date: 区间起始(必填)。
    - end_date / forward_count: 二选一。forward_count 表示自 start_date 起向后
      forward_count 个交易日的区间(与 start_date 同用)。
    返回 DataFrame, 列为 day(解禁日, 'YYYY-MM-DD' 字符串)、code、num(解禁股数)、
    rate1(解禁股数/总股本)、rate2(解禁股数/流通股本), 按 (day, code) 升序。
    """
    codes = [stock_list] if isinstance(stock_list, str) else list(stock_list)
    assert codes, "stock_list is required"
    start = _to_date(start_date)
    if start is None:
        raise ValueError("start_date 参数必须输入")
    if forward_count is not None:
        # live 探测:forward_count 实为**日历日**(非文档所称交易日),区间右端含
        # start + forward_count 日(fc=1116 时端点恰为该日且当日解禁计入)。
        end = start + dt.timedelta(days=forward_count)
    elif end_date is not None:
        end = _to_date(end_date)
    else:
        raise ValueError("end_date 参数与 forward_count 参数必须输入一个")
    if end < start:
        return _empty()

    cli = get_client()
    inlist = ", ".join("'" + str(c).replace("'", "") + "'" for c in codes)
    # FINAL 去 ReplacingMergeTree 未合并的重复(回补幂等重灌后取最新一版)
    raw = query_df(cli, f"""
        SELECT code, day, num, rate1, rate2
        FROM {DATABASE}.locked_shares FINAL
        WHERE code IN ({inlist})
          AND day >= toDate('{start.isoformat()}') AND day <= toDate('{end.isoformat()}')
    """)
    if raw.empty:
        return _empty()

    out = pd.DataFrame({
        "day": pd.to_datetime(raw["day"]).dt.strftime("%Y-%m-%d"),
        "code": raw["code"].astype(object),
        "num": pd.to_numeric(raw["num"], errors="coerce").astype("float64"),
        "rate1": pd.to_numeric(raw["rate1"], errors="coerce").astype("float64"),
        "rate2": pd.to_numeric(raw["rate2"], errors="coerce").astype("float64"),
    })
    return out.sort_values(["day", "code"]).reset_index(drop=True)[_COLUMNS]
