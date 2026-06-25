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


def _day_select_body(name: str, where: str | None = None) -> str:
    """生成 date 模式基本面 as-of 的 SELECT 主体(视图与物化表共用,口径同源)。

    对每个 code 与 day, 取 pubDate<=day 的报告中 statDate 最大者作为最近报告期.
    不能用 ASOF JOIN, 它只按 pubDate 取最近一条. 年报与一季报常同日披露,
    ASOF 在并列时任取一条会错选去年年报, 聚宽返回 statDate 更大的当期报告.

    单段 GROUP-BY argMax 实现: stock_valuation 与基表按 code 等值 + ``pubDate<=day``
    内连接后, 按 (code, day) 分组, 用 ``argMax(col, statDate)`` 直接取 statDate 最大
    那一条报告的各列值。较早先「picked 取 max(statDate) 再等值 JOIN 回基表」两段式
    少一次 JOIN, q1(全市场 28 列 5 表)由 ~585ms 降到 ~420ms, 结果逐字节一致。

    NULL 保真: ClickHouse ``argMax(arg, val)`` 会跳过 ``arg`` 为 NULL 的行, 若最新报告
    某列为 NULL 会误取上一期非空值。故用 ``argMax(tuple(col), statDate).1`` —— tuple 整体
    永不为 NULL, argMax 选中 statDate 最大那行后再 ``.1`` 取回(可能为 NULL 的)原值,
    与两段式 JOIN 回基表的 NULL 行为完全一致(已对多日含季报披露日逐字段校验)。

    INNER JOIN(非 LEFT): 聚宽 get_fundamentals 对所引用的报告期表做内连接,
    某交易日尚无任一可见报告(pubDate<=day)的标的不返回。早先用 LEFT JOIN + 估值表脊柱,
    会为这类标的产出"估值列非空、报告列全空"的行,聚宽不返回(实测 get_fundamentals 较
    jqdatasdk 多出该类标的),从而抬高样本数、改变下游打分排名。INNER 连接将其自然剔除,
    成员资格与 jqdatasdk 逐一致。

    ``where`` 用于物化时按 day 区间分片(放在 GROUP BY 之前的 WHERE, 作用于 ``sv.day``)。
    因 GROUP BY 是 per (code, day)、join 条件 ``b.pubDate<=sv.day``, 按 day 切片不改变
    任一 (code, day) 单元的取值, 故分区物化与整表现算逐字节一致。
    """
    base, model = DAY_VIEWS[name]
    picks = [
        "argMax(tuple(b.id), b.statDate).1 AS id",
        "argMax(tuple(b.pubDate), b.statDate).1 AS pubDate",
        "max(b.statDate) AS statDate",
    ]
    # 数据列统一转 Float64。income/cash_flow/balance 报表列为 Decimal(20,4),
    # query_arrow→pandas 会物化成 Python Decimal object 列(arrow 物化慢, 且
    # postprocess 需逐元素 to_numeric 解析)。在视图里 toFloat64 直接产出 Float64,
    # arrow→pandas 走快路径, postprocess 也省去 to_numeric。toFloat64(Decimal) 与
    # Python float(Decimal) 经全表 250 万非空值校验逐位一致, 故逐字节不变;
    # financial_indicator 本就是 Float64, toFloat64 为 no-op。NULL 保真: 对 Nullable
    # 入参 toFloat64 传播 NULL, 仍配合 tuple-argMax 在并列报告期取回原值(含 NULL)。
    picks += [
        f"toFloat64(argMax(tuple(b.`{c}`), b.statDate).1) AS `{c}`"
        for c in data_columns(model)
    ]
    cols = ",\n  ".join(picks)
    where_clause = f"\nWHERE {where}" if where else ""
    return f"""SELECT sv.code AS code, sv.day AS day,
  {cols}
FROM {DATABASE}.stock_valuation AS sv
INNER JOIN {DATABASE}.{base} AS b
  ON sv.code = b.code AND b.pubDate <= sv.day{where_clause}
GROUP BY sv.code, sv.day"""


def day_view_ddl(name: str) -> str:
    """生成 date 模式基本面 as-of 视图 DDL。

    视图名为 ``{name}_view``(如 income_statement_day_view), 仅供 A/B 校验与回退;
    查询热点路径使用的同名 drop-in 关系由 day_materialize.full_build 物化成表。
    """
    return f"CREATE OR REPLACE VIEW {DATABASE}.{name}_view AS\n{_day_select_body(name)}"


