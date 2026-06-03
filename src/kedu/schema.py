"""ClickHouse 表与视图 DDL.

基本面镜像表的列由 vendored 查询模型(_jqsdk)程序化生成, 列名与语义与聚宽逻辑表完全一致,
以便 get_fundamentals_sql 产出的 SQL 经 sqlglot 转译后可直接在 ClickHouse 执行.

命名约定:
- 行情和主表用 `instrument_id`, 对齐 factors 表.
- 基本面镜像表用 `code` / `day` / `statDate` / `pubDate`, 必须与 vendored 模型 SQL 列名一致.
"""
from __future__ import annotations

from ._jqsdk import balance, cash_flow, income, indicator, valuation

from .db import DATABASE

META = {"id", "code", "day", "pubDate", "statDate"}

# 逻辑表 -> (jqdatasdk 模型, 数据列 ClickHouse 类型)
_DECIMAL = "Nullable(Decimal(20, 4))"
_FLOAT = "Nullable(Float64)"


def data_columns(model) -> list[str]:
    """返回模型中去掉 meta 列后的数据列."""
    return [c.key for c in model.__table__.columns if c.key not in META]


# 单季/累计/快照(statDate 键)基本面镜像表
STATDATE_TABLES = {
    "income_statement": (income, _DECIMAL),
    "income_statement_acc": (income, _DECIMAL),
    "cash_flow_statement": (cash_flow, _DECIMAL),
    "cash_flow_statement_acc": (cash_flow, _DECIMAL),
    "balance_sheet": (balance, _DECIMAL),
    "financial_indicator": (indicator, _FLOAT),
    "financial_indicator_acc": (indicator, _FLOAT),
}

# date 模式 ASOF 视图: 视图名 -> 底层 statDate 表
DAY_VIEWS = {
    "income_statement_day": ("income_statement", income),
    "cash_flow_statement_day": ("cash_flow_statement", cash_flow),
    "financial_indicator_day": ("financial_indicator", indicator),
    "balance_sheet_day": ("balance_sheet", balance),
}


def statdate_table_ddl(name: str) -> str:
    """生成 statDate 键基本面镜像表 DDL."""
    model, col_type = STATDATE_TABLES[name]
    cols = ",\n  ".join(f"`{c}` {col_type}" for c in data_columns(model))
    return f"""CREATE TABLE IF NOT EXISTS {DATABASE}.{name} (
  id Int64,
  code String,
  statDate Date,
  pubDate Date,
  {cols},
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at)
ORDER BY (code, statDate)"""


def stock_valuation_ddl() -> str:
    """生成 stock_valuation 表 DDL."""
    cols = ",\n  ".join(f"`{c}` {_FLOAT}" for c in data_columns(valuation))
    return f"""CREATE TABLE IF NOT EXISTS {DATABASE}.stock_valuation (
  id Int64,
  code String,
  day Date,
  {cols},
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at)
ORDER BY (code, day)"""


def day_view_ddl(name: str) -> str:
    """生成 date 模式基本面视图 DDL.

    对每个 code 与 day, 取 pubDate<=day 的报告中 statDate 最大者作为最近报告期.
    不能用 ASOF JOIN, 它只按 pubDate 取最近一条. 年报与一季报常同日披露,
    ASOF 在并列时任取一条会错选去年年报, 聚宽返回 statDate 更大的当期报告.

    两段式以保证谓词下推与执行效率. picked 只带 key, day 与 code,
    GROUP BY code 与 day 后取 max(statDate), 再按 code 与 statDate 等值 JOIN 回基表.
    """
    base, model = DAY_VIEWS[name]
    proj = ",\n  ".join(f"b.`{c}` AS `{c}`" for c in data_columns(model))
    return f"""CREATE OR REPLACE VIEW {DATABASE}.{name} AS
WITH picked AS (
  SELECT sv.code AS code, sv.day AS day, max(b.statDate) AS statDate
  FROM {DATABASE}.stock_valuation AS sv
  LEFT JOIN {DATABASE}.{base} AS b
    ON sv.code = b.code AND b.pubDate <= sv.day
  GROUP BY sv.code, sv.day
)
SELECT b.id AS id, p.code AS code, p.day AS day, b.pubDate AS pubDate, p.statDate AS statDate,
  {proj}
FROM picked AS p
LEFT JOIN {DATABASE}.{base} AS b
  ON p.code = b.code AND p.statDate = b.statDate"""


