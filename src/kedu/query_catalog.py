"""kedu_query 的 jqdata 表目录与语义提示。"""

from __future__ import annotations

from collections.abc import Iterable

from .db import DATABASE
from .query_policy import TableReference

# 这些关系服务于同步、折叠或 A/B 校验。SQL 仍可显式查询，但默认目录不展示。
INTERNAL_TABLES = frozenset(
    {
        "balance_sheet_day_view",
        "cash_flow_statement_day_view",
        "concept_member_raw",
        "financial_indicator_day_view",
        "income_statement_day_view",
        "index_member_seg",
        "index_sync_state",
        "industry_member_raw",
        "margin_target_raw",
    }
)

# 仅列出当前读侧实现明确使用 FINAL 的表。其他 ReplacingMergeTree 不擅自加 FINAL。
FINAL_REQUIRED = frozenset(
    {
        "call_auction",
        "concept_history",
        "index_valuation",
        "index_weights",
        "locked_shares",
        "money_flow_pro",
        "stock_valuation",
    }
)


def semantic_notes(table: str) -> tuple[str, ...]:
    """返回与该物理表直接相关、会影响查询正确性的说明。"""
    name = table.lower()
    notes: list[str] = []
    if name in {"bar_1d", "bar_1m"}:
        notes.append(
            "价格列是原始价；前复权应使用该票全表最新 factor 作为动态锚，"
            "后复权为 raw * factor"
        )
        notes.append("paused=1 的价格是存储口径填充值；需要跳过停牌时显式过滤")
    if name in {
        "income_statement_day",
        "cash_flow_statement_day",
        "balance_sheet_day",
        "financial_indicator_day",
    }:
        notes.append("已物化 pubDate <= day 且 statDate 最大的 as-of 日截面")
    if name in {
        "income_statement",
        "cash_flow_statement",
        "financial_indicator",
    }:
        notes.append("报告期单季口径，以 (code, statDate) 为粒度")
    if name.endswith("_acc"):
        notes.append("报告期累计口径，以 (code, statDate) 为粒度")
    if name.startswith("stk_"):
        notes.append(
            "finance 原始表可能包含 report_type 等多版本字段，查询时应显式决定口径"
        )
    if name in {
        "concept_history",
        "industry_history",
        "index_member_history",
        "margin_target_history",
    }:
        notes.append(
            "成员有效区间右端包含：start_date <= day AND (end_date IS NULL OR end_date >= day)"
        )
    if name == "index_weights":
        notes.append("每个 weight_date 是一份权重快照；查询日最近快照需由 SQL 显式选择")
    if name in INTERNAL_TABLES:
        notes.append("内部同步、staging 或校验关系；默认目录中隐藏")
    return tuple(notes)


def query_warnings(references: Iterable[TableReference]) -> tuple[str, ...]:
    """根据表引用返回非阻断式语义警告，不修改用户 SQL。"""
    warnings: list[str] = []
    seen: set[str] = set()
    refs = list(references)
    for ref in refs:
        name = ref.name.lower()
        if name in FINAL_REQUIRED and not ref.final:
            message = f"jqdata.{name} 可能存在未合并版本；需要确定快照语义时使用 FINAL"
            if message not in seen:
                warnings.append(message)
                seen.add(message)
        if name in {"bar_1d", "bar_1m"}:
            message = f"jqdata.{name} 是原始行情，复权必须显式使用 factor 和全表最新锚"
            if message not in seen:
                warnings.append(message)
                seen.add(message)
        if name.startswith("stk_"):
            message = (
                f"jqdata.{name} 是 finance 原始表，请显式处理 report_type/报告版本"
            )
            if message not in seen:
                warnings.append(message)
                seen.add(message)
        if name in {
            "concept_history",
            "industry_history",
            "index_member_history",
            "margin_target_history",
        }:
            message = f"jqdata.{name} 的 end_date 为右端包含"
            if message not in seen:
                warnings.append(message)
                seen.add(message)
    return tuple(warnings)


def _table_rows(client) -> list[tuple]:
    return client.query(
        "SELECT name, engine, total_rows "
        "FROM system.tables WHERE database = {database:String} ORDER BY name",
        parameters={"database": DATABASE},
    ).result_rows


def render_catalog(client, include_internal: bool = False) -> str:
    """列出 jqdata 查询关系；默认隐藏同步与校验关系。"""
    rows = _table_rows(client)
    public = [row for row in rows if row[0] not in INTERNAL_TABLES]
    visible = rows if include_internal else public
    lines = [
        f"{DATABASE} 查询关系: public={len(public)} internal={len(rows) - len(public)} "
        f"shown={len(visible)}"
    ]
    for name, engine, total_rows in visible:
        tags = []
        if name in INTERNAL_TABLES:
            tags.append("internal")
        if name in FINAL_REQUIRED:
            tags.append("FINAL")
        suffix = f" [{' '.join(tags)}]" if tags else ""
        count = "?" if total_rows is None else str(total_rows)
        lines.append(f"  {name} engine={engine} rows={count}{suffix}")
    if not include_internal and len(rows) != len(public):
        lines.append("内部关系已隐藏；可用精确表名查询 kedu_describe(table=...)。")
    return "\n".join(lines)


def _normalize_table_name(value: str) -> str:
    name = value.strip().replace("`", "")
    if name.lower().startswith(f"{DATABASE.lower()}."):
        name = name.split(".", 1)[1]
    return name


def render_table_description(client, table: str) -> str:
    """返回物理表结构及 Kedu 语义元数据。"""
    name = _normalize_table_name(table)
    metadata = client.query(
        "SELECT engine, total_rows FROM system.tables "
        "WHERE database = {database:String} AND name = {table:String}",
        parameters={"database": DATABASE, "table": name},
    ).result_rows
    if not metadata:
        return f"未知 {DATABASE} 表 {table!r}；用 kedu_describe(table='*') 查看目录。"
    engine, total_rows = metadata[0]
    columns = client.query(
        "SELECT name, type, default_kind, default_expression, comment "
        "FROM system.columns WHERE database = {database:String} AND table = {table:String} "
        "ORDER BY position",
        parameters={"database": DATABASE, "table": name},
    ).result_rows
    tags = ["internal"] if name in INTERNAL_TABLES else ["public"]
    if name in FINAL_REQUIRED:
        tags.append("FINAL recommended")
    lines = [
        f"{DATABASE}.{name} engine={engine} rows={total_rows if total_rows is not None else '?'} "
        f"tags={','.join(tags)}",
        "columns:",
    ]
    for col_name, col_type, default_kind, default_expression, comment in columns:
        details = []
        if default_kind:
            details.append(f"{default_kind} {default_expression}".strip())
        if comment:
            details.append(str(comment))
        suffix = f" -- {'; '.join(details)}" if details else ""
        lines.append(f"  {col_name} {col_type}{suffix}")
    notes = semantic_notes(name)
    if notes:
        lines.append("semantics:")
        lines.extend(f"  - {note}" for note in notes)
    return "\n".join(lines)
