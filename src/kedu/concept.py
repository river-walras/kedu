"""本地复刻聚宽概念板块 API:get_concepts / get_concept_stocks / get_concept。

数据源为 ClickHouse 两表(由 scripts/backfill_industry.py 同步):
- jqdata.concepts         概念列表
- jqdata.concept_history  概念成分区间(无历史 API,由逐交易日 get_concept_stocks 快照 diff 而来)

concept_history 日更靠改写 end_date 重插关区间,存在 ReplacingMergeTree 待合并版本,
故成分点查一律加 FINAL(表小,代价低,免受后台 merge 时机影响)。区间「某日活跃」语义:
start_date <= d AND (end_date IS NULL OR end_date >= d),end_date 含当日。
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import pandas as pd

from .db import DATABASE, get_client


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
    """北京时区今天(date=None 时的查询锚点)。"""
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()


def _q(s: object) -> str:
    """转义为 SQL 单引号字符串字面量。"""
    return "'" + str(s).replace("'", "\\'") + "'"


def _active(col_prefix: str, iso: str) -> str:
    """区间某日活跃谓词。"""
    return (f"{col_prefix}start_date <= '{iso}' "
            f"AND ({col_prefix}end_date IS NULL OR {col_prefix}end_date >= '{iso}')")


def get_concepts() -> pd.DataFrame:
    """获取概念板块列表,复刻 jqdatasdk.get_concepts。

    返回 DataFrame:index 为概念代码,列为 name(概念名称)与 start_date(概念起始日)。
    """
    cli = get_client()
    sql = (f"SELECT concept_code, concept_name, start_date "
           f"FROM {DATABASE}.concepts ORDER BY concept_code")
    rows = cli.query(sql).result_rows
    df = pd.DataFrame(rows, columns=["concept_code", "name", "start_date"]).set_index("concept_code")
    df.index = df.index.astype(object)
    df.index.name = None
    df["name"] = df["name"].astype(object)
    return df[["name", "start_date"]]


def get_concept_stocks(concept_code: str, date: str | dt.date | None = None) -> list[str]:
    """获取某概念板块在给定日期的成分股,复刻 jqdatasdk.get_concept_stocks。返回股票代码 list。"""
    cli = get_client()
    iso = (_to_date(date) or _today()).isoformat()
    sql = (f"SELECT DISTINCT stock FROM {DATABASE}.concept_history FINAL "
           f"WHERE concept_code = {_q(concept_code)} AND {_active('', iso)} "
           f"ORDER BY stock")
    return [r[0] for r in cli.query(sql).result_rows]


def get_concept(security: str | Sequence[str], date: str | dt.date | None = None) -> dict:
    """查询股票在给定日期所属的概念板块,复刻 jqdatasdk.get_concept。

    security 可为单代码或代码 list。返回 dict:
      {code: {'jq_concept': [{'concept_code':..., 'concept_name':...}, ...(按 concept_code 升序)]}}。
    """
    cli = get_client()
    secs = [security] if isinstance(security, str) else list(security)
    iso = (_to_date(date) or _today()).isoformat()
    quoted = ", ".join(_q(s) for s in secs)
    sql = (
        f"SELECT h.stock AS stock, h.concept_code AS concept_code, c.concept_name AS concept_name "
        f"FROM (SELECT concept_code, stock, start_date, end_date "
        f"      FROM {DATABASE}.concept_history FINAL) AS h "
        f"LEFT JOIN (SELECT concept_code, any(concept_name) AS concept_name "
        f"           FROM {DATABASE}.concepts GROUP BY concept_code) AS c "
        f"  ON c.concept_code = h.concept_code "
        f"WHERE h.stock IN ({quoted}) AND {_active('h.', iso)} "
        f"ORDER BY h.stock, h.concept_code"
    )
    rows = cli.query(sql).result_rows  # (stock, concept_code, concept_name)
    grouped: dict[str, list] = {s: [] for s in secs}
    for stock, ccode, cname in rows:
        grouped.setdefault(stock, []).append({"concept_code": ccode, "concept_name": cname})
    return {s: {"jq_concept": grouped.get(s, [])} for s in secs}