# ---- 行情 / 主表 DDL(聚宽 get_price / get_all_securities / get_trade_days)----
MARKET_DDL = {
    "trade_days": f"""CREATE TABLE IF NOT EXISTS {DATABASE}.trade_days (
  day Date,
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at) ORDER BY day""",
    "bar_1d": f"""CREATE TABLE IF NOT EXISTS {DATABASE}.bar_1d (
  instrument_id String, date Date,
  open Nullable(Float64), close Nullable(Float64), high Nullable(Float64), low Nullable(Float64),
  pre_close Nullable(Float64), high_limit Nullable(Float64), low_limit Nullable(Float64),
  volume Nullable(Float64), money Nullable(Float64), avg Nullable(Float64),
  factor Nullable(Float64), paused UInt8, is_st UInt8,
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at)
PARTITION BY toYYYYMM(date)
ORDER BY (instrument_id, date)""",
    "bar_1m": f"""CREATE TABLE IF NOT EXISTS {DATABASE}.bar_1m (
  instrument_id String, datetime DateTime,
  open Nullable(Float64), close Nullable(Float64), high Nullable(Float64), low Nullable(Float64),
  pre_close Nullable(Float64), high_limit Nullable(Float64), low_limit Nullable(Float64),
  volume Nullable(Float64), money Nullable(Float64), avg Nullable(Float64),
  factor Nullable(Float64), paused UInt8,
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at)
PARTITION BY toYYYYMM(datetime)
ORDER BY (instrument_id, datetime)""",
    "is_st": f"""CREATE TABLE IF NOT EXISTS {DATABASE}.is_st (
  instrument_id String, date Date, is_st UInt8,
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at)
ORDER BY (instrument_id, date)""",
    "securities": f"""CREATE TABLE IF NOT EXISTS {DATABASE}.securities (
  instrument_id String, display_name Nullable(String), name Nullable(String),
  start_date Nullable(Date32), end_date Nullable(Date32),
  type Nullable(String), exchange Nullable(String), board_type Nullable(String),
  industry_code Nullable(String), sector_code Nullable(String),
  round_lot Nullable(Float64), status Nullable(String),
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at) ORDER BY instrument_id""",
}


# ---- 行业 / 概念分类 DDL(聚宽 get_industries / get_industry_stocks /
#      get_history_industry / get_industry / get_concepts / get_concept_stocks / get_concept)----
#
# 维护方式决定读侧是否需要 FINAL:
# - industries / industry_history / concepts 由「全量权威拉取 + TRUNCATE+reload」维护,
#   无重复版本,读查询无需 FINAL。
# - concept_history 无历史 API,由逐日快照 diff 而来,日更靠改写 end_date 重插关区间,
#   存在待合并版本,读查询一律加 FINAL(表小,代价低)。
# 区间「某日活跃」语义:start_date <= d AND (end_date IS NULL OR end_date >= d),
# end_date 含当日(对齐 get_history_industry 示例:截至 2024-02-07、下一区间自 2024-02-08)。
CLASSIFY_DDL = {
    "industries": f"""CREATE TABLE IF NOT EXISTS {DATABASE}.industries (
  name String, industry_code String, industry_name String,
  start_date Date, end_date Nullable(Date),
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at)
ORDER BY (name, industry_code, start_date)""",
    "industry_history": f"""CREATE TABLE IF NOT EXISTS {DATABASE}.industry_history (
  name String, industry_code String, stock String,
  start_date Date, end_date Nullable(Date),
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at)
ORDER BY (name, industry_code, stock, start_date)""",
    "concepts": f"""CREATE TABLE IF NOT EXISTS {DATABASE}.concepts (
  concept_code String, concept_name String, start_date Date,
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at)
ORDER BY concept_code""",
    "concept_history": f"""CREATE TABLE IF NOT EXISTS {DATABASE}.concept_history (
  concept_code String, stock String,
  start_date Date, end_date Nullable(Date),
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at)
ORDER BY (concept_code, stock, start_date)""",
}


