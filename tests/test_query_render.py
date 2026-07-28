"""kedu_query MCP 文本结果格式。"""

from __future__ import annotations

from kedu.mcp_server._query import render_query_output
from kedu.sql_query import QueryOutput


def test_query_render_has_types_csv_nulls_and_warnings():
    result = QueryOutput(
        column_names=("code", "values", "missing"),
        column_types=("String", "Array(UInt8)", "Nullable(String)"),
        rows=(("000001.XSHE", [1, 2], None),),
        truncated=True,
        query_id="query-1",
        elapsed_ms=12.3456,
        summary={"read_rows": "10", "read_bytes": "100"},
        warnings=("需要 FINAL",),
    )
    rendered = render_query_output(result)
    assert "rows_shown=1 cols=3 truncated=true" in rendered
    assert "query_id=query-1 elapsed_ms=12.346" in rendered
    assert "code:String" in rendered
    assert "stats: read_rows=10 read_bytes=100" in rendered
    assert "WARNING: 需要 FINAL" in rendered
    assert "000001.XSHE," in rendered
    assert '"[1,2]"' in rendered
    assert r"\N" in rendered
    assert "总行数未知" in rendered


def test_query_render_handles_empty_result():
    rendered = render_query_output(
        QueryOutput(
            column_names=("value",),
            column_types=("UInt8",),
            rows=(),
            truncated=False,
            query_id="",
            elapsed_ms=0,
            summary={},
            warnings=(),
        )
    )
    assert rendered.endswith("\nvalue")
