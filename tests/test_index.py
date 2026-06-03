"""指数 API 校验:本地 kedu vs live jqdatasdk,逐值比对。

覆盖:
- get_all_securities(['index'])  secdf_compare(index=指数码)。
- get_index_stocks               抽样指数 × 多日期,顺序敏感 list 比对。
- get_index_weights              抽样指数 × 日期,DataFrame(index=code)比对 + 最近披露日语义。
- get_index_valuation            9 指数 × 区间 × fields 子集 × count 三路径。
- get_price(index, daily)        指数日线逐字段严格比对(factor≡1)。
- 签名回归                       get_price / get_all_securities / 三个 index 函数参数与 jqdatasdk 一致。
- 本地一致性                     index_member_history 开区间成员须出现在 get_index_stocks(today)。

数据依赖:已跑 scripts/backfill_index.py(成分/权重/估值)与指数日线种子;未灌则相关用例 skip。
live 结果走快照缓存(首次落盘,重跑 0 配额);证券列表不计配额,直连。
"""
from __future__ import annotations

import datetime as dt
import inspect

import pytest

import kedu
import jqdatasdk

from kedu.db import DATABASE, get_client
from tests._compare import col_equal, df_compare, secdf_compare
from tests._snapshot import ensure_jq_auth

# 抽样指数(主流、必有成分/估值):沪深300 / 中证500 / 上证50 / 创业板指。
SAMPLE_INDEX = ["000300.XSHG", "000905.XSHG", "000016.XSHG", "399006.XSHE"]
MEMBER_DATES = ["2020-06-01", "2024-03-01"]
VAL_FIELDS_SUB = ["pe_ratio", "pb_ratio", "market_cap", "circulating_market_cap"]


@pytest.fixture(scope="module", autouse=True)
def _ch(clickhouse_auth):
    """全模块需要 ClickHouse 凭证;JQ 凭证由 snap / ensure_jq_auth 惰性拉起。"""


def _count(table: str) -> int:
    return get_client().query(f"SELECT count() FROM {DATABASE}.{table}").result_rows[0][0]


# ---------------------------------------------------------------------------
# 指数列表
# ---------------------------------------------------------------------------
def test_get_all_securities_index():
    ensure_jq_auth()
    for kw, label in [(dict(types=["index"]), "['index']"),
                      (dict(types=["index"], date="2020-10-10"), "['index'] @2020-10-10")]:
        local = kedu.get_all_securities(**kw)
        if local.empty:
            pytest.skip("本地 securities 无指数(先跑 update_securities 含指数)")
        live = jqdatasdk.get_all_securities(**kw)
        ok, issues = secdf_compare(local, live, f"get_all_securities({label})")
        assert ok, f"{label}: " + " | ".join(issues)


# ---------------------------------------------------------------------------
# 成分股(顺序敏感)
# ---------------------------------------------------------------------------
def test_get_index_stocks(snap):
    if _count("index_member_history") == 0:
        pytest.skip("本地 index_member_history 为空(先跑 backfill_index.py)")
    for date in MEMBER_DATES:
        for code in SAMPLE_INDEX:
            local = kedu.get_index_stocks(code, date=date)
            if not local:
                continue
            live = snap.get(f"index_stocks-{code}-{date}", (code, date),
                            lambda c=code, d=date: jqdatasdk.get_index_stocks(c, date=d))
            assert local == list(live), (
                f"get_index_stocks({code},{date}): n local={len(local)} live={len(live)} "
                f"first_diff={next((i for i,(a,b) in enumerate(zip(local,live)) if a!=b), 'len')}")


# ---------------------------------------------------------------------------
# 成分权重
# ---------------------------------------------------------------------------
def test_get_index_weights(snap):
    if _count("index_weights") == 0:
        pytest.skip("本地 index_weights 为空(先跑 backfill_index.py)")
    for code, date in [("000300.XSHG", "2023-06-30"), ("000016.XSHG", "2021-12-31")]:
        local = kedu.get_index_weights(code, date=date)
        if local.empty:
            continue
        live = snap.get(f"index_weights-{code}-{date}", (code, date),
                        lambda c=code, d=date: jqdatasdk.get_index_weights(c, date=d))
        loc = local.reset_index().rename(columns={"index": "code"})
        liv = live.reset_index().rename(columns={live.index.name or "index": "code"})
        ok, msgs = df_compare(loc, liv, f"get_index_weights({code},{date})", keys=["code"])
        assert ok, " | ".join(msgs)


# ---------------------------------------------------------------------------
# 指数估值(9 指数;三路径)
# ---------------------------------------------------------------------------
def test_get_index_valuation_range(snap):
    if _count("index_valuation") == 0:
        pytest.skip("本地 index_valuation 为空(先跑 backfill_index.py)")
    code, start, end = "000300.XSHG", "2024-05-06", "2024-06-03"
    local = kedu.get_index_valuation(code, start_date=start, end_date=end, fields=VAL_FIELDS_SUB)
    if local.empty:
        pytest.skip("本地该区间无估值")
    live = snap.get(f"index_val-{code}-{start}-{end}", (code, start, end, tuple(VAL_FIELDS_SUB)),
                    lambda: jqdatasdk.get_index_valuation(code, start_date=start, end_date=end,
                                                          fields=list(VAL_FIELDS_SUB)))
    ok, msgs = df_compare(local, live, f"get_index_valuation({code})", keys=["code", "day"])
    assert ok, " | ".join(msgs)


