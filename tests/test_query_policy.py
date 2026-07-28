"""kedu_query SQL 策略与语义提示的纯静态测试。"""

from __future__ import annotations

import pytest

from kedu.query_catalog import query_warnings, semantic_notes
from kedu.query_policy import (
    QueryPolicyError,
    TableReference,
    prepare_query,
    validate_native_ast,
)
from kedu.sql_query import _query_config, _verify_readonly


def test_prepare_query_keeps_sql_and_extracts_tables():
    query = prepare_query(
        "SELECT code FROM jqdata.stock_valuation FINAL WHERE day >= {start:Date};",
        {"start": "2024-01-01"},
    )
    assert query.sql.endswith("{start:Date}")
    assert query.parameters == {"start": "2024-01-01"}
    assert query.references == (
        TableReference("stock_valuation", "jqdata", final=True),
    )


def test_prepare_query_excludes_cte_names_from_real_tables():
    query = prepare_query("WITH x AS (SELECT * FROM jqdata.trade_days) SELECT * FROM x")
    assert query.references == (TableReference("trade_days", "jqdata"),)


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("INSERT INTO jqdata.trade_days SELECT today()", "只允许 SELECT"),
        ("SELECT 1; SELECT 2", "只允许一条"),
        ("SELECT 1 SETTINGS max_threads=1", "SETTINGS"),
        ("SELECT 1 FORMAT JSON", "FORMAT"),
        ("SELECT 1 INTO OUTFILE 'x'", "INTO"),
        ("SELECT * FROM system.users", "只允许访问 jqdata"),
        ("SELECT * FROM alphaforge.foo", "只允许访问 jqdata"),
    ],
)
def test_prepare_query_rejects_unsafe_sql(sql, message):
    with pytest.raises(QueryPolicyError, match=message):
        prepare_query(sql)


def test_format_scalar_function_is_allowed():
    assert prepare_query("SELECT format('{}', 1) AS value").sql


@pytest.mark.parametrize("parameters", [{"bad-name": 1}, {"": 1}, {1: "x"}])
def test_prepare_query_rejects_bad_parameter_names(parameters):
    with pytest.raises(QueryPolicyError, match="参数名"):
        prepare_query("SELECT 1", parameters)


def test_native_ast_allows_select_explain_and_local_generators():
    select = validate_native_ast(
        """SelectWithUnionQuery (children 1)
 ExpressionList (children 1)
  SelectQuery (children 2)
   ExpressionList (children 1)
    Asterisk
   TablesInSelectQuery (children 1)
    TablesInSelectQueryElement (children 1)
     TableExpression (children 1)
      TableIdentifier jqdata.trade_days
"""
    )
    assert select.tables == (TableReference("trade_days", "jqdata"),)

    explain = validate_native_ast(
        """Explain EXPLAIN PIPELINE (children 1)
 SelectWithUnionQuery (children 1)
  ExpressionList (children 1)
   SelectQuery (children 1)
    ExpressionList (children 1)
     Literal UInt64_1
"""
    )
    assert explain.root.startswith("Explain")

    numbers = validate_native_ast(
        """SelectWithUnionQuery (children 1)
 ExpressionList (children 1)
  SelectQuery (children 1)
   TablesInSelectQuery (children 1)
    TablesInSelectQueryElement (children 1)
     TableExpression (children 1)
      Function numbers (children 1)
       ExpressionList (children 1)
        Literal UInt64_10
"""
    )
    assert numbers.table_functions == ("numbers",)


@pytest.mark.parametrize(
    ("ast", "message"),
    [
        ("InsertQuery (children 1)\n SelectWithUnionQuery", "只允许 SELECT"),
        (
            "SelectWithUnionQuery (children 1)\n TableIdentifier system.users",
            "只允许访问 jqdata",
        ),
        (
            """SelectWithUnionQuery (children 1)
 TableExpression (children 1)
  Function url (children 1)
""",
            "禁止表函数 url",
        ),
        (
            """SelectWithUnionQuery (children 1)
 ExpressionList (children 1)
  Function dictGet (children 1)
""",
            "禁止函数 dictGet",
        ),
        (
            """Explain EXPLAIN AST (children 1)
 InsertQuery (children 1)
  SelectWithUnionQuery
""",
            "禁止语句",
        ),
    ],
)
def test_native_ast_rejects_unsafe_nodes(ast, message):
    with pytest.raises(QueryPolicyError, match=message):
        validate_native_ast(ast)


def test_catalog_warnings_do_not_rewrite_semantics():
    warnings = query_warnings(
        [
            TableReference("stock_valuation", "jqdata", final=False),
            TableReference("bar_1d", "jqdata"),
            TableReference("industry_history", "jqdata"),
        ]
    )
    assert any("FINAL" in warning for warning in warnings)
    assert any("复权" in warning for warning in warnings)
    assert any("右端包含" in warning for warning in warnings)
    assert any(
        "pubDate <= day" in note for note in semantic_notes("income_statement_day")
    )


def test_query_credentials_never_fall_back_to_clickhouse_user(monkeypatch):
    monkeypatch.setenv("CLICKHOUSE_USER", "admin")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "admin-secret")
    monkeypatch.delenv("KEDU_READONLY_USER", raising=False)
    monkeypatch.delenv("KEDU_READONLY_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="不会回退"):
        _query_config()


def test_non_readonly_client_is_closed_and_rejected():
    class Result:
        first_row = ("0",)

    class Client:
        closed = False

        def query(self, *_args, **_kwargs):
            return Result()

        def close(self):
            self.closed = True

    client = Client()
    with pytest.raises(RuntimeError, match="readonly=1"):
        _verify_readonly(client)
    assert client.closed is True
