"""kedu_query 的 ClickHouse SQL 安全策略。

SQL 原文不会被转译或重写。SQLGlot 负责 token/常见 AST 检查，ClickHouse
``EXPLAIN AST`` 负责覆盖当前服务端支持的完整语法。数据库只读角色是最终安全
边界；本模块进一步把能力限制在 jqdata 数据域和只读分析语句。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import Dialect, exp
from sqlglot.errors import ParseError, TokenError
from sqlglot.tokens import Token, TokenType

from .db import DATABASE

MAX_SQL_BYTES = 256 * 1024
MAX_PARAMETERS = 256
MAX_PARAMETER_BYTES = 1024 * 1024

_PARAMETER_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ALLOWED_TABLE_FUNCTIONS = frozenset({"numbers", "values"})
_FORBIDDEN_SCALAR_FUNCTIONS = frozenset(
    {
        "benchmark",
        "currentprofiles",
        "currentroles",
        "enabledprofiles",
        "enabledroles",
        "getmergetreesetting",
        "getserverport",
        "getserversetting",
        "getsetting",
        "hostname",
        "queryid",
        "initialqueryid",
        "s3queue",
        "sleepeachrow",
        "sleep",
    }
)
_FORBIDDEN_QUERY_NODES = (
    "AlterQuery",
    "AttachQuery",
    "BackupQuery",
    "CreateQuery",
    "DeleteQuery",
    "DetachQuery",
    "DropQuery",
    "GrantQuery",
    "InsertQuery",
    "KillQuery",
    "OptimizeQuery",
    "RenameQuery",
    "RestoreQuery",
    "RevokeQuery",
    "SetQuery",
    "SystemQuery",
    "TruncateQuery",
    "UpdateQuery",
    "UseQuery",
)


class QueryPolicyError(ValueError):
    """SQL 被 kedu_query 的只读策略拒绝。"""


@dataclass(frozen=True)
class TableReference:
    """SQL 中的真实表引用；CTE 名不计入。"""

    name: str
    database: str = ""
    final: bool = False


@dataclass(frozen=True)
class PreparedQuery:
    """完成本地预检、可送往 ClickHouse 原生解析的查询。"""

    sql: str
    parameters: dict[str, Any]
    references: tuple[TableReference, ...]


@dataclass(frozen=True)
class NativeAstAnalysis:
    """ClickHouse EXPLAIN AST 中与权限策略相关的信息。"""

    root: str
    tables: tuple[TableReference, ...]
    table_functions: tuple[str, ...]
    functions: tuple[str, ...]


@dataclass(frozen=True)
class _NativeNode:
    label: str
    indent: int
    parent: int | None


def _tokens(sql: str) -> list[Token]:
    try:
        return Dialect.get_or_raise("clickhouse").tokenizer().tokenize(sql)
    except TokenError as exc:
        raise QueryPolicyError(f"SQL token 解析失败: {exc}") from None


def _normalize_statement(sql: str) -> tuple[str, list[Token]]:
    if not isinstance(sql, str) or not sql.strip():
        raise QueryPolicyError("SQL 不能为空")
    if "\x00" in sql:
        raise QueryPolicyError("SQL 不能包含 NUL 字节")
    if len(sql.encode("utf-8")) > MAX_SQL_BYTES:
        raise QueryPolicyError(f"SQL 超过 {MAX_SQL_BYTES} 字节上限")

    normalized = sql.strip()
    tokens = _tokens(normalized)
    semicolons = [
        i for i, token in enumerate(tokens) if token.token_type == TokenType.SEMICOLON
    ]
    if semicolons:
        trailing = (
            len(semicolons) == 1
            and semicolons[0] == len(tokens) - 1
            and normalized.endswith(";")
        )
        if not trailing:
            raise QueryPolicyError("只允许一条 SQL 语句")
        normalized = normalized[:-1].rstrip()
        tokens = _tokens(normalized)
    if not tokens:
        raise QueryPolicyError("SQL 不能为空")
    return normalized, tokens


def _validate_tokens(tokens: list[Token]) -> None:
    first = tokens[0]
    allowed_root = first.token_type in {TokenType.SELECT, TokenType.WITH} or (
        first.token_type == TokenType.COMMAND and first.text.upper() == "EXPLAIN"
    )
    if not allowed_root:
        raise QueryPolicyError(f"只允许 SELECT 或 EXPLAIN SELECT，收到 {first.text}")
    for index, token in enumerate(tokens):
        kind = token.token_type
        if kind == TokenType.SETTINGS:
            raise QueryPolicyError("禁止在 SQL 中使用 SETTINGS")
        if kind == TokenType.INTO:
            raise QueryPolicyError("禁止 INTO / INTO OUTFILE")
        if kind == TokenType.FORMAT:
            # ClickHouse 的 format(...) 是普通字符串函数；这里只禁止结果 FORMAT 子句。
            next_kind = (
                tokens[index + 1].token_type if index + 1 < len(tokens) else None
            )
            if next_kind != TokenType.L_PAREN:
                raise QueryPolicyError("禁止在 SQL 中指定 FORMAT")


def _validate_parameters(parameters: dict[str, Any] | None) -> dict[str, Any]:
    if parameters is None:
        return {}
    if not isinstance(parameters, dict):
        raise QueryPolicyError("parameters 必须是对象")
    if len(parameters) > MAX_PARAMETERS:
        raise QueryPolicyError(f"parameters 最多允许 {MAX_PARAMETERS} 项")
    for name in parameters:
        if not isinstance(name, str) or not _PARAMETER_NAME.fullmatch(name):
            raise QueryPolicyError(f"非法参数名: {name!r}")
    try:
        size = len(
            json.dumps(parameters, ensure_ascii=False, default=str).encode("utf-8")
        )
    except (TypeError, ValueError) as exc:
        raise QueryPolicyError(f"parameters 无法序列化: {exc}") from None
    if size > MAX_PARAMETER_BYTES:
        raise QueryPolicyError(f"parameters 超过 {MAX_PARAMETER_BYTES} 字节上限")
    return dict(parameters)


def _sqlglot_references(sql: str) -> tuple[TableReference, ...]:
    """提取常见 ClickHouse SQL 的表引用；新语法解析失败时交由原生 AST。"""
    if sql.lstrip().upper().startswith("EXPLAIN"):
        return ()
    try:
        statements = sqlglot.parse(sql, read="clickhouse")
    except ParseError:
        return ()
    if len(statements) != 1:
        raise QueryPolicyError("只允许一条 SQL 语句")
    statement = statements[0]
    if not isinstance(statement, exp.Query):
        # SQLGlot 把 ClickHouse EXPLAIN 暂存为 Command，最终由原生 AST 判定。
        if not (
            isinstance(statement, exp.Command)
            and sql.lstrip().upper().startswith("EXPLAIN")
        ):
            raise QueryPolicyError(
                f"只允许 SELECT 或 EXPLAIN SELECT，收到 {type(statement).__name__}"
            )
        return ()

    cte_names = {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}
    references: list[TableReference] = []
    for table in statement.find_all(exp.Table):
        if isinstance(table.this, exp.Func):
            continue
        name = table.name
        database = table.db
        catalog = table.catalog
        if catalog:
            raise QueryPolicyError(
                f"禁止 catalog 级表引用: {table.sql(dialect='clickhouse')}"
            )
        if database and database.lower() != DATABASE.lower():
            raise QueryPolicyError(
                f"只允许访问 {DATABASE} 数据库，收到 {database}.{name}"
            )
        if not database and name.lower() in cte_names:
            continue
        references.append(
            TableReference(
                name=name,
                database=database,
                final=isinstance(table.parent, exp.Final),
            )
        )
    return tuple(references)


def prepare_query(sql: str, parameters: dict[str, Any] | None = None) -> PreparedQuery:
    """完成无数据库访问的预检；完整语义随后由 ClickHouse 原生 AST 复核。"""
    normalized, tokens = _normalize_statement(sql)
    _validate_tokens(tokens)
    bound = _validate_parameters(parameters)
    return PreparedQuery(normalized, bound, _sqlglot_references(normalized))


def _parse_native_nodes(ast_text: str) -> list[_NativeNode]:
    nodes: list[_NativeNode] = []
    stack: list[int] = []
    for raw_line in ast_text.splitlines():
        if not raw_line.strip():
            continue
        line = raw_line.expandtabs(2).rstrip()
        indent = len(line) - len(line.lstrip(" "))
        while stack and nodes[stack[-1]].indent >= indent:
            stack.pop()
        parent = stack[-1] if stack else None
        nodes.append(_NativeNode(line.strip(), indent, parent))
        stack.append(len(nodes) - 1)
    if not nodes:
        raise QueryPolicyError("ClickHouse 未返回可解析的 AST")
    return nodes


def _has_ancestor(nodes: list[_NativeNode], index: int, prefix: str) -> bool:
    parent = nodes[index].parent
    while parent is not None:
        if nodes[parent].label.startswith(prefix):
            return True
        parent = nodes[parent].parent
    return False


def _unquote_identifier(value: str) -> str:
    return value.strip().strip("`")


def _validate_table_identifier(value: str) -> TableReference:
    parts = [_unquote_identifier(part) for part in value.split(".")]
    if len(parts) == 1:
        return TableReference(parts[0])
    if len(parts) == 2 and parts[0].lower() == DATABASE.lower():
        return TableReference(parts[1], parts[0])
    raise QueryPolicyError(f"只允许访问 {DATABASE} 数据库，收到 {value}")


def validate_native_ast(ast_text: str) -> NativeAstAnalysis:
    """校验 ClickHouse ``EXPLAIN AST`` 文本，覆盖 SQLGlot 尚不认识的新语法。"""
    nodes = _parse_native_nodes(ast_text)
    root = nodes[0].label
    if root.startswith("SelectWithUnionQuery"):
        pass
    elif root.startswith("Explain "):
        if not any(node.label.startswith("SelectWithUnionQuery") for node in nodes[1:]):
            raise QueryPolicyError("EXPLAIN 只能作用于 SELECT 查询")
    else:
        raise QueryPolicyError(
            f"只允许 SELECT 或 EXPLAIN SELECT，收到 {root.split(' ', 1)[0]}"
        )

    for node in nodes:
        if node.label.startswith(_FORBIDDEN_QUERY_NODES):
            raise QueryPolicyError(f"查询包含禁止语句: {node.label.split(' ', 1)[0]}")

    tables: list[TableReference] = []
    functions: list[str] = []
    table_functions: list[str] = []
    for index, node in enumerate(nodes):
        if node.label.startswith("TableIdentifier "):
            tables.append(
                _validate_table_identifier(node.label.removeprefix("TableIdentifier "))
            )
            continue
        if not node.label.startswith("Function "):
            continue
        name = node.label.removeprefix("Function ").split(" ", 1)[0]
        normalized = name.lower()
        functions.append(name)
        if _has_ancestor(nodes, index, "TableExpression"):
            table_functions.append(name)
            if normalized not in _ALLOWED_TABLE_FUNCTIONS:
                raise QueryPolicyError(f"禁止表函数 {name}；只允许 numbers 和 values")
        if (
            normalized in _FORBIDDEN_SCALAR_FUNCTIONS
            or normalized.startswith("dictget")
            or normalized.startswith("joinget")
        ):
            raise QueryPolicyError(f"禁止函数 {name}")

    return NativeAstAnalysis(
        root=root,
        tables=tuple(tables),
        table_functions=tuple(table_functions),
        functions=tuple(functions),
    )
