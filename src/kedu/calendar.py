"""本地交易日历, 复刻聚宽 get_trade_days / get_all_trade_days.

数据源为 ClickHouse `jqdata.trade_days`, 由 scripts/backfill_jq.sync_trade_days 同步.
返回 numpy.ndarray, 元素为 datetime.date, dtype=object, 与 jqdatasdk 逐项一致.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .db import DATABASE, get_client

# 交易日历是中国市场口径,"今天"必须按交易所时区(北京 UTC+8)取,而非机器本地时区。
# 机器常为 UTC,UTC 傍晚 16:00-24:00 已是北京次日,若用 dt.date.today() 会比聚宽 live 早一天。
_CN_TZ = ZoneInfo("Asia/Shanghai")


def _today_cn() -> dt.date:
    """返回北京时区的今天, 对齐交易所与聚宽 live 的日期口径."""
    return dt.datetime.now(_CN_TZ).date()


def _to_date(x: str | dt.date | None) -> dt.date | None:
    """将日期类输入转换为 datetime.date, None 原样返回."""
    if x is None:
        return None
    if isinstance(x, dt.datetime):
        return x.date()
    if isinstance(x, dt.date):
        return x
    return pd.Timestamp(x).date()


def _all_days() -> list[dt.date]:
    """返回本地交易日历中的全部交易日."""
    cli = get_client()
    return [r[0] for r in cli.query(
        f"SELECT day FROM {DATABASE}.trade_days ORDER BY day").result_rows]


def get_all_trade_days() -> np.ndarray:
    """获取全部交易日."""
    return np.array(_all_days(), dtype=object)


def get_trade_days(start_date: str | dt.date | None = None, end_date: str | dt.date | None = None,
                   count: int | None = None) -> np.ndarray:
    """获取指定范围交易日, 复刻聚宽语义.

    - 含 start_date 与 end_date, end_date 默认 datetime.date.today().
    - count 必须大于 0. 给 count 时 start_date 与 end_date 只能二选一.
    - 给 start_date 与 count 时, 从 start 起往后取 count 个交易日.
    - 仅给 end_date 与 count 时, 从 end 起往前取 count 个交易日.
    - 不给 count 时必须给 start_date, 返回 [start, end] 区间.
    - 既无 start_date 又无 count 时抛错, 与聚宽一致.
    """
    if count is not None and count <= 0:
        raise ValueError("count 参数需要大于 0 或者为 None")
    start = _to_date(start_date)
    end = _to_date(end_date)
    if count is not None and start is not None and end is not None:
        raise ValueError("当指定了 count 时,start_date 与 end_date 只能二选一")
    if count is None and start is None:
        raise ValueError("start_date 参数与 count 参数必须输入一个")

    days = _all_days()
    if count is not None:
        if start is not None and end is None:
            res = [d for d in days if d >= start][:count]          # 往后 count 个
        else:
            e = end or _today_cn()
            res = [d for d in days if d <= e][-count:]              # 往前 count 个
    else:
        e = end or _today_cn()
        res = [d for d in days if d >= start and d <= e]           # start 必非 None
    return np.array(res, dtype=object)
