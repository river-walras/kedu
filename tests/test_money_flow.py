"""get_money_flow_pro 日频资金流向校验:本地 kedu vs live jqdatasdk。"""

from __future__ import annotations

import inspect

import pandas as pd
import pytest

import jqdatasdk
import kedu

from kedu.db import DATABASE, get_client
from kedu.money_flow import BASE_FIELDS
from tests._compare import df_compare


@pytest.fixture(scope="module", autouse=True)
def _ch(clickhouse_auth):
    """本模块需要 ClickHouse；live 由 snap 惰性登录。"""


def _count(table: str) -> int:
    cli = get_client()
    if not cli.query(f"EXISTS TABLE {DATABASE}.{table}").result_rows[0][0]:
        return 0
    return cli.query(f"SELECT count() FROM {DATABASE}.{table}").result_rows[0][0]


def _order_key(df: pd.DataFrame):
    return list(
        zip(pd.to_datetime(df["time"]).tolist(), df["code"].astype(str).tolist())
    )


def _run(snap, name: str, kwargs: dict):
    local = kedu.get_money_flow_pro(**kwargs)
    if local.empty:
        pytest.skip("本地 money_flow_pro 该窗口无数据")
    live = snap.get(
        f"money-flow-{name}",
        ("money-flow", name, repr(sorted(kwargs.items()))),
        lambda: jqdatasdk.get_money_flow_pro(**kwargs),
    )
    ok, msgs = df_compare(local, live, name, keys=["time", "code"])
    assert ok, " | ".join(msgs)
    assert _order_key(local) == _order_key(live), f"{name}: 原始行序不一致"
    assert str(local["time"].dtype) == "datetime64[ns]"
    assert list(local.columns) == list(live.columns)


def test_money_flow_single_fields_none(snap):
    if _count("money_flow_pro") == 0:
        pytest.skip("本地 money_flow_pro 为空(先跑 backfill_money_flow.py)")
    _run(
        snap,
        "single-fields-none",
        dict(
            security_list="000001.XSHE",
            start_date="2024-02-26",
            end_date="2024-03-01",
            frequency="daily",
            fields=None,
            data_type="money",
        ),
    )


def test_money_flow_multi_time_code_order(snap):
    if _count("money_flow_pro") == 0:
        pytest.skip("本地 money_flow_pro 为空")
    _run(
        snap,
        "multi-fields-none",
        dict(
            security_list=["000001.XSHE", "000002.XSHE"],
            start_date="2024-02-26",
            end_date="2024-03-01",
            frequency="daily",
            fields=None,
            data_type="money",
        ),
    )


def test_money_flow_count_non_trade_end(snap):
    if _count("money_flow_pro") == 0:
        pytest.skip("本地 money_flow_pro 为空")
    _run(
        snap,
        "count-non-trade-end",
        dict(
            security_list="000001.XSHE",
            end_date="2024-03-03",
            count=2,
            frequency="daily",
            fields=BASE_FIELDS,
            data_type="money",
        ),
    )


@pytest.mark.parametrize("data_type", ["volume", "deal"])
def test_money_flow_data_types(snap, data_type):
    if _count("money_flow_pro") == 0:
        pytest.skip("本地 money_flow_pro 为空")
    _run(
        snap,
        f"data-type-{data_type}",
        dict(
            security_list="000001.XSHE",
            start_date="2024-02-26",
            end_date="2024-03-01",
            frequency="daily",
            fields=BASE_FIELDS,
            data_type=data_type,
        ),
    )


def test_money_flow_fields_order_and_netflow(snap):
    if _count("money_flow_pro") == 0:
        pytest.skip("本地 money_flow_pro 为空")
    _run(
        snap,
        "fields-order-netflow",
        dict(
            security_list="000001.XSHE",
            start_date="2024-02-26",
            end_date="2024-03-01",
            frequency="daily",
            fields=["netflow_s", "inflow_xl", "outflow_s"],
            data_type="money",
        ),
    )


def test_money_flow_parameter_errors():
    with pytest.raises(Exception, match="分钟数据"):
        kedu.get_money_flow_pro(
            "000001.XSHE",
            end_date="2024-02-28 14:55:00",
            count=1,
            frequency="1m",
            fields=["inflow_xl"],
        )
    with pytest.raises(
        Exception, match=r"data_type 只能是 \('money', 'volume', 'deal'\) 中的一个"
    ):
        kedu.get_money_flow_pro(
            "000001.XSHE",
            start_date="2024-02-26",
            end_date="2024-03-01",
            data_type="bad",
        )
    with pytest.raises(Exception, match="count 参数需要大于 0"):
        kedu.get_money_flow_pro("000001.XSHE", end_date="2024-03-01", count=0)
    with pytest.raises(
        Exception, match=r"\(start_date, count\) only one param is required"
    ):
        kedu.get_money_flow_pro(
            "000001.XSHE", start_date="2024-02-26", end_date="2024-03-01", count=2
        )


def test_money_flow_signature():
    ours = inspect.signature(kedu.get_money_flow_pro).parameters
    native = inspect.signature(jqdatasdk.get_money_flow_pro).parameters
    assert list(ours) == list(native)
    assert [p.default for p in ours.values()] == [p.default for p in native.values()]
