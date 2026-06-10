"""get_billboard_list 龙虎榜校验:本地 kedu vs live jqdatasdk。"""

from __future__ import annotations

import datetime as dt
import inspect

import pandas as pd
import pytest

import jqdatasdk
import kedu

from kedu.billboard import BILLBOARD_COLUMNS
from kedu.db import DATABASE, get_client
from tests._compare import df_compare


@pytest.fixture(scope="module", autouse=True)
def _ch(clickhouse_auth):
    """本模块需要 ClickHouse；live 由 snap 惰性登录。"""


def _count(table: str) -> int:
    cli = get_client()
    if not cli.query(f"EXISTS TABLE {DATABASE}.{table}").result_rows[0][0]:
        return 0
    return cli.query(f"SELECT count() FROM {DATABASE}.{table}").result_rows[0][0]


def _norm(v):
    try:
        if v is None or v is pd.NaT or pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, pd.Timestamp):
        return v.date().isoformat()
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat()[:10]
    return v


def _fingerprint(df: pd.DataFrame):
    cols = [
        "code",
        "day",
        "direction",
        "rank",
        "abnormal_code",
        "abnormal_name",
        "sales_depart_name",
        "buy_value",
        "sell_value",
        "net_value",
    ]
    return [tuple(_norm(row[c]) for c in cols) for _, row in df.iterrows()]


def _assert_day_dtype(df: pd.DataFrame) -> None:
    if df.empty:
        return
    assert (
        df["day"]
        .map(lambda x: isinstance(x, dt.date) and not isinstance(x, dt.datetime))
        .all()
    )
    assert str(df["day"].dtype) == "object"


def _run(snap, name: str, kwargs: dict):
    local = kedu.get_billboard_list(**kwargs)
    if local.empty:
        pytest.skip("本地 billboard 该窗口无数据")
    live = snap.get(
        f"billboard-{name}",
        ("billboard", name, repr(sorted(kwargs.items()))),
        lambda: jqdatasdk.get_billboard_list(**kwargs),
    )
    ok, msgs = df_compare(
        local,
        live,
        name,
        keys=[
            "code",
            "day",
            "direction",
            "rank",
            "abnormal_code",
            "abnormal_name",
            "sales_depart_name",
        ],
    )
    assert ok, " | ".join(msgs)
    assert list(local.columns) == BILLBOARD_COLUMNS
    assert _fingerprint(local) == _fingerprint(live), f"{name}: 原始行序不一致"
    _assert_day_dtype(local)


def test_billboard_docs_all_market_count1(snap):
    if _count("billboard") == 0:
        pytest.skip("本地 billboard 为空(先跑 backfill_billboard.py)")
    _run(
        snap,
        "docs-all-market-count1",
        dict(
            stock_list=None,
            end_date="2022-08-01",
            count=1,
        ),
    )


def test_billboard_single_stock_str(snap):
    if _count("billboard") == 0:
        pytest.skip("本地 billboard 为空")
    _run(
        snap,
        "single-stock-str",
        dict(
            stock_list="688786.XSHG",
            end_date="2022-08-01",
            count=1,
        ),
    )


def test_billboard_stock_list(snap):
    if _count("billboard") == 0:
        pytest.skip("本地 billboard 为空")
    _run(
        snap,
        "stock-list",
        dict(
            stock_list=["688786.XSHG", "000001.XSHE"],
            end_date="2022-08-01",
            count=1,
        ),
    )


def test_billboard_start_end_range(snap):
    if _count("billboard") == 0:
        pytest.skip("本地 billboard 为空")
    _run(
        snap,
        "start-end-range",
        dict(
            stock_list="688786.XSHG",
            start_date="2022-08-01",
            end_date="2022-08-05",
        ),
    )


def test_billboard_count_non_trade_end(snap):
    if _count("billboard") == 0:
        pytest.skip("本地 billboard 为空")
    _run(
        snap,
        "count-non-trade-end",
        dict(
            stock_list=None,
            end_date="2022-08-07",
            count=1,
        ),
    )


def test_billboard_empty_list(snap):
    local = kedu.get_billboard_list(stock_list=[], end_date="2022-08-01", count=1)
    live = snap.get(
        "billboard-empty-list",
        ("billboard", "empty-list", "2022-08-01", 1),
        lambda: jqdatasdk.get_billboard_list(
            stock_list=[], end_date="2022-08-01", count=1
        ),
    )
    assert local.empty and live.empty
    assert list(local.columns) == list(live.columns) == BILLBOARD_COLUMNS


def test_billboard_parameter_errors():
    with pytest.raises(Exception, match="必须指定 start_date 或 count"):
        kedu.get_billboard_list(stock_list="688786.XSHG")
    with pytest.raises(Exception, match="不能同时指定 start_date 和 count"):
        kedu.get_billboard_list(
            stock_list="688786.XSHG", start_date="2022-08-01", count=1
        )
    with pytest.raises(Exception, match="必须指定 start_date 或 count"):
        kedu.get_billboard_list(stock_list=None, end_date="2022-08-01", count=0)


def test_billboard_signature():
    ours = inspect.signature(kedu.get_billboard_list).parameters
    native = inspect.signature(jqdatasdk.get_billboard_list).parameters
    assert list(ours) == list(native)
    assert [p.default for p in ours.values()] == [p.default for p in native.values()]
