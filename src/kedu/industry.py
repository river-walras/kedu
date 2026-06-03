"""本地复刻聚宽行业分类 API:get_industries / get_industry_stocks /
get_history_industry / get_industry。

数据源为 ClickHouse 两表(由 scripts/backfill_industry.py 同步):
- jqdata.industries        行业列表(全 6 taxonomy,带 [start_date, end_date] 有效区间)
- jqdata.industry_history  行业成分纳入/剔除区间(直接来自 jqdatasdk.get_history_industry)

成分点查(get_industry_stocks / get_industry)由 industry_history 的区间重建;
区间「某日活跃」语义:start_date <= d AND (end_date IS NULL OR end_date >= d),
end_date 含当日(对齐 get_history_industry:截至 2024-02-07、下一区间自 2024-02-08)。
两表均由「全量权威拉取 + TRUNCATE+reload」维护,无重复版本,读查询无需 FINAL。
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import numpy as np
import pandas as pd

from .db import DATABASE, get_client

# get_industry dict 输出的 taxonomy 顺序,对齐聚宽 live 返回。
_TAXO_ORDER = ["sw_l1", "sw_l2", "sw_l3", "zjw", "jq_l2", "jq_l1"]


def _to_date(x: str | dt.date | None) -> dt.date | None:
    """将日期类输入转换为 datetime.date。"""
    if x is None:
        return None
    if isinstance(x, dt.datetime):
        return x.date()
    if isinstance(x, dt.date):
        return x
    return pd.Timestamp(x).date()


def _today() -> dt.date:
    """北京时区今天(date=None 时的查询锚点,与聚宽 today 语义一致)。"""
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()


def _q(s: object) -> str:
    """转义为 SQL 单引号字符串字面量。"""
    return "'" + str(s).replace("'", "\\'") + "'"


def _active(col_prefix: str, iso: str) -> str:
    """区间某日活跃谓词。"""
    return (f"{col_prefix}start_date <= '{iso}' "
            f"AND ({col_prefix}end_date IS NULL OR {col_prefix}end_date >= '{iso}')")


def get_industries(name: str = "zjw", date: str | dt.date | None = None) -> pd.DataFrame:
    """获取行业列表,复刻 jqdatasdk.get_industries。

    name 取 sw_l1/sw_l2/sw_l3/jq_l1/jq_l2/zjw。返回 DataFrame:index 为行业代码,
    列为 name(行业名称)与 start_date(行业起始日)。date 默认今天,返回该日有效的行业。
    """
    cli = get_client()
    iso = (_to_date(date) or _today()).isoformat()
    sql = (f"SELECT industry_code, industry_name, start_date "
           f"FROM {DATABASE}.industries "
           f"WHERE name = {_q(name)} AND {_active('', iso)} "
           f"ORDER BY industry_code")
    rows = cli.query(sql).result_rows
    df = pd.DataFrame(rows, columns=["industry_code", "name", "start_date"]).set_index("industry_code")
    df.index = df.index.astype(object)
    df.index.name = None
    df["name"] = df["name"].astype(object)
    return df[["name", "start_date"]]


def get_industry_stocks(industry_code: str, date: str | dt.date | None = None) -> list[str]:
    """获取某行业在给定日期的成分股,复刻 jqdatasdk.get_industry_stocks。返回股票代码 list。

    industry_code 在各 taxonomy 间不冲突(申万数字 / 证监会字母 / 聚宽 HY 前缀),无需再给 name。
    """
    cli = get_client()
    iso = (_to_date(date) or _today()).isoformat()
    sql = (f"SELECT DISTINCT stock FROM {DATABASE}.industry_history "
           f"WHERE industry_code = {_q(industry_code)} AND {_active('', iso)} "
           f"ORDER BY stock")
    return [r[0] for r in cli.query(sql).result_rows]


def get_history_industry(name: str,
                         securities: str | Sequence[str] | None = None) -> pd.DataFrame:
    """获取某行业体系的所有历史成分股纳入/剔除记录,复刻 jqdatasdk.get_history_industry。

    返回 DataFrame,列为 code(行业代码)/start_date/end_date/stock,未被剔除时 end_date 为 NaN。
    securities 给定时仅返回这些标的的记录(可为单代码 str 或代码 list),默认全部。
    """
    cli = get_client()
    where = [f"name = {_q(name)}"]
    if securities is not None:
        secs = [securities] if isinstance(securities, str) else list(securities)
        where.append(f"stock IN ({', '.join(_q(s) for s in secs)})")
    sql = (f"SELECT industry_code AS code, start_date, end_date, stock "
           f"FROM {DATABASE}.industry_history WHERE {' AND '.join(where)} "
           f"ORDER BY industry_code, stock, start_date")
    rows = cli.query(sql).result_rows
    df = pd.DataFrame(rows, columns=["code", "start_date", "end_date", "stock"])
    # 开区间 end_date 为 None -> NaN,对齐聚宽展示。
    if not df.empty:
        df["end_date"] = df["end_date"].where(df["end_date"].notna(), np.nan)
    return df


def get_industry(security: str | Sequence[str], date: str | dt.date | None = None,
                 df: bool = False) -> dict | pd.DataFrame:
    """查询股票在给定日期所属的各体系行业,复刻 jqdatasdk.get_industry。

    security 可为单代码或代码 list。df=False(默认)返回 dict:
      {code: {taxonomy: {'industry_code':..., 'industry_name':...}}};
    df=True 返回长表,列为 code/type/industry_code/industry_name。
    """
    cli = get_client()
    secs = [security] if isinstance(security, str) else list(security)
    iso = (_to_date(date) or _today()).isoformat()
    quoted = ", ".join(_q(s) for s in secs)
    # 行业名取自 industries(按 (name, industry_code) 去重;同体系内代码不跨标准复用,名稳定)。
    sql = (
        f"SELECT h.stock AS stock, h.name AS type, h.industry_code AS industry_code, "
        f"i.industry_name AS industry_name "
        f"FROM {DATABASE}.industry_history AS h "
        f"LEFT JOIN ("
        f"  SELECT name, industry_code, any(industry_name) AS industry_name "
        f"  FROM {DATABASE}.industries GROUP BY name, industry_code) AS i "
        f"  ON i.name = h.name AND i.industry_code = h.industry_code "
        f"WHERE h.stock IN ({quoted}) AND {_active('h.', iso)}"
    )
    rows = cli.query(sql).result_rows  # (stock, type, industry_code, industry_name)

    if df:
        out = pd.DataFrame(rows, columns=["code", "type", "industry_code", "industry_name"])
        if not out.empty:
            order = {t: i for i, t in enumerate(_TAXO_ORDER)}
            out = (out.assign(_o=out["type"].map(lambda t: order.get(t, len(order))))
                   .sort_values(["code", "_o"], kind="stable")
                   .drop(columns="_o").reset_index(drop=True))
        return out[["code", "type", "industry_code", "industry_name"]]

    grouped: dict[str, dict] = {s: {} for s in secs}
    for stock, taxo, icode, iname in rows:
        grouped.setdefault(stock, {})[taxo] = {"industry_code": icode, "industry_name": iname}
    result: dict[str, dict] = {}
    for s in secs:
        inner = grouped.get(s, {})
        ordered = {t: inner[t] for t in _TAXO_ORDER if t in inner}
        for t in inner:  # 体系顺序之外的兜底(正常不触发)
            ordered.setdefault(t, inner[t])
        result[s] = ordered
    return result
