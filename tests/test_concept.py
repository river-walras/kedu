"""概念板块校验:本地 kedu vs live jqdatasdk,逐值比对。

覆盖:
- get_concepts        secdf_compare(index=概念码,列 name/start_date)。
- get_concept_stocks  抽样概念码 × 多日期,sorted(list) 比对。
- get_concept         100 抽样 × 多日期,dict 深比对。

概念无历史 API,本地 concept_history 由逐交易日 get_concept_stocks 快照折叠而来,
故 get_concept_stocks/get_concept 与 live 点查一致即验证了折叠逻辑(数据源同为点查)。

依赖:已跑 backfill_industry.py --concepts --concept-history-backfill --build-concept-intervals。
live 结果走快照缓存(首次落盘,重跑 0 配额)。
"""
from __future__ import annotations

import datetime as dt

import pytest

import kedu
import jqdatasdk

from tests._compare import secdf_compare

# 概念历史自 2016 起;取文档示例日 + 近期。
MEMBER_DATES = ["2019-07-15", "2026-05-06"]


@pytest.fixture(scope="module", autouse=True)
def _ch(clickhouse_auth):
    """全模块需要 ClickHouse 凭证;JQ 凭证由 snap 在缓存 miss 时惰性拉起。"""


def _cmp_list(local, live, label):
    a, b = sorted(local), sorted(live)
    assert a == b, (f"{label}: n local={len(a)} live={len(b)} "
                    f"only_local={sorted(set(a) - set(b))[:5]} only_live={sorted(set(b) - set(a))[:5]}")


def _cmp_dict(local, live, label):
    assert set(local) == set(live), (f"{label}: 键集差异 "
                                     f"local_only={set(local) - set(live)} live_only={set(live) - set(local)}")
    diffs = [k for k in local if local[k] != live[k]]
    assert not diffs, (f"{label}: {len(diffs)} 键值不同, 例 {diffs[0]}: "
                       f"local={local[diffs[0]]} live={live[diffs[0]]}")


def test_get_concepts(snap):
    local = kedu.get_concepts()
    live = snap.get("get_concepts", ("all",), lambda: jqdatasdk.get_concepts())
    ok, issues = secdf_compare(local, live, "get_concepts()")
    assert ok, " | ".join(issues)


def test_get_concept_stocks(snap):
    concepts = kedu.get_concepts()
    if concepts.empty:
        pytest.skip("本地 concepts 为空(先灌 concepts)")
    codes = list(concepts.index[:6])
    for date in MEMBER_DATES:
        for code in codes:
            local = kedu.get_concept_stocks(code, date=date)
            live = snap.get(f"concept_stocks-{code}-{date}", (code, date),
                            lambda c=code, d=date: jqdatasdk.get_concept_stocks(c, date=d))
            _cmp_list(local, live, f"get_concept_stocks({code}, {date})")


def test_get_concept_dict(sample_codes, snap):
    codes = sample_codes[:40]
    for date in MEMBER_DATES:
        local = kedu.get_concept(codes, date=date)
        live = snap.get(f"get_concept-{date}", (tuple(codes), date),
                        lambda c=codes, d=date: jqdatasdk.get_concept(c, date=d))
        _cmp_dict(local, live, f"get_concept({date})")


def test_date_none_equiv_today():
    """date=None 即北京今天:纯本地等价 smoke,不耗配额。"""
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
    concepts = kedu.get_concepts()
    if concepts.empty:
        pytest.skip("本地 concepts 为空")
    code = concepts.index[0]
    assert kedu.get_concept_stocks(code) == kedu.get_concept_stocks(code, date=today)
