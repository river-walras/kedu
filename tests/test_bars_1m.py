"""分钟线校验:本地 get_price(frequency='1m') vs live jqdatasdk。

bar_1m 尚未入库,默认整组 skip;需先回补对应窗口后用 --run-bars-1m 显式启用:
    uv run --env-file .env python scripts/rebuild_from_jq.py --bars-1m \
        --bars-1m-year 2026 --limit-codes 5
    uv run --env-file .env pytest tests/test_bars_1m.py --run-bars-1m \
        --bars-1m-codes 000001.XSHE,600519.XSHG --bars-1m-start 2026-05-26 --bars-1m-end 2026-05-29

默认对比 fq=None 与 fq='post'(与「今日锚点」无关,可在任意窗口校验);fq='pre' 需
覆盖到最新交易日才能与 live 一致。live 结果走快照缓存。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import jqdatasdk
from kedu import get_price as local_price

FIELDS = ["open", "close", "high", "low", "volume", "money", "avg",
          "high_limit", "low_limit", "pre_close", "factor", "paused"]
TOL = 1e-4


@pytest.fixture(scope="module", autouse=True)
def _gate(request):
    if not request.config.getoption("--run-bars-1m"):
        pytest.skip("bar_1m 未入库;加 --run-bars-1m 并先回补窗口后启用")


def _cases(request):
    codes = [c.strip() for c in request.config.getoption("--bars-1m-codes").split(",") if c.strip()]
    fqs = [None if x.strip().lower() == "none" else x.strip()
           for x in request.config.getoption("--bars-1m-fq").split(",") if x.strip()]
    start = request.config.getoption("--bars-1m-start")
    end = request.config.getoption("--bars-1m-end")
    return [(code, start, end, fq) for code in codes for fq in fqs]


def test_bars_1m(request, snap, clickhouse_auth):
    problems: list[str] = []
    for code, start, end, fq in _cases(request):
        label = f"{code} {start}..{end} fq={fq}"
        loc = local_price(code, start_date=start, end_date=end,
                          frequency="1m", fields=FIELDS, fq=fq)
        fq_tag = "none" if fq is None else fq
        live = snap.get(
            f"bars1m-{code}-{start}-{end}-{fq_tag}",
            (code, start, end, fq_tag, tuple(FIELDS)),
            lambda code=code, start=start, end=end, fq=fq: jqdatasdk.get_price(
                code, start_date=start, end_date=end, frequency="1m",
                fields=FIELDS, fq=fq, skip_paused=False,
            ),
        )
        if live is None or live.empty:
            continue
        if loc.empty:
            problems.append(f"{label}: 本地 0 行(该窗口未回补？)")
            continue
        idx = loc.index.intersection(live.index)
        for c in FIELDS:
            a = pd.to_numeric(loc.loc[idx, c], errors="coerce").to_numpy(float)
            b = pd.to_numeric(live.loc[idx, c], errors="coerce").to_numpy(float)
            both_nan = np.isnan(a) & np.isnan(b)
            bad = (~both_nan) & ~(np.abs(a - b) <= TOL)
            if bad.any():
                mx = np.nanmax(np.abs(a - b)[~both_nan])
                problems.append(f"{label}.{c}: {int(bad.sum())} 处不一致 (max_abs={mx:.3g})")
    assert not problems, " | ".join(problems[:20])
