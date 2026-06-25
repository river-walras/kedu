"""*_day 物化表 vs *_day_view 视图 逐字节一致(tol=0)。

物化表是 get_fundamentals(date=) 查询热点的 drop-in, 必须与同源视图(共用
schema._day_select_body)在行集合 + 逐列上完全一致(见 day_materialize.compare_day)。
覆盖高风险样本日:年报/一季报同日披露(statDate 并列)、年中/年末、早期年份。

需先离线物化:`python -m kedu.day_materialize full-build --all`;尚未物化为表的关系自动 skip。
"""

from __future__ import annotations

import pytest

from kedu import day_materialize as daymat
from kedu.db import DATABASE, get_client
from kedu.schema import DAY_VIEWS

# 高风险锚点(年报/Q1 同披露季末附近、年中、年末、早期年份);解析为 <=锚点 的真实交易日,
# 确保比较非空。非交易日会落到上一交易日, 不会假阴性。
ANCHORS = [
    "2021-04-30",
    "2021-05-06",
    "2021-08-31",
    "2021-10-29",
    "2015-07-01",
    "2010-06-01",
]


def _to_iso(d) -> str:
    return d.isoformat() if hasattr(d, "isoformat") else str(d)


@pytest.fixture(scope="module")
def client(clickhouse_auth):
    cli = get_client()
    daymat.ensure_views(cli, verbose=False)  # A/B 校验需要 *_day_view
    return cli


@pytest.fixture(scope="module")
def sample_days(client):
    days: list[str] = []
    for a in ANCHORS:
        r = client.query(
            f"SELECT max(day) FROM {DATABASE}.stock_valuation WHERE day <= '{a}'"
        ).result_rows[0][0]
        if r is not None:
            days.append(_to_iso(r))
    mx = client.query(f"SELECT max(day) FROM {DATABASE}.stock_valuation").result_rows[
        0
    ][0]
    if mx is not None:
        days.append(_to_iso(mx))
    return sorted(set(days))


@pytest.mark.parametrize("name", list(DAY_VIEWS))
def test_day_table_matches_view(client, sample_days, name):
    if daymat._relation_kind(client, name) != "table":
        pytest.skip(
            f"{name} 尚未物化为表;先跑 `python -m kedu.day_materialize full-build --all`"
        )
    assert sample_days, "无样本交易日(stock_valuation 为空?)"
    for day in sample_days:
        ok, msg = daymat.compare_day(client, name, day)
        assert ok, msg