def day_table_ddl(name: str) -> str:
    """生成 date 模式基本面 as-of 物化表 DDL(与 *_day 视图同名, 透明 drop-in)。

    列与 _day_select_body 产出严格对齐: 数据列统一 Nullable(Float64)(视图 toFloat64 口径)。
    按月分区 + ORDER BY (day, code): q1 谓词 ``day = X AND code IN (...)`` 走分区裁剪 + 主键定位;
    分区粒度也是日更 REPLACE PARTITION 的原子单元。
    """
    _, model = DAY_VIEWS[name]
    cols = ",\n  ".join(f"`{c}` {_FLOAT}" for c in data_columns(model))
    return f"""CREATE TABLE IF NOT EXISTS {DATABASE}.{name} (
  code String,
  day Date,
  id Int64,
  pubDate Date,
  statDate Date,
  {cols},
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at)
PARTITION BY toYYYYMM(day)
ORDER BY (day, code)"""


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
    # get_locked_shares 派生数据集(聚宽独立维护,非由 STK_* 表重算):day/num/rate1/rate2,
    # 每 (code, day) 唯一(聚宽已按解禁日聚合)。由 backfill_locked_shares.py 自 live 全量灌入。
    "locked_shares": f"""CREATE TABLE IF NOT EXISTS {DATABASE}.locked_shares (
  code String, day Date,
  num Nullable(Float64), rate1 Nullable(Float64), rate2 Nullable(Float64),
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at)
ORDER BY (code, day)""",
    # get_money_flow_pro 日频资金流向。data_type ∈ {'money','volume','deal'},长表存储,
    # 避免三种统计口径互相覆盖。分钟资金流向为聚宽付费模块,本地接口 fail-fast。
    "money_flow_pro": f"""CREATE TABLE IF NOT EXISTS {DATABASE}.money_flow_pro (
  time DateTime,
  code String,
  data_type String,
  inflow_xl Nullable(Float64),
  inflow_l Nullable(Float64),
  inflow_m Nullable(Float64),
  inflow_s Nullable(Float64),
  outflow_xl Nullable(Float64),
  outflow_l Nullable(Float64),
  outflow_m Nullable(Float64),
  outflow_s Nullable(Float64),
  netflow_xl Nullable(Float64),
  netflow_l Nullable(Float64),
  netflow_m Nullable(Float64),
  netflow_s Nullable(Float64),
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at)
PARTITION BY toYYYYMM(time)
ORDER BY (time, code, data_type)""",
    # get_billboard_list 龙虎榜。普通 MergeTree 保留 live 可能出现的重复行与原始行序;
    # 回补/日更按 day DELETE + INSERT 覆盖盘后 20:00/22:00 修正。
    "billboard": f"""CREATE TABLE IF NOT EXISTS {DATABASE}.billboard (
  code String,
  day Date,
  direction Nullable(String),
  rank Int64,
  abnormal_code Int64,
  abnormal_name Nullable(String),
  sales_depart_name Nullable(String),
  buy_value Nullable(Float64),
  buy_rate Nullable(Float64),
  sell_value Nullable(Float64),
  sell_rate Nullable(Float64),
  total_value Nullable(Float64),
  net_value Nullable(Float64),
  amount Nullable(Float64),
  _position UInt32,
  _ingested_at DateTime DEFAULT now()
) ENGINE = MergeTree
PARTITION BY toYYYYMM(day)
ORDER BY (day, code)""",
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


# ---- 融资融券 DDL(聚宽 get_mtss / get_margincash_stocks / get_marginsec_stocks)----
#
# - mtss            逐股融资融券明细,按 (sec_code, date) 唯一,ReplacingMergeTree。
# - margin_target_history  融资/融券标的列表折叠区间(type='cash'/'sec'),由
#   scripts/backfill_margin.py 从 staging margin_target_raw(逐日快照)gaps-and-islands
#   折叠后 TRUNCATE+reload,无重复版本 → 读侧不加 FINAL。区间「某日活跃」语义同 index/industry:
#   start_date <= d AND (end_date IS NULL OR end_date >= d)。
#   get_*_stocks(date=None) 的「最近一次披露」锚点取 staging margin_target_raw 的 max(date)
#   (开区间不记录覆盖到哪天),故 staging 须保留。
MARGIN_DDL = {
    "mtss": f"""CREATE TABLE IF NOT EXISTS {DATABASE}.mtss (
  sec_code String, date Date,
  fin_value Nullable(Float64), fin_buy_value Nullable(Float64), fin_refund_value Nullable(Float64),
  sec_value Nullable(Float64), sec_sell_value Nullable(Float64), sec_refund_value Nullable(Float64),
  fin_sec_value Nullable(Float64),
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at)
ORDER BY (sec_code, date)""",
    "margin_target_history": f"""CREATE TABLE IF NOT EXISTS {DATABASE}.margin_target_history (
  type String, stock String,
  start_date Date, end_date Nullable(Date),
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at)
ORDER BY (type, stock, start_date)""",
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
    ddls.update(MARGIN_DDL)
    return ddls


def create_all(client, include_views: bool = True, verbose: bool = True) -> None:
    """创建全部 ClickHouse base 表与可选 *_day_view 校验视图。

    注意: **不创建** date 模式查询用的同名 *_day drop-in 关系 —— 它们由
    day_materialize.full_build 显式物化成表。include_views=True 仅创建用于 A/B 校验
    与回退的 *_day_view 视图。
    """
    for name, ddl in all_table_ddls().items():
        if verbose:
            print(f"creating table {DATABASE}.{name}")
        client.command(ddl)
    if include_views:
        for name in DAY_VIEWS:
            if verbose:
                print(f"creating view {DATABASE}.{name}_view")
            client.command(day_view_ddl(name))


if __name__ == "__main__":
    from .db import auth_from_env, get_client

    auth_from_env()
    create_all(get_client())
