"""STK 报告期表的注册表与建表辅助.

服务于 finance.run_query 接口. 聚宽 `finance.STK_*` 模型需要 auth 才能在本地构建,
故本地自建 SQLAlchemy 模型, 见 finance_models.py, 底层落 ClickHouse `stk_*` 表.
income/balance/cashflow + 业绩预告/审计意见/预约披露/状态变动, 由 backfill_stk.py
据聚宽模型列类型建表, 不手写列.
"""
from __future__ import annotations

import sqlalchemy as sa

from .db import DATABASE

# 聚宽逻辑表名 -> ClickHouse 物理表名(STK_CASHFLOW_STATEMENT 为聚宽真实名,
# STK_CASH_FLOW_STATEMENT 为用户惯用别名,二者映射同一张 CH 表)
STK_TABLES: dict[str, str] = {
    "STK_INCOME_STATEMENT": "stk_income_statement",
    "STK_BALANCE_SHEET": "stk_balance_sheet",
    "STK_CASHFLOW_STATEMENT": "stk_cashflow_statement",
    "STK_CASH_FLOW_STATEMENT": "stk_cashflow_statement",
    "STK_FIN_FORCAST": "stk_fin_forcast",
    "STK_AUDIT_OPINION": "stk_audit_opinion",
    "STK_REPORT_DISCLOSURE": "stk_report_disclosure",
    "STK_STATUS_CHANGE": "stk_status_change",
    # 市场每日成交概况 / 融资融券汇总(均以 date 为键,无 pub_date/end_date)
    "STK_EXCHANGE_TRADE_INFO": "stk_exchange_trade_info",
    "STK_MT_TOTAL": "stk_mt_total",
    # 上市公司基本信息族(reference/上市公司基本信息):基本信息/上市信息/简称变更/员工/
    # 股东户数/预计解禁/实际解禁/大股东增减持/股本变动/股份冻结/前十大及十大流通股东
    "STK_COMPANY_INFO": "stk_company_info",
    "STK_LIST": "stk_list",
    "STK_NAME_HISTORY": "stk_name_history",
    "STK_EMPLOYEE_INFO": "stk_employee_info",
    "STK_HOLDER_NUM": "stk_holder_num",
    "STK_LIMITED_SHARES_LIST": "stk_limited_shares_list",
    "STK_LIMITED_SHARES_UNLIMIT": "stk_limited_shares_unlimit",
    "STK_SHAREHOLDERS_SHARE_CHANGE": "stk_shareholders_share_change",
    "STK_CAPITAL_CHANGE": "stk_capital_change",
    "STK_SHARES_FROZEN": "stk_shares_frozen",
    "STK_SHAREHOLDER_TOP10": "stk_shareholder_top10",
    "STK_SHAREHOLDER_FLOATING_TOP10": "stk_shareholder_floating_top10",
}


def _ch_from_sa(sa_type, name: str) -> str:
    """将 SQLAlchemy 列类型映射为 ClickHouse 类型.

    类型来自模型定义, 不受全空采样影响. 数值类型统一映射为 Float64,
    聚宽 run_query 本就返回 float. Float 是 Numeric 子类, 需先判断.
    """
    if name == "id":
        return "Int64"
    if isinstance(sa_type, sa.Integer):
        return "Nullable(Int64)"
    if isinstance(sa_type, sa.Float):
        return "Nullable(Float64)"
    if isinstance(sa_type, sa.Numeric):  # DECIMAL
        return "Nullable(Float64)"
    # Date32(1900-2299)而非 Date(1970-2149):成立日期等可早于 1970,Date 会溢出
    # (UInt16 天数上限 65535)。build_model 仍将 Date32 还原为 SQLAlchemy Date,parity 不变。
    if isinstance(sa_type, sa.DateTime):
        return "Nullable(Date32)"
    if isinstance(sa_type, sa.Date):
        return "Nullable(Date32)"
    return "Nullable(String)"


def schema_from_model(model) -> list[tuple[str, str]]:
    """由 jqdatasdk 模型列类型生成 ClickHouse schema.

    返回元素为列名与 ClickHouse 类型, 列序等于模型定义顺序与聚宽返回顺序.
    """
    return [(c.name, _ch_from_sa(c.type, c.name)) for c in model.__table__.columns]


def new_table_ddl(ch_table: str, schema: list[tuple[str, str]]) -> str:
    """根据 schema 生成新表 DDL."""
    cols = ",\n  ".join(f"`{c}` {t}" for c, t in schema)
    return f"""CREATE TABLE IF NOT EXISTS {DATABASE}.{ch_table} (
  {cols},
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at)
ORDER BY id"""
