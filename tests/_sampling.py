"""确定性抽样 —— 抽查策略的核心,可复现、不依赖随机。

- PINNED:固定纳入的边界/代表股(金融/白酒/医药/石油/家电/新能源/光伏)。
- sample_codes(n):pinned + 对其余股票按等步长均匀切片,覆盖跨交易所/板块/退市/B 股。
- price_codes(n):日线抽样股,pinned 打底再确定性扩展。
- ex_div_windows(code):从本地 bar_1d 的 factor 变化推导除权事件日,取窗口集合 ——
  「用本地推导关键窗口,再定向去 live 校验」,最大化每条配额覆盖的复权场景。
"""
from __future__ import annotations

import datetime as dt

from kedu.db import DATABASE, get_client

# 固定纳入的边界/代表股(银行/券商/保险/白酒/医药/石油/家电/机场/新能源/光伏/食品)
PINNED = [
    "000001.XSHE", "600036.XSHG", "601398.XSHG", "600030.XSHG", "601688.XSHG",
    "601318.XSHG", "601601.XSHG", "600519.XSHG", "000858.XSHE", "600276.XSHG",
    "300760.XSHE", "601857.XSHG", "600028.XSHG", "000651.XSHE", "000333.XSHE",
    "600009.XSHG", "002594.XSHE", "300750.XSHE", "601012.XSHG", "600887.XSHG",
]


def _all_stock_codes(client=None) -> list[str]:
    cli = client or get_client()
    return sorted(
        r[0]
        for r in cli.query(
            f"SELECT DISTINCT instrument_id FROM {DATABASE}.securities WHERE type='stock'"
        ).result_rows
    )


def _pinned_plus_uniform(n: int, client=None) -> list[str]:
    """pinned + 对其余股票等步长均匀切片,确定性,去重后排序。"""
    allc = _all_stock_codes(client)
    pinned = [c for c in PINNED if c in allc]
    rest = [c for c in allc if c not in pinned]
    k = max(0, n - len(pinned))
    step = max(1, len(rest) // k) if k else 1
    sampled = rest[::step][:k]
    return sorted(set(pinned + sampled))


def sample_codes(n: int = 100, client=None) -> list[str]:
    """基本面 / finance / history 用:100 票确定性抽样。"""
    return _pinned_plus_uniform(n, client)


def price_codes(n: int = 50, client=None) -> list[str]:
    """日线用:50 票确定性抽样(pinned 打底 + 均匀扩展)。"""
    return _pinned_plus_uniform(n, client)


def ex_div_windows(
    code: str, pad: int = 10, recent_days: int = 250, cap_days: int = 500, client=None
) -> list[tuple[str, str]]:
    """从本地 bar_1d 推导该 code 的校验窗口(不耗 JQ 配额)。

    取 factor 相对前一交易日发生变化的日期(除权事件),各取 ±pad 交易日窗口,
    并入最近 recent_days 个交易日窗口;合并重叠区间后按 (start, end) 日期串返回。
    总覆盖天数按 cap_days 截断(优先保留最近 + 较多事件的窗口)。
    """
    cli = client or get_client()
    rows = cli.query(
        f"SELECT date, factor FROM {DATABASE}.bar_1d "
        f"WHERE instrument_id = '{code}' ORDER BY date"
    ).result_rows
    if not rows:
        return []
    days: list[dt.date] = [r[0] for r in rows]
    factors = [float(r[1]) if r[1] is not None else 1.0 for r in rows]
    n = len(days)

    # 除权事件下标:factor 相对前一日变化
    event_idx = [i for i in range(1, n) if abs(factors[i] - factors[i - 1]) > 1e-12]

    intervals: list[tuple[int, int]] = []
    for i in event_idx:
        intervals.append((max(0, i - pad), min(n - 1, i + pad)))
    # 最近窗口
    intervals.append((max(0, n - recent_days), n - 1))

    # 合并重叠/相邻区间(按下标)
    intervals.sort()
    merged: list[list[int]] = []
    for lo, hi in intervals:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])

    # cap_days:从最近往早保留,累计覆盖天数不超过 cap_days
    out: list[tuple[str, str]] = []
    total = 0
    for lo, hi in reversed(merged):
        span = hi - lo + 1
        if total + span > cap_days and out:
            break
        total += span
        out.append((days[lo].isoformat(), days[hi].isoformat()))
    out.reverse()
    return out
