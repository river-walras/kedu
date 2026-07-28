"""kedu_query 的 MCP 适配与紧凑 CSV 渲染。"""

from __future__ import annotations

import csv
import json
from io import StringIO
from typing import Any

from ..query_catalog import render_catalog, render_table_description
from ..sql_query import QueryOutput, execute_query, get_query_client
from ._render import resolve_limits


def _cell(value: Any) -> Any:
    if value is None:
        return r"\N"
    if isinstance(value, bytes):
        return "0x" + value.hex()
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))
    return value


def render_query_output(result: QueryOutput) -> str:
    r"""QueryOutput -> 元数据头 + CSV；NULL 显示为 ``\N``。"""
    lines = [
        f"kind=ClickHouseQuery rows_shown={len(result.rows)} "
        f"cols={len(result.column_names)} truncated={str(result.truncated).lower()}",
        f"query_id={result.query_id or '(unknown)'} elapsed_ms={result.elapsed_ms:.3f}",
        "types: "
        + ", ".join(
            f"{name}:{data_type}"
            for name, data_type in zip(result.column_names, result.column_types)
        ),
    ]
    stats = []
    for key in ("read_rows", "read_bytes", "written_rows", "written_bytes"):
        if key in result.summary:
            stats.append(f"{key}={result.summary[key]}")
    if stats:
        lines.append("stats: " + " ".join(stats))
    lines.extend(f"WARNING: {warning}" for warning in result.warnings)
    if result.truncated:
        lines.append(
            "WARNING: 结果已达到出站行数上限；总行数未知，请收窄 SQL、显式 LIMIT 或执行 count()"
        )

    out = StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(result.column_names)
    writer.writerows([_cell(value) for value in row] for row in result.rows)
    return "\n".join(lines) + "\n" + out.getvalue().rstrip("\n")


def run(
    sql: str,
    parameters: dict[str, Any] | None = None,
    max_rows: int | None = None,
) -> str:
    rows, cols = resolve_limits(max_rows=max_rows)
    return render_query_output(
        execute_query(sql, parameters, max_rows=rows, max_cols=cols)
    )


def describe_table(table: str) -> str:
    client = get_query_client()
    if table.strip() == "*":
        return render_catalog(client)
    return render_table_description(client, table)
