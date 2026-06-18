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
    # 股东户数/预计解禁/实际解禁/大股东增减持/股本变动/除权除息/股份冻结/前十大及十大流通股东
    "STK_COMPANY_INFO": "stk_company_info",
    "STK_LIST": "stk_list",
    "STK_NAME_HISTORY": "stk_name_history",
    "STK_EMPLOYEE_INFO": "stk_employee_info",
    "STK_HOLDER_NUM": "stk_holder_num",
    "STK_LIMITED_SHARES_LIST": "stk_limited_shares_list",
    "STK_LIMITED_SHARES_UNLIMIT": "stk_limited_shares_unlimit",
    "STK_SHAREHOLDERS_SHARE_CHANGE": "stk_shareholders_share_change",
    "STK_CAPITAL_CHANGE": "stk_capital_change",
    "STK_XR_XD": "stk_xr_xd",
    "STK_SHARES_FROZEN": "stk_shares_frozen",
    "STK_SHAREHOLDER_TOP10": "stk_shareholder_top10",
    "STK_SHAREHOLDER_FLOATING_TOP10": "stk_shareholder_floating_top10",
}

# 基金族(reference/基金):同样走 finance.run_query,与 STK 共用同步引擎与建表辅助。
# 这些是聚宽真实 finance 表,backfill 据 jqdatasdk 模型列类型建表(见 backfill_stk/backfill_fund)。
FUND_TABLES: dict[str, str] = {
    "FUND_MAIN_INFO": "fund_main_info",
    "FUND_NET_VALUE": "fund_net_value",
    "FUND_FIN_INDICATOR": "fund_fin_indicator",
    "FUND_PORTFOLIO": "fund_portfolio",
    "FUND_PORTFOLIO_BOND": "fund_portfolio_bond",
    "FUND_PORTFOLIO_STOCK": "fund_portfolio_stock",
    "FUND_INVEST_TARGET": "fund_invest_target",
    "FUND_DIVIDEND": "fund_dividend",
    "FUND_SHARE_DAILY": "fund_share_daily",
    "FUND_MF_DAILY_PROFIT": "fund_mf_daily_profit",
}

# finance.run_query 可见的全部逻辑表(STK + 基金)。命名避开 backfill_stk 内的局部
# FINANCE_TABLES(那是旧的 finance_income_statement/balance/cashflow 三张表常量)。
RUN_QUERY_TABLES: dict[str, str] = {**STK_TABLES, **FUND_TABLES}

# 基金同步表(去别名;基金无别名,即 FUND_TABLES 全集)。集中放此处,供 update_jqdata
# 与 backfill_fund 共用,依赖单向(脚本 → finance_schema),杜绝循环 import。
FUND_SYNC_TABLES: list[str] = list(FUND_TABLES)

# 场内基金细分类型:get_all_securities(['fund']) 返回的 type 取值,均有行情 bar
# (与股票/指数同进 bar_1d/bar_1m)。场外(.OF)基金不在此列。亦作伞型 'fund' 的展开集。
# 单一来源,供 securities.py(伞型展开)、update_jqdata.py、rebuild_from_jq.py(bar 范围)共用。
FUND_ONEXCHANGE_TYPES: tuple[str, ...] = ("etf", "lof", "mmf", "reits", "fja", "fjb", "fjm")

# 物理表排序键覆盖:默认 ORDER BY id(报告期表一码多行、靠 id 唯一)。基金逐日大表
# 常按 (code, 日期) 查,ORDER BY id 会全表扫 -> 改排序键(同时即 ReplacingMergeTree
# 去重键;这些表每 (code, 日期) 唯一,语义不变)。
ORDER_BY_OVERRIDE: dict[str, str] = {
    "fund_net_value": "(code, day)",
    "fund_share_daily": "(code, date)",
    "fund_mf_daily_profit": "(code, end_date)",
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


def new_table_ddl(ch_table: str, schema: list[tuple[str, str]],
                  order_by: str | None = None) -> str:
    """根据 schema 生成新表 DDL.

    order_by 缺省取 ORDER_BY_OVERRIDE 中该表的排序键, 再缺省回落 'id'
    (报告期表一码多行靠 id 唯一;基金逐日大表用 (code, 日期) 提升按码查询).
    """
    cols = ",\n  ".join(f"`{c}` {t}" for c, t in schema)
    ob = order_by or ORDER_BY_OVERRIDE.get(ch_table, "id")
    # 非 id 排序键(基金大表的 (code, 日期))列为 Nullable, 需开启 allow_nullable_key;
    # id 排序键(Int64 非空)无此问题, 保持 STK DDL 不变。
    settings = "" if ob == "id" else "\nSETTINGS allow_nullable_key = 1"
    return f"""CREATE TABLE IF NOT EXISTS {DATABASE}.{ch_table} (
  {cols},
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at)
ORDER BY {ob}{settings}"""
