"""融资融券 API 校验:本地 kedu vs live jqdatasdk,逐值 + 顺序敏感比对。

覆盖:
- get_mtss                 抽样单票/多票 × 区间 与 count 三路径,逐值比对 + 行序严格一致。
                           另含 assert 互斥(both/neither 抛 AssertionError)、fields 单字符串。
- get_margincash_stocks    显式日期为强 parity(顺序敏感 list);date=None 走本地一致性(0 配额)。
- get_marginsec_stocks     同上。
- 签名回归                 三函数参数名/顺序/默认值与 jqdatasdk 一致。

数据依赖:已跑 scripts/backfill_margin.py(mtss / margin_target_history);未灌则相关用例 skip。
live 结果走快照缓存(首次落盘,重跑 0 配额)。date=None 随同步新鲜度变化,不做 live 比对。
"""
from __future__ import annotations

import inspect

import pytest

import jqdatasdk
import kedu

from kedu.db import DATABASE, get_client
from tests._compare import df_compare


@pytest.fixture(scope="module", autouse=True)
def _ch(clickhouse_auth):
    """全模块需要 ClickHouse 凭证;JQ 凭证由 snap 惰性拉起。"""


def _count(table: str) -> int:
    """表行数;表不存在(未回补)视作 0,让用例 skip 而非报错。"""
    cli = get_client()
    if not cli.query(f"EXISTS TABLE {DATABASE}.{table}").result_rows[0][0]:
        return 0
    return cli.query(f"SELECT count() FROM {DATABASE}.{table}").result_rows[0][0]


def _order_key(df):
    """行序指纹:(sec_code, date) 序列,用于顺序敏感比对(df_compare 会排序,验不了行序)。"""
    import pandas as pd
    return list(zip(df["sec_code"].astype(str).tolist(),
                    [pd.Timestamp(x) for x in df["date"].tolist()]))


# ---------------------------------------------------------------------------
# get_mtss(逐值 + 行序)
# ---------------------------------------------------------------------------
def test_get_mtss_range(snap):
    if _count("mtss") == 0:
        pytest.skip("本地 mtss 为空(先跑 backfill_margin.py)")
    code, start, end = "000001.XSHE", "2016-01-01", "2016-04-01"
    local = kedu.get_mtss(code, start_date=start, end_date=end)
    if local.empty:
        pytest.skip("本地该区间无 mtss")
    live = snap.get(f"mtss-{code}-{start}-{end}", (code, start, end),
                    lambda: jqdatasdk.get_mtss(code, start_date=start, end_date=end))
    ok, msgs = df_compare(local, live, f"get_mtss({code})", keys=["date", "sec_code"])
    assert ok, " | ".join(msgs)
    assert _order_key(local) == _order_key(live), "get_mtss 行序与 jqdatasdk 不一致"


def test_get_mtss_multi_order(snap):
    """多票:行序须按 security_list 入参顺序、组内 date 升序。"""
    if _count("mtss") == 0:
        pytest.skip("本地 mtss 为空")
    codes, start, end = ["000002.XSHE", "000001.XSHE"], "2016-01-04", "2016-01-06"
    local = kedu.get_mtss(codes, start_date=start, end_date=end)
    if local.empty:
        pytest.skip("本地该区间无 mtss")
    live = snap.get(f"mtss-multi-{'-'.join(codes)}-{start}-{end}", (tuple(codes), start, end),
                    lambda: jqdatasdk.get_mtss(codes, start_date=start, end_date=end))
    ok, msgs = df_compare(local, live, "get_mtss(multi)", keys=["date", "sec_code"])
    assert ok, " | ".join(msgs)
    assert _order_key(local) == _order_key(live), "get_mtss 多票行序与 jqdatasdk 不一致"


