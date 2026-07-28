"""kedu_query 的只读 ClickHouse 执行器。"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any

import clickhouse_connect

from .db import DATABASE
from .query_catalog import query_warnings
from .query_policy import (
    NativeAstAnalysis,
    PreparedQuery,
    QueryPolicyError,
    TableReference,
    prepare_query,
    validate_native_ast,
)

_CLIENT = None
_CLIENT_CONFIG: tuple[str, int, str, str] | None = None
_CLIENT_LOCK = threading.Lock()


@dataclass(frozen=True)
class QueryOutput:
    """流式截断后的 ClickHouse 查询结果。"""

    column_names: tuple[str, ...]
    column_types: tuple[str, ...]
    rows: tuple[tuple[Any, ...], ...]
    truncated: bool
    query_id: str
    elapsed_ms: float
    summary: dict[str, Any]
    warnings: tuple[str, ...]


def _query_config() -> tuple[str, int, str, str]:
    try:
        username = os.environ["KEDU_READONLY_USER"]
        password = os.environ["KEDU_READONLY_PASSWORD"]
    except KeyError as exc:
        raise RuntimeError(
            f"缺少环境变量 {exc.args[0]}；kedu_query 只使用独立只读账户，"
            "不会回退到 CLICKHOUSE_USER"
        ) from None
    return (
        os.getenv("CLICKHOUSE_HOST", "localhost"),
        int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username,
        password,
    )


def _verify_readonly(client) -> None:
    row = client.query(
        "SELECT value FROM system.settings WHERE name = {name:String}",
        parameters={"name": "readonly"},
    ).first_row
    if not row or str(row[0]) != "1":
        client.close()
        raise RuntimeError("KEDU_READONLY_USER 未启用 readonly=1，拒绝执行 kedu_query")


def get_query_client():
    """返回只使用 KEDU_READONLY_* 的缓存客户端，并验证 readonly=1。"""
    config = _query_config()
    global _CLIENT, _CLIENT_CONFIG
    if _CLIENT is not None and _CLIENT_CONFIG == config:
        return _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is not None and _CLIENT_CONFIG == config:
            return _CLIENT
        if _CLIENT is not None:
            _CLIENT.close()
        host, port, username, password = config
        client = clickhouse_connect.get_client(
            host=host,
            port=port,
            username=username,
            password=password,
            database=DATABASE,
            client_name="kedu-query",
            autogenerate_query_id=True,
            send_receive_timeout=45,
        )
        _verify_readonly(client)
        _CLIENT = client
        _CLIENT_CONFIG = config
        return client


def reset_query_client() -> None:
    """关闭查询客户端缓存，供配置切换与测试使用。"""
    global _CLIENT, _CLIENT_CONFIG
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            _CLIENT.close()
        _CLIENT = None
        _CLIENT_CONFIG = None


def _clean_database_error(exc: Exception) -> str:
    return str(exc).split(" (for url ", 1)[0].strip()


def _native_analysis(client, prepared: PreparedQuery) -> NativeAstAnalysis:
    try:
        payload = client.raw_query(
            "EXPLAIN AST " + prepared.sql,
            parameters=prepared.parameters,
            fmt="TabSeparated",
        )
    except Exception as exc:
        raise QueryPolicyError(
            "ClickHouse 语法或只读策略拒绝查询: " + _clean_database_error(exc)
        ) from None
    return validate_native_ast(payload.decode("utf-8", errors="replace"))


def _references_for_warnings(
    prepared: PreparedQuery, native: NativeAstAnalysis
) -> tuple[TableReference, ...]:
    # SQLGlot 能定位每张表是否带 FINAL；遇到新 ClickHouse 语法时退回原生表清单。
    return prepared.references or native.tables


def execute_query(
    sql: str,
    parameters: dict[str, Any] | None = None,
    *,
    max_rows: int,
    max_cols: int,
    client=None,
) -> QueryOutput:
    """校验并执行单条只读查询，最多保留 max_rows 行。"""
    if max_rows <= 0 or max_cols <= 0:
        raise ValueError("max_rows/max_cols 必须为正整数")
    prepared = prepare_query(sql, parameters)
    cli = client or get_query_client()
    native = _native_analysis(cli, prepared)
    warnings = query_warnings(_references_for_warnings(prepared, native))

    started = time.perf_counter()
    try:
        stream_context = cli.query_rows_stream(
            prepared.sql,
            parameters=prepared.parameters,
        )
        source = stream_context.source
        column_names = tuple(source.column_names)
        column_types = tuple(data_type.name for data_type in source.column_types)
        if len(column_names) > max_cols:
            with stream_context:
                pass
            raise ValueError(
                f"查询返回 {len(column_names)} 列，超过 {max_cols} 列上限；请显式选择所需字段"
            )
        rows: list[tuple[Any, ...]] = []
        truncated = False
        with stream_context as stream:
            for row in stream:
                if len(rows) >= max_rows:
                    truncated = True
                    break
                rows.append(tuple(row))
        summary = dict(source.summary)
        query_id = source.query_id or str(summary.get("query_id", ""))
    except ValueError:
        raise
    except Exception as exc:
        raise RuntimeError(
            "ClickHouse 查询失败: " + _clean_database_error(exc)
        ) from None

    return QueryOutput(
        column_names=column_names,
        column_types=column_types,
        rows=tuple(rows),
        truncated=truncated,
        query_id=query_id,
        elapsed_ms=(time.perf_counter() - started) * 1000,
        summary=summary,
        warnings=warnings,
    )
