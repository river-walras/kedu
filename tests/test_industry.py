"""行业分类校验:本地 kedu vs live jqdatasdk,逐值比对。

覆盖:
- get_industries        6 taxonomy × {历史/近期截面},secdf_compare(index=行业码)。
- get_industry_stocks   抽样行业码 × 多日期,sorted(list) 比对 live。
- get_industry          100 抽样 × 多日期,dict 深比对 + df=True 走 df_compare 比对 live。
- get_history_industry  聚宽是付费模块、无法比对 live;本地复刻自 industry_history,只做
                        结构 + 与 get_industry_stocks 的内部一致性检查。

**闸门**:industry_history 由逐股 get_industry walk 折叠成区间(get_history_industry 付费不可用)。
get_industry_stocks / get_industry 由该区间重建,本测试拿它们对齐 live 的点查结果 ——
即验证「逐股 walk → 折叠 → 服务」整条链与聚宽点查一致。

依赖:已跑 backfill_industry.py --lists --industry-backfill --build 把表灌好。
live 结果走快照缓存(首次落盘,重跑 0 配额),date=None 路径仅做本地等价 smoke(不耗配额)。
"""
from __future__ import annotations

import datetime as dt

import pytest

import kedu
import jqdatasdk

from tests._compare import df_compare, secdf_compare

TAXONOMIES = ["sw_l1", "sw_l2", "sw_l3", "jq_l1", "jq_l2", "zjw"]
# 显式稳定截面:2021 申万切换前 / 切换间 / 2024 证监会切换后 / 近期。
DATES = ["2018-06-01", "2022-05-07", "2024-03-01", "2026-05-06"]
MEMBER_DATES = ["2022-05-07", "2026-05-06"]  # 成分点查计配额,收敛到两档


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


@pytest.mark.parametrize("name", TAXONOMIES)
def test_get_industries(name, snap):
    for date in DATES:
        local = kedu.get_industries(name=name, date=date)
        live = snap.get(
            f"get_industries-{name}-{date}", (name, date),
            lambda n=name, d=date: jqdatasdk.get_industries(name=n, date=d))
        ok, issues = secdf_compare(local, live, f"get_industries({name}, {date})")
        assert ok, " | ".join(issues)


def test_get_history_industry_local_consistency():
    """get_history_industry 是聚宽付费 API,无法对 live 比对;查结构 + 与 get_industry_stocks 一致性。

    开区间(end_date 缺失)的成分股,应出现在今日该行业 get_industry_stocks 中;
    交叉验证两条读路径对同一 industry_history 区间的解释一致。
    """
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
    name = "sw_l1"
    h = kedu.get_history_industry(name)
    if h.empty:
        pytest.skip("industry_history 为空(先 --industry-backfill --build)")
    assert list(h.columns) == ["code", "start_date", "end_date", "stock"]
    open_rows = h[h["end_date"].isna()].head(20)
    for _, r in open_rows.iterrows():
        stocks = kedu.get_industry_stocks(r["code"], date=today)
        assert r["stock"] in stocks, f"开区间 {r['stock']}@{r['code']} 不在今日 get_industry_stocks"
    # securities 过滤路径:返回的行 stock 必须都在请求集合内
    sec = str(open_rows.iloc[0]["stock"]) if not open_rows.empty else None
    if sec:
        sub = kedu.get_history_industry(name, securities=[sec])
        assert not sub.empty and set(sub["stock"]) == {sec}


@pytest.mark.parametrize("name", TAXONOMIES)
def test_get_industry_stocks(name, snap):
    """区间重建 vs live 点查一致性闸门。"""
    for date in MEMBER_DATES:
        inds = kedu.get_industries(name=name, date=date)
        if inds.empty:
            pytest.skip(f"{name}@{date}: 本地行业列表为空(先灌 industries)")
        for code in list(inds.index[:3]):
            local = kedu.get_industry_stocks(code, date=date)
            live = snap.get(f"industry_stocks-{code}-{date}", (code, date),
                            lambda c=code, d=date: jqdatasdk.get_industry_stocks(c, date=d))
            _cmp_list(local, live, f"get_industry_stocks({code}, {date})")


def test_get_industry_dict(sample_codes, snap):
    """区间重建 vs live 点查一致性闸门(dict 形式)。"""
    codes = sample_codes[:40]
    for date in MEMBER_DATES:
        local = kedu.get_industry(codes, date=date)
        live = snap.get(f"get_industry-{date}", (tuple(codes), date),
                        lambda c=codes, d=date: jqdatasdk.get_industry(c, date=d))
        _cmp_dict(local, live, f"get_industry({date})")


def test_get_industry_df(sample_codes, snap):
    codes = sample_codes[:40]
    for date in MEMBER_DATES:
        local = kedu.get_industry(codes, date=date, df=True)
        live = snap.get(f"get_industry_df-{date}", (tuple(codes), date, "df"),
                        lambda c=codes, d=date: jqdatasdk.get_industry(c, date=d, df=True))
        if local.empty and live.empty:
            continue
        ok, msgs = df_compare(local, live, f"get_industry(df, {date})", keys=["code", "type"])
        assert ok, " | ".join(msgs)


def test_date_none_equiv_today():
    """date=None 即北京今天:纯本地等价 smoke,不耗配额。"""
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
    inds = kedu.get_industries("sw_l1")
    if inds.empty:
        pytest.skip("本地 industries 为空")
    code = inds.index[0]
    assert kedu.get_industry_stocks(code) == kedu.get_industry_stocks(code, date=today)
    assert kedu.get_industry(code) == kedu.get_industry(code, date=today)