def test_get_mtss_count(snap):
    if _count("mtss") == 0:
        pytest.skip("本地 mtss 为空")
    code, end, n = "000001.XSHE", "2016-06-30", 5
    local = kedu.get_mtss(code, end_date=end, count=n)
    if local.empty:
        pytest.skip("本地无数据")
    live = snap.get(f"mtss-count-{code}-{end}-{n}", (code, end, n),
                    lambda: jqdatasdk.get_mtss(code, end_date=end, count=n))
    ok, msgs = df_compare(local, live, f"get_mtss count({code})", keys=["date", "sec_code"])
    assert ok, " | ".join(msgs)
    assert _order_key(local) == _order_key(live), "get_mtss count 行序不一致"


def test_get_mtss_assert_mutual_exclusive():
    """start_date 与 count 恰好二选一:都给/都缺均抛 AssertionError(照搬聚宽源码)。"""
    with pytest.raises(AssertionError):
        kedu.get_mtss("000001.XSHE", start_date="2016-01-01", count=5)
    with pytest.raises(AssertionError):
        kedu.get_mtss("000001.XSHE")  # both None


def test_get_mtss_fields_single_string():
    """fields 单字符串不应被拆成字符列;空结果也应返回正确列。"""
    if _count("mtss") == 0:
        pytest.skip("本地 mtss 为空")
    df = kedu.get_mtss("000001.XSHE", start_date="2016-01-01", end_date="2016-01-15",
                       fields="fin_value")
    assert list(df.columns) == ["fin_value"], f"fields='fin_value' 列错误:{list(df.columns)}"


# ---------------------------------------------------------------------------
# 标的列表(显式日期强 parity;date=None 本地一致性)
# ---------------------------------------------------------------------------
def test_get_margincash_stocks(snap):
    if _count("margin_target_history") == 0:
        pytest.skip("本地 margin_target_history 为空(先跑 backfill_margin.py)")
    date = "2018-07-02"
    local = kedu.get_margincash_stocks(date=date)
    if not local:
        pytest.skip("本地该日无融资标的")
    live = snap.get(f"margincash-{date}", (date,),
                    lambda: jqdatasdk.get_margincash_stocks(date=date))
    assert local == list(live), (
        f"get_margincash_stocks({date}): n local={len(local)} live={len(live)}")


def test_get_marginsec_stocks(snap):
    if _count("margin_target_history") == 0:
        pytest.skip("本地 margin_target_history 为空")
    date = "2018-07-05"
    local = kedu.get_marginsec_stocks(date=date)
    if not local:
        pytest.skip("本地该日无融券标的")
    live = snap.get(f"marginsec-{date}", (date,),
                    lambda: jqdatasdk.get_marginsec_stocks(date=date))
    assert local == list(live), (
        f"get_marginsec_stocks({date}): n local={len(local)} live={len(live)}")


def test_margin_targets_date_none_local():
    """date=None == 本地 staging 最近披露日的列表(纯本地,0 配额,验锚点逻辑)。"""
    if _count("margin_target_history") == 0 or _count("margin_target_raw") == 0:
        pytest.skip("本地标的表为空")
    cli = get_client()
    for kind, fn in [("cash", kedu.get_margincash_stocks), ("sec", kedu.get_marginsec_stocks)]:
        cnt, mx = cli.query(
            f"SELECT count(), max(date) FROM {DATABASE}.margin_target_raw WHERE type='{kind}'"
        ).result_rows[0]
        if not cnt:
            continue
        assert fn() == fn(date=mx.isoformat()), f"{kind}: date=None 应等于最近披露日 {mx}"


# ---------------------------------------------------------------------------
# 签名回归
# ---------------------------------------------------------------------------
def _sig(fn):
    return inspect.signature(fn).parameters


@pytest.mark.parametrize("name", ["get_mtss", "get_margincash_stocks", "get_marginsec_stocks"])
def test_margin_fn_signature(name):
    """参数名/顺序/默认值与 jqdatasdk 逐项一致(三函数默认均无冻结日期 quirk)。"""
    ours, native = _sig(getattr(kedu, name)), _sig(getattr(jqdatasdk, name))
    assert list(ours) == list(native), f"{name} 参数名/顺序不一致 ours={list(ours)} native={list(native)}"
    assert [p.default for p in ours.values()] == [p.default for p in native.values()], \
        f"{name} 默认值不一致"
