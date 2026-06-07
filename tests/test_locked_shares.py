"""校验本地 kedu.get_locked_shares vs live jqdatasdk.get_locked_shares。

逐行比对 day/code/num/rate1/rate2(按 (code, day) 对齐,数值 abs<=1e-4,日期精确)。
覆盖 end_date 与 forward_count 两种窗口形式 + 同日多股东解禁求和(601318 2010) +
100 票宽窗口 offset 抽样。live 走快照缓存。
"""
from __future__ import annotations

import jqdatasdk
import pytest

from kedu import get_locked_shares as local_get_locked_shares

from tests._compare import df_compare

JQ_locked = jqdatasdk.get_locked_shares


def _run(snap, name, kwargs):
    """本地直算,live 走缓存,按 (code, day) 逐字段比对。"""
    local = local_get_locked_shares(**kwargs)

    def producer():
        return JQ_locked(**kwargs)

    live = snap.get(f"locked-{name}", ("locked", name, repr(sorted(kwargs.items()))), producer)
    return df_compare(local, live, name, keys=["code", "day"])


# (name, kwargs)
FIXED_CASES = [
    ("两票 end_date 形式", dict(stock_list=["002345.XSHE", "603025.XSHG"],
                              start_date="2020-01-01", end_date="2024-12-31")),
    ("单票宽窗口 603025", dict(stock_list="603025.XSHG",
                            start_date="2010-01-01", end_date="2024-12-31")),
    ("同日多股东求和 601318@2010", dict(stock_list="601318.XSHG",
                                  start_date="2010-01-01", end_date="2012-12-31")),
    ("forward_count 形式 600276", dict(stock_list="600276.XSHG",
                                     start_date="2018-01-01", forward_count=800)),
]


@pytest.mark.parametrize("name,kwargs", FIXED_CASES, ids=[c[0] for c in FIXED_CASES])
def test_locked_shares_fixed(name, kwargs, snap, clickhouse_auth):
    ok, msgs = _run(snap, name, kwargs)
    assert ok, " | ".join(msgs)


def test_locked_shares_100codes(sample_codes, snap):
    """100 票 × [2015, 2023] 宽窗口解禁,逐行比对 live。"""
    codes = sample_codes
    ok, msgs = _run(
        snap, "100票 2015-2023",
        dict(stock_list=codes, start_date="2015-01-01", end_date="2023-12-31"),
    )
    assert ok, " | ".join(msgs)