# ---- 指数 DDL(聚宽 get_index_stocks / get_index_weights / get_index_valuation)----
#
# 维护方式决定读侧是否需要 FINAL:
# - index_member_history 由 staging(index_member_seg)折叠后 TRUNCATE+reload,无重复版本,
#   读查询无需 FINAL。禁止日更直接插同 key 闭区间(合并前新旧并存)。
# - index_weights / index_valuation 逐月/逐日增量重插,存在待合并版本,读查询加 FINAL(表小)。
# 指数列表沿用 securities 表(type='index'),不另建表。
# position 保留聚宽成分股/权重的原始返回序,读侧 ORDER BY position, code。
# index_sync_state 记录每个 (dataset, index_code) 已扫描到的交易日 covered_until,
#   解决「多年不调仓无法判断扫完」与「空月/空日反复重拉」。dataset ∈ {member, weight, valuation}。
_INDEX_VAL_COLS = [
    "pe_ratio", "turnover_ratio", "pb_ratio", "ps_ratio", "pcf_ratio", "capitalization",
    "market_cap", "circulating_cap", "circulating_market_cap", "pe_ratio_lyr", "pcf_ratio2",
    "dividend_ratio", "free_cap", "free_market_cap", "a_cap", "a_market_cap",
]
_INDEX_VAL_COLS_DDL = ",\n  ".join(f"`{c}` {_FLOAT}" for c in _INDEX_VAL_COLS)

INDEX_DDL = {
    "index_member_history": f"""CREATE TABLE IF NOT EXISTS {DATABASE}.index_member_history (
  index_code String, stock String, position UInt32,
  start_date Date, end_date Nullable(Date),
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at)
ORDER BY (index_code, stock, start_date)""",
    "index_weights": f"""CREATE TABLE IF NOT EXISTS {DATABASE}.index_weights (
  index_code String, code String, position UInt32,
  weight Nullable(Float64), display_name Nullable(String), weight_date Date,
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at)
ORDER BY (index_code, weight_date, code)""",
    "index_valuation": f"""CREATE TABLE IF NOT EXISTS {DATABASE}.index_valuation (
  code String, day Date,
  {_INDEX_VAL_COLS_DDL},
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at)
ORDER BY (code, day)""",
    "index_sync_state": f"""CREATE TABLE IF NOT EXISTS {DATABASE}.index_sync_state (
  dataset String, index_code String, covered_until Date,
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at)
ORDER BY (dataset, index_code)""",
}


def all_table_ddls() -> dict[str, str]:
    """返回全部基础表 DDL."""
    ddls: dict[str, str] = {}
    for name in STATDATE_TABLES:
        ddls[name] = statdate_table_ddl(name)
    ddls["stock_valuation"] = stock_valuation_ddl()
    ddls.update(MARKET_DDL)
    ddls.update(CLASSIFY_DDL)
    ddls.update(INDEX_DDL)
    return ddls


def create_all(client, include_views: bool = True, verbose: bool = True) -> None:
    """创建全部 ClickHouse 表与可选视图."""
    for name, ddl in all_table_ddls().items():
        if verbose:
            print(f"creating table {DATABASE}.{name}")
        client.command(ddl)
    if include_views:
        for name in DAY_VIEWS:
            if verbose:
                print(f"creating view {DATABASE}.{name}")
            client.command(day_view_ddl(name))


if __name__ == "__main__":
    from .db import auth_from_env, get_client

    auth_from_env()
    create_all(get_client())
