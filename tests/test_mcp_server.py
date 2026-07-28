"""MCP server 的纯静态校验:渲染截断、受限 eval 的闸门、tool 注册面。

这些用例不碰 ClickHouse 也不碰聚宽,不请求 clickhouse_auth fixture,
因此无凭证环境也能跑。涉及 finance 表模型的路径要 DESCRIBE 实表,不在这里测。
"""

from __future__ import annotations

import asyncio

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("mcp", reason="MCP server 需要 `uv sync --extra mcp`")

from kedu.mcp_server import build_server  # noqa: E402
from kedu.mcp_server import _dsl, _render  # noqa: E402


# ---------------------------------------------------------------------------
# 渲染:截断必须显式可见,绝不静默
# ---------------------------------------------------------------------------
def test_render_dataframe_row_truncation_is_announced():
    df = pd.DataFrame({"a": range(50), "b": range(50)})
    out = _render.render(df, max_rows=5)
    assert "rows=50" in out and "shown_rows=5" in out
    assert "⚠ 行已截断" in out
    assert out.rstrip().endswith("4,4")  # 第 5 行(0-indexed 的 4)是最后一行
    assert "5,5" not in out


def test_render_dataframe_col_truncation_lists_dropped_columns():
    df = pd.DataFrame({f"c{i}": [i] for i in range(10)})
    out = _render.render(df, max_cols=3)
    assert "⚠ 列已截断" in out
    assert "未返回 7 列" in out
    assert "c3" in out  # 被丢的列名要点出来


def test_render_drops_rangeindex_but_keeps_named_index():
    plain = _render.render(pd.DataFrame({"a": [1, 2], "b": [3, 4]}))
    assert plain.splitlines()[-3] == "a,b"  # 表头无前导逗号 = 没输出 index 列
    assert "index=" not in plain

    dated = pd.DataFrame(
        {"a": [1]}, index=pd.DatetimeIndex(["2024-01-02"], name="date")
    )
    out = _render.render(dated)
    assert "index=DatetimeIndex" in out
    assert "date,a" in out


def test_render_unnamed_index_gets_placeholder_header():
    df = pd.DataFrame({"a": [1]}, index=pd.Index(["000001.XSHE"]))
    out = _render.render(df)
    assert "index,a" in out  # 不是裸逗号开头
    assert "原索引无名时占位显示为 'index'" in out


def test_render_covers_kedu_return_shapes():
    assert "kind=ndarray" in _render.render(np.array(["2024-01-02"], dtype=object))
    assert "kind=list" in _render.render(["000001.XSHE"])
    assert "kind=dict" in _render.render(
        {"000001.XSHE": {"sw_l1": {"industry_code": "801780"}}}
    )
    assert "kind=None" in _render.render(None)


def test_render_limits_are_clamped_to_hard_ceiling():
    rows, cols = _render.resolve_limits(10**9, 10**9)
    assert rows == _render.HARD_MAX_ROWS
    assert cols == _render.HARD_MAX_COLS


def test_render_env_override(monkeypatch):
    monkeypatch.setenv("KEDU_MCP_MAX_ROWS", "7")
    assert _render.resolve_limits()[0] == 7
    monkeypatch.setenv("KEDU_MCP_MAX_ROWS", "垃圾")
    assert _render.resolve_limits()[0] == _render.DEFAULT_MAX_ROWS


# ---------------------------------------------------------------------------
# 受限 eval:三道闸
# ---------------------------------------------------------------------------
def test_dsl_builds_query_object():
    from kedu._jqsdk import SqlQuery

    q = _dsl.eval_query("query(valuation).filter(valuation.pe_ratio < 10).limit(5)")
    assert isinstance(q, SqlQuery)


@pytest.mark.parametrize(
    "expr",
    [
        "().__class__.__mro__[1].__subclasses__()",  # 经典逃逸链
        "valuation.__dict__",
        "__import__('os').system('id')",
    ],
)
def test_dsl_rejects_dunder_access(expr):
    with pytest.raises(ValueError, match="下划线"):
        _dsl.eval_expr(expr)


@pytest.mark.parametrize(
    "expr",
    [
        "open('/etc/passwd').read()",
        "eval('1')",
        "globals()",
    ],
)
def test_dsl_has_no_builtins(expr):
    with pytest.raises(ValueError, match="未知名字"):
        _dsl.eval_expr(expr)


def test_dsl_rejects_statements_and_syntax_errors():
    with pytest.raises(ValueError, match="语法错误"):
        _dsl.eval_expr("import os")


def test_dsl_rejects_non_query_result():
    with pytest.raises(ValueError, match="不是查询对象"):
        _dsl.eval_query("1 + 1")


def test_dsl_rejects_empty_expression():
    with pytest.raises(ValueError, match="不能为空"):
        _dsl.eval_expr("   ")


def test_dsl_namespace_exposes_query_surface_and_finance_tables():
    names = _dsl.available_names()
    for expected in ("query", "valuation", "income", "or_", "STK_INCOME_STATEMENT"):
        assert expected in names


# ---------------------------------------------------------------------------
# tool 注册面
# ---------------------------------------------------------------------------
def _tools():
    return asyncio.run(build_server().list_tools())


def test_expected_tools_are_registered():
    names = {t.name for t in _tools()}
    assert names == {
        "kedu_get_price",
        "kedu_get_fundamentals",
        "kedu_get_fundamentals_continuously",
        "kedu_get_history_fundamentals",
        "kedu_finance_run_query",
        "kedu_get_valuation",
        "kedu_get_all_securities",
        "kedu_get_trade_days",
        "kedu_get_industry",
        "kedu_get_industry_stocks",
        "kedu_get_index_stocks",
        "kedu_get_extras",
        "kedu_describe",
        "kedu_call",
        "kedu_plot",
    }


def test_every_tool_has_a_description():
    assert all(t.description for t in _tools())


def test_dispatcher_covers_the_long_tail_of_kedu_all():
    """__all__ 里的函数要么有专用 tool, 要么能被 kedu_call 反射到, 不许漏。"""
    import kedu
    from kedu.mcp_server import _invoke

    reachable = set(_invoke.dispatchable()) | set(_invoke.DSL_TOOL_REDIRECT)
    for name in kedu.__all__:
        if name in _invoke.NOT_DISPATCHABLE:
            continue
        if callable(getattr(kedu, name, None)):
            assert name in reachable, f"{name} 既没有专用 tool 也进不了 dispatcher"


def test_query_object_apis_are_not_dispatchable():
    """吃查询对象的 API 不能进 dispatcher —— JSON 传不进去, 只会给出困惑的报错。"""
    from kedu.mcp_server import _invoke

    dispatchable = _invoke.dispatchable()
    for name in _invoke.DSL_TOOL_REDIRECT:
        assert name not in dispatchable
    assert "finance.run_offset_query" not in dispatchable