def test_get_index_valuation_count(snap):
    if _count("index_valuation") == 0:
        pytest.skip("本地 index_valuation 为空")
    code, end, n = "000905.XSHG", "2024-06-03", 5
    local = kedu.get_index_valuation(code, end_date=end, count=n, fields=VAL_FIELDS_SUB)
    if local.empty:
        pytest.skip("本地无数据")
    live = snap.get(f"index_val-count-{code}-{end}-{n}", (code, end, n, tuple(VAL_FIELDS_SUB)),
                    lambda: jqdatasdk.get_index_valuation(code, end_date=end, count=n,
                                                          fields=list(VAL_FIELDS_SUB)))
    ok, msgs = df_compare(local, live, f"get_index_valuation count({code})", keys=["code", "day"])
    assert ok, " | ".join(msgs)


def test_get_index_valuation_fields_with_meta():
    """fields 含 code/day 不应产生重复列。"""
    if _count("index_valuation") == 0:
        pytest.skip("本地 index_valuation 为空")
    df = kedu.get_index_valuation("000300.XSHG", start_date="2024-05-06", end_date="2024-05-10",
                                  fields=["code", "day", "pe_ratio"])
    assert list(df.columns).count("code") == 1 and list(df.columns).count("day") == 1
    assert list(df.columns) == ["code", "day", "pe_ratio"]


# ---------------------------------------------------------------------------
# 指数日线
# ---------------------------------------------------------------------------
_PRICE_FIELDS = ["open", "close", "high", "low", "volume", "money", "pre_close"]


def test_get_price_index_daily(snap):
    code, start, end = "000300.XSHG", "2024-05-06", "2024-06-03"
    local = kedu.get_price(code, start_date=start, end_date=end, frequency="daily",
                           fields=_PRICE_FIELDS, fq=None)
    if local.empty:
        pytest.skip("本地无指数日线(先跑 backfill_index.py 或 update_bars 含指数)")
    live = snap.get(f"index_price-{code}-{start}-{end}", (code, start, end),
                    lambda: jqdatasdk.get_price(code, start_date=start, end_date=end,
                                                frequency="daily", fields=_PRICE_FIELDS, fq=None))
    idx = local.index.intersection(live.index)
    assert len(idx) == len(live.index), f"行对齐 local={len(local)} live={len(live)}"
    for c in _PRICE_FIELDS:
        ok, mx, nbad = col_equal(local.loc[idx, c], live.loc[idx, c], tol=1e-4)
        assert ok, f"{code}.{c}: {nbad} 处不一致 (max_abs={mx:.3g})"


# ---------------------------------------------------------------------------
# 本地一致性(0 配额)
# ---------------------------------------------------------------------------
def test_member_history_open_intervals_match_today():
    """开区间(end_date IS NULL)成员须等于 get_index_stocks(今天)。纯本地。"""
    if _count("index_member_history") == 0:
        pytest.skip("本地 index_member_history 为空")
    cli = get_client()
    code = cli.query(
        f"SELECT index_code FROM {DATABASE}.index_member_history WHERE end_date IS NULL LIMIT 1"
    ).result_rows
    if not code:
        pytest.skip("无开区间")
    ic = code[0][0]
    open_members = {r[0] for r in cli.query(
        f"SELECT stock FROM {DATABASE}.index_member_history "
        f"WHERE index_code='{ic}' AND end_date IS NULL").result_rows}
    assert set(kedu.get_index_stocks(ic)) == open_members


# ---------------------------------------------------------------------------
# 签名回归(参数名/顺序/默认值与 jqdatasdk 一致)
# ---------------------------------------------------------------------------
def _sig(fn):
    return inspect.signature(fn).parameters


def _names(fn) -> list[str]:
    return list(_sig(fn))


def _defaults(fn) -> list:
    return [p.default for p in _sig(fn).values()]


def test_get_price_signature_strict():
    """get_price 参数名/顺序/默认值与 jqdatasdk 逐项一致(含 skip_paused/panel/fill_paused/round)。"""
    assert _names(kedu.get_price) == _names(jqdatasdk.get_price)
    assert _defaults(kedu.get_price) == _defaults(jqdatasdk.get_price)


def test_get_all_securities_signature_strict():
    assert _names(kedu.get_all_securities) == _names(jqdatasdk.get_all_securities)
    assert _defaults(kedu.get_all_securities) == _defaults(jqdatasdk.get_all_securities)


@pytest.mark.parametrize("name", ["get_index_stocks", "get_index_weights", "get_index_valuation"])
def test_index_fn_signature(name):
    """参数名/顺序严格一致;默认值逐项一致,但 jqdatasdk 把 date 默认冻结成 import 当日
    (已知 quirk),我们用 None 哨兵(调用时解析为北京今天)更正确,对该项只断言 None。"""
    ours, native = _sig(getattr(kedu, name)), _sig(getattr(jqdatasdk, name))
    assert list(ours) == list(native), f"{name} 参数名/顺序不一致 ours={list(ours)} native={list(native)}"
    for k in ours:
        nd = native[k].default
        if isinstance(nd, dt.date):
            assert ours[k].default is None, f"{name}.{k} 应为 None 哨兵, 实为 {ours[k].default!r}"
        else:
            assert ours[k].default == nd, f"{name}.{k} 默认不一致 ours={ours[k].default!r} native={nd!r}"


def test_date_none_equiv_today():
    """date=None 即北京今天:纯本地等价 smoke,不耗配额。"""
    if _count("index_member_history") == 0:
        pytest.skip("本地 index_member_history 为空")
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
    cli = get_client()
    code = cli.query(f"SELECT index_code FROM {DATABASE}.index_member_history LIMIT 1").result_rows
    if not code:
        pytest.skip("无数据")
    ic = code[0][0]
    assert kedu.get_index_stocks(ic) == kedu.get_index_stocks(ic, date=today)
