"""get_valuation 市值表校验:本地 kedu vs live jqdatasdk,逐字段逐值比对。

数据源 jqdata.stock_valuation 由 update_jqdata step 3 维护,常态已满,故可直接比对。
live 走快照缓存(首次落盘,重跑 0 配额)。
"""

from __future__ import annotations

import datetime as dt
import inspect

import pandas as pd
import pytest

import jqdatasdk
import kedu

from kedu.db import DATABASE, get_client
from kedu.valuation import VALUATION_FIELDS
from tests._compare import df_compare

# 抽样股票(流动性好、跨交易所/板块,区间内必有估值)。
SAMPLE = [
    "000001.XSHE",
    "600519.XSHG",
    "000651.XSHE",
    "601318.XSHG",
    "300750.XSHE",
    "600009.XSHG",
]
FIELDS_SUB = ["market_cap", "pe_ratio", "circulating_market_cap"]


@pytest.fixture(scope="module", autouse=True)
def _ch(clickhouse_auth):
    """本模块需要 ClickHouse;live 由 snap 惰性登录。"""


def _count(table: str) -> int:
    cli = get_client()
    if not cli.query(f"EXISTS TABLE {DATABASE}.{table}").result_rows[0][0]:
        return 0
    return cli.query(f"SELECT count() FROM {DATABASE}.{table}").result_rows[0][0]


def _order_key(df: pd.DataFrame):
    """按输出行序返回 (day, code) 序列,用于校验行序而非仅集合。"""
    return list(zip(df["day"].astype(str).tolist(), df["code"].astype(str).tolist()))


def _assert_match(local: pd.DataFrame, live: pd.DataFrame, name: str):
    ok, msgs = df_compare(local, live, name, keys=["day", "code"])
    assert ok, " | ".join(msgs)
    assert list(local.columns) == list(live.columns), (
        f"{name}: 列序 local={list(local.columns)} live={list(live.columns)}"
    )
    assert _order_key(local) == _order_key(live), f"{name}: 原始行序不一致"


def test_valuation_single_count(snap):
    if _count("stock_valuation") == 0:
        pytest.skip("本地 stock_valuation 为空")
    kw = dict(security_list="000001.XSHE", end_date="2024-11-18", count=3)
    local = kedu.get_valuation(**kw)
    if local.empty:
        pytest.skip("本地该区间无估值")
    live = snap.get(
        "valuation-single-count",
        ("single-count", repr(sorted(kw.items()))),
        lambda: jqdatasdk.get_valuation(**kw),
    )
    _assert_match(local, live, "single-count")


def test_valuation_multi_count_order(snap):
    """多票 count:验 (day, code) 升序行序。"""
    if _count("stock_valuation") == 0:
        pytest.skip("本地 stock_valuation 为空")
    kw = dict(security_list=SAMPLE, end_date="2024-11-18", count=2, fields=FIELDS_SUB)
    local = kedu.get_valuation(**kw)
    if local.empty:
        pytest.skip("本地无数据")
    live = snap.get(
        "valuation-multi-count",
        (
            "multi-count",
            repr(
                sorted(
                    (k, tuple(v) if isinstance(v, list) else v) for k, v in kw.items()
                )
            ),
        ),
        lambda: jqdatasdk.get_valuation(**kw),
    )
    _assert_match(local, live, "multi-count")


def test_valuation_range(snap):
    if _count("stock_valuation") == 0:
        pytest.skip("本地 stock_valuation 为空")
    kw = dict(
        security_list=SAMPLE,
        start_date="2024-11-11",
        end_date="2024-11-18",
        fields=FIELDS_SUB,
    )
    local = kedu.get_valuation(**kw)
    if local.empty:
        pytest.skip("本地无数据")
    live = snap.get(
        "valuation-range",
        (
            "range",
            repr(
                sorted(
                    (k, tuple(v) if isinstance(v, list) else v) for k, v in kw.items()
                )
            ),
        ),
        lambda: jqdatasdk.get_valuation(**kw),
    )
    _assert_match(local, live, "range")


def test_valuation_fields_none_full(snap):
    """fields=None 返回 valuation 全列序。"""
    if _count("stock_valuation") == 0:
        pytest.skip("本地 stock_valuation 为空")
    kw = dict(
        security_list="600519.XSHG", start_date="2024-11-14", end_date="2024-11-18"
    )
    local = kedu.get_valuation(**kw)
    if local.empty:
        pytest.skip("本地无数据")
    assert list(local.columns) == ["code", "day", *VALUATION_FIELDS]
    live = snap.get(
        "valuation-fields-none",
        ("fields-none", repr(sorted(kw.items()))),
        lambda: jqdatasdk.get_valuation(**kw),
    )
    _assert_match(local, live, "fields-none")


def test_valuation_fields_with_meta():
    """fields 含 code/day 不产生重复列,列序恒 [code, day, *其余]。"""
    if _count("stock_valuation") == 0:
        pytest.skip("本地 stock_valuation 为空")
    df = kedu.get_valuation(
        "000001.XSHE",
        start_date="2024-11-14",
        end_date="2024-11-18",
        fields=["code", "day", "pe_ratio"],
    )
    assert list(df.columns).count("code") == 1 and list(df.columns).count("day") == 1
    assert list(df.columns) == ["code", "day", "pe_ratio"]


def test_valuation_day_is_date():
    """day 输出 Python datetime.date(object),对齐聚宽(非 datetime64)。"""
    if _count("stock_valuation") == 0:
        pytest.skip("本地 stock_valuation 为空")
    df = kedu.get_valuation("000001.XSHE", end_date="2024-11-18", count=1)
    if df.empty:
        pytest.skip("本地无数据")
    assert df["day"].dtype == object
    assert isinstance(df["day"].iloc[0], dt.date)


def test_valuation_no_truncation(sample_codes):
    """本地不施加聚宽 10000 行截断:宽窗口 × 多票可超 10000 行。纯本地,0 配额。"""
    if _count("stock_valuation") == 0:
        pytest.skip("本地 stock_valuation 为空")
    df = kedu.get_valuation(
        sample_codes,
        start_date="2023-01-01",
        end_date="2023-12-31",
        fields=["market_cap"],
    )
    assert len(df) > 10000, f"预期 > 10000 行(证不截断),实得 {len(df)}"


def test_valuation_param_errors():
    with pytest.raises(Exception, match="only one param is required"):
        kedu.get_valuation(
            "000001.XSHE", start_date="2024-11-11", end_date="2024-11-18", count=2
        )
    with pytest.raises(Exception, match="count 参数需要大于 0"):
        kedu.get_valuation("000001.XSHE", end_date="2024-11-18", count=0)
    with pytest.raises(Exception, match="不支持的字段"):
        kedu.get_valuation(
            "000001.XSHE", end_date="2024-11-18", count=1, fields=["bogus"]
        )


def test_valuation_signature():
    ours = inspect.signature(kedu.get_valuation).parameters
    native = inspect.signature(jqdatasdk.get_valuation).parameters
    assert list(ours) == list(native)
    assert [p.default for p in ours.values()] == [p.default for p in native.values()]
