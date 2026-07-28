"""kedu_query 使用独立只读 ClickHouse 账户的集成测试。"""

from __future__ import annotations

import os

import pytest

from kedu.query_catalog import render_catalog, render_table_description
from kedu.query_policy import QueryPolicyError
from kedu.sql_query import execute_query, get_query_client

pytestmark = pytest.mark.skipif(
    not os.getenv("KEDU_READONLY_USER") or "KEDU_READONLY_PASSWORD" not in os.environ,
    reason="需要 KEDU_READONLY_USER/KEDU_READONLY_PASSWORD",
)


def _run(sql, parameters=None, max_rows=200):
    return execute_query(
        sql,
        parameters,
        max_rows=max_rows,
        max_cols=40,
    )


def test_readonly_client_and_parameter_binding():
    client = get_query_client()
    readonly = client.query(
        "SELECT value FROM system.settings WHERE name = {name:String}",
        parameters={"name": "readonly"},
    ).first_row[0]
    assert str(readonly) == "1"

    result = _run("SELECT {value:UInt32} AS value", {"value": 42})
    assert result.rows == ((42,),)
    assert result.column_types == ("UInt32",)


def test_cte_window_table_function_and_streaming_truncation():
    result = _run(
        """WITH source AS (
             SELECT number FROM numbers(10)
           )
           SELECT number, sum(number) OVER (ORDER BY number) AS running_sum
           FROM source
           ORDER BY number""",
        max_rows=3,
    )
    assert result.rows == ((0, 0), (1, 1), (2, 3))
    assert result.truncated is True
    assert result.query_id

    qualified = _run(
        """SELECT number, row_number() OVER (ORDER BY number) AS rank
           FROM numbers(5)
           QUALIFY rank <= 2
           ORDER BY number"""
    )
    assert qualified.rows == ((0, 1), (1, 2))


def test_real_jqdata_read_and_semantic_warning():
    result = _run(
        "SELECT instrument_id, date, close FROM jqdata.bar_1d ORDER BY date DESC LIMIT 1"
    )
    assert len(result.rows) == 1
    assert any("复权" in warning for warning in result.warnings)


def test_explain_pipeline_is_supported():
    result = _run("EXPLAIN PIPELINE SELECT count() FROM jqdata.trade_days")
    assert result.column_names == ("explain",)
    assert any("ReadFrom" in row[0] for row in result.rows)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO jqdata.trade_days SELECT today()",
        "SELECT * FROM system.users",
        "SELECT * FROM url('http://example.com/data.csv', CSV)",
        "SELECT 1 SETTINGS max_threads=1",
    ],
)
def test_dangerous_queries_are_rejected(sql):
    with pytest.raises(QueryPolicyError):
        _run(sql)


def test_catalog_lists_public_tables_and_describes_semantics():
    client = get_query_client()
    catalog = render_catalog(client)
    assert "bar_1d" in catalog
    assert "index_sync_state" not in catalog
    description = render_table_description(client, "bar_1d")
    assert "instrument_id" in description
    assert "前复权" in description
