"""get_call_auction 集合竞价校验:本地 kedu vs live jqdatasdk,逐字段逐值比对。

数据源 jqdata.call_auction 由 scripts/backfill_call_auction.py 逐票灌入;未回补则相关用例 skip。
live 走快照缓存(首次落盘,重跑 0 配额)。
"""

from __future__ import annotations

import inspect

import pandas as pd
import pytest

import jqdatasdk
import kedu

from kedu.call_auction import CALL_AUCTION_FIELDS
from kedu.db import DATABASE, get_client
from tests._compare import df_compare

STOCK = "000001.XSHE"
STOCKS = ["000001.XSHE", "600519.XSHG"]
INDEX = "000300.XSHG"
START, END = "2024-09-02", "2024-09-05"


@pytest.fixture(scope="module", autouse=True)
def _ch(clickhouse_auth):
    """本模块需要 ClickHouse;live 由 snap 惰性登录。"""


def _count(table: str) -> int:
    cli = get_client()
    if not cli.query(f"EXISTS TABLE {DATABASE}.{table}").result_rows[0][0]:
        return 0
    return cli.query(f"SELECT count() FROM {DATABASE}.{table}").result_rows[0][0]


def _skip_if_empty():
    if _count("call_auction") == 0:
        pytest.skip("本地 call_auction 为空(先跑 scripts/backfill_call_auction.py)")


def _order_key(df: pd.DataFrame):
    """按输出行序返回 (code, time) 序列,用于校验 (code, time) 行序。"""
    return list(
        zip(df["code"].astype(str).tolist(), pd.to_datetime(df["time"]).tolist())
    )


def test_call_auction_single(snap):
    _skip_if_empty()
    local = kedu.get_call_auction(STOCK, START, END)
    if local.empty:
        pytest.skip("本地该区间无集合竞价")
    live = snap.get(
        "ca-single",
        ("single", STOCK, START, END),
        lambda: jqdatasdk.get_call_auction(STOCK, START, END),
    )
    ok, msgs = df_compare(local, live, "single", keys=["code", "time"])
    assert ok, " | ".join(msgs)
    assert list(local.columns) == list(live.columns)
    assert str(local["time"].dtype) == "datetime64[ns]"


def test_call_auction_multi_order(snap):
    """多票:验 (code, time) 行序(先某票全部日期,再下一票)。"""
    _skip_if_empty()
    local = kedu.get_call_auction(STOCKS, START, END)
    if local.empty:
        pytest.skip("本地无数据")
    live = snap.get(
        "ca-multi",
        ("multi", tuple(STOCKS), START, END),
        lambda: jqdatasdk.get_call_auction(STOCKS, START, END),
    )
    ok, msgs = df_compare(local, live, "multi", keys=["code", "time"])
    assert ok, " | ".join(msgs)
    assert _order_key(local) == _order_key(live), "多票 (code,time) 行序不一致"


def test_call_auction_fields_subset_drops_time(snap):
    """显式 fields 未含 'time' 时不返回 time 列(对齐聚宽),code 恒在最前。"""
    _skip_if_empty()
    flds = ["current", "volume", "money"]
    local = kedu.get_call_auction(STOCK, START, END, fields=flds)
    if local.empty:
        pytest.skip("本地无数据")
    assert list(local.columns) == ["code", *flds]
    live = snap.get(
        "ca-fields-sub",
        ("fields-sub", STOCK, START, END, tuple(flds)),
        lambda: jqdatasdk.get_call_auction(STOCK, START, END, fields=list(flds)),
    )
    ok, msgs = df_compare(local, live, "fields-sub", keys=["code"])
    assert ok, " | ".join(msgs)
    assert list(local.columns) == list(live.columns)


def test_call_auction_fields_none_full(snap):
    """fields=None 返回全 25 列(code + 全字段交错五档)。"""
    _skip_if_empty()
    local = kedu.get_call_auction(STOCK, START, END)
    if local.empty:
        pytest.skip("本地无数据")
    assert list(local.columns) == ["code", *CALL_AUCTION_FIELDS]
    assert len(local.columns) == 25


def test_call_auction_index(snap):
    """指数无盘口(五档本地 NaN / live None):仅逐值比对(df_compare 已按 NaN 等价处理)。"""
    _skip_if_empty()
    local = kedu.get_call_auction(INDEX, START, END)
    if local.empty:
        pytest.skip("本地无指数集合竞价")
    live = snap.get(
        "ca-index",
        ("index", INDEX, START, END),
        lambda: jqdatasdk.get_call_auction(INDEX, START, END),
    )
    ok, msgs = df_compare(local, live, "index", keys=["code", "time"])
    assert ok, " | ".join(msgs)


def test_call_auction_empty_window():
    """周末空窗口:返回全 25 列、0 行。纯本地。"""
    _skip_if_empty()
    local = kedu.get_call_auction(STOCK, "2024-09-07", "2024-09-08")  # 周六日
    assert local.empty
    assert list(local.columns) == ["code", *CALL_AUCTION_FIELDS]


def test_call_auction_signature():
    ours = inspect.signature(kedu.get_call_auction).parameters
    native = inspect.signature(jqdatasdk.get_call_auction).parameters
    assert list(ours) == list(native)
    assert [p.default for p in ours.values()] == [p.default for p in native.values()]
