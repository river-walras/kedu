"""证券列表校验:本地 get_all_securities vs live jqdatasdk。

重点验不同 date 截面返回一致(行集合 + display_name/name/type/start_date/end_date
逐字段 + dtype),以及 types 维度。

证券列表在 JQ 不计配额,故直接打 live(不走快照缓存),但仍需 JQ 凭证。
"""
from __future__ import annotations

import pytest

import kedu
import jqdatasdk

from tests._compare import secdf_compare
from tests._snapshot import ensure_jq_auth


@pytest.fixture(scope="module", autouse=True)
def _auth(clickhouse_auth):
    ensure_jq_auth()


# (kwargs, 标签):types 维度 + 多个历史 date 截面(早期 / 退市潮 / 近期)
CASES = [
    (dict(), "get_all_securities()"),
    (dict(types=["stock"]), "get_all_securities(['stock'])"),
    (dict(date="2010-06-01"), "date=2010-06-01"),
    (dict(date="2015-06-30"), "date=2015-06-30"),
    (dict(date="2018-12-28"), "date=2018-12-28"),
    (dict(date="2020-10-10"), "date=2020-10-10"),
    (dict(date="2024-05-10"), "date=2024-05-10"),
    (dict(types=["stock"], date="2013-03-15"), "['stock'] @2013-03-15"),
]


@pytest.mark.parametrize("kw,label", CASES, ids=[c[1] for c in CASES])
def test_get_all_securities(kw, label):
    local = kedu.get_all_securities(**kw)
    live = jqdatasdk.get_all_securities(**kw)
    ok, issues = secdf_compare(local, live, label)
    assert ok, f"{label} 不一致: " + " | ".join(issues)
