"""场内基金日线行情严格值校验:本地 get_price vs live jqdatasdk。

重点验基金复权价/均价 round 到 **3 位**(对齐聚宽,实测 P0d;股票为 2 位)。
fq=None 原始价逐字段精确;fq='post' 后复权价逐字段精确(3 位舍入须与聚宽一致)。
live 结果走快照缓存。代码需先由 backfill_fund 灌入 bar_1d。
"""
from __future__ import annotations

import pytest

import jqdatasdk
from kedu import get_price as local_price

from tests._compare import col_equal

FIELDS = ["open", "close", "high", "low", "volume", "money", "avg",
          "high_limit", "low_limit", "pre_close", "factor", "paused"]

# 场内基金代表:宽基 ETF(含明显后复权因子,考验 3 位舍入)。
FUND_CODES = ["510300.XSHG", "510050.XSHG", "159915.XSHE"]
START, END = "2021-01-04", "2021-03-31"


def _live_daily(snap, code, start, end, fq):
    fq_tag = "none" if fq is None else fq
    return snap.get(
        f"price-fund-{code}-{start}-{end}-{fq_tag}",
        (code, start, end, fq_tag, tuple(FIELDS)),
        lambda: jqdatasdk.get_price(
            code, start_date=start, end_date=end, frequency="daily",
            fields=FIELDS, fq=fq, skip_paused=False),
    )


def _check_strict(loc, live, label) -> list[str]:
    idx = loc.index.intersection(live.index)
    problems = []
    if len(idx) != len(live.index) or len(idx) != len(loc.index):
        problems.append(f"{label}: 行对齐 local={len(loc)} live={len(live)} common={len(idx)}")
    for c in FIELDS:
        if c not in loc or c not in live:
            problems.append(f"{label}.{c}: 缺列")
            continue
        ok, mx, nbad = col_equal(loc.loc[idx, c], live.loc[idx, c], tol=1e-4)
        if not ok:
            problems.append(f"{label}.{c}: {nbad} 处不一致 (max_abs={mx:.3g})")
    return problems


@pytest.mark.parametrize("code", FUND_CODES, ids=FUND_CODES)
@pytest.mark.parametrize("fq_tag", ["none", "post"])
def test_price_fund_daily(code, fq_tag, snap, clickhouse_auth):
    fq = None if fq_tag == "none" else fq_tag
    loc = local_price(code, start_date=START, end_date=END, frequency="daily",
                      fields=FIELDS, fq=fq)
    if loc.empty:
        pytest.skip(f"{code}: 本地 bar_1d 无基金数据(先跑 backfill_fund)")
    live = _live_daily(snap, code, START, END, fq)
    if live is None or live.empty:
        pytest.skip(f"{code}: live 无数据")
    problems = _check_strict(loc, live, f"{code} {fq_tag}")
    assert not problems, " | ".join(problems[:20])


def test_price_fund_round_3_decimals(clickhouse_auth):
    """纯本地:场内基金 fq='post' 复权价最多 3 位小数(股票为 2 位)。"""
    loc = local_price("510300.XSHG", start_date=START, end_date=END, frequency="daily",
                      fields=["close", "avg"], fq="post")
    if loc.empty:
        pytest.skip("本地 bar_1d 无 510300.XSHG(先跑 backfill_fund)")
    for col in ("close", "avg"):
        maxdec = max(
            (len(repr(float(x)).split(".")[1].rstrip("0")) for x in loc[col].dropna()
             if "." in repr(float(x))),
            default=0)
        assert maxdec <= 3, f"{col} 出现 >3 位小数(={maxdec}),基金复权价应 round 到 3 位"
