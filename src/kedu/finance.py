"""本地复刻聚宽 `finance.run_query` / `finance.run_offset_query` / `get_table_info`.

查询 `stk_*` 原始报告期表, 含 report_type=0 本期 / 1 非本期多版本.
`query(finance.STK_INCOME_STATEMENT).filter(...)` -> vendored compile_query(kedu._jqsdk) 产出
MySQL SQL -> sqlglot 转译 ClickHouse -> 执行 -> 去表前缀, Decimal 转 float, 对齐聚宽返回.

不能连表查询, 聚宽同款限制, 本地未强制. run_query 默认上限 5000 行,
run_offset_query 分页 10000x20, 即 20 万行.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pandas as pd
from sqlalchemy import text

from ._jqsdk import SqlQuery, compile_query
from .db import get_client, query_df
from .finance_models import build_model
from .finance_schema import RUN_QUERY_TABLES
from .fundamentals import _strip_table_prefix, _to_clickhouse_sql

def _convert_series(s: pd.Series) -> pd.Series:
    """对齐聚宽 run_query 的列表示.

    数值与 Decimal 转为 float64. 整数无空值时转 int64, 有空值时保留 Int64.
    日期转 datetime.date 对象, 缺失为 None. 字符串转 object, 缺失为 None.
    与现网 jqdatasdk.finance.run_query 逐项一致.
    """
    dt = s.dtype
    if pd.api.types.is_bool_dtype(dt):
        return s
    if pd.api.types.is_integer_dtype(dt):
        return s.astype("int64") if not s.isna().any() else s
    if pd.api.types.is_float_dtype(dt):
        return s.astype("float64")
    if pd.api.types.is_datetime64_any_dtype(dt):
        out = s.dt.date
        return out.where(s.notna(), None)
    if isinstance(dt, pd.StringDtype):
        return s.astype(object).where(s.notna(), None)
    # object:Decimal→float64;date 原样;其余字符串 NA→None
    nonnull = s.dropna()
    if len(nonnull):
        v = nonnull.iloc[0]
        if isinstance(v, Decimal):
            return pd.to_numeric(s, errors="coerce").astype("float64")
        if isinstance(v, (datetime.date, datetime.datetime, pd.Timestamp)):
            return s.where(s.notna(), None)
    return s.where(s.notna(), None)


def _postprocess(df: pd.DataFrame) -> pd.DataFrame:
    """整理 finance 查询结果的列名与 dtype."""
    stripped = [_strip_table_prefix(c) for c in df.columns]
    seen: dict[str, int] = {}
    names = []
    for base in stripped:
        name = base if base not in seen else f"{base}.{seen[base]}"
        seen[base] = seen.get(base, 0) + 1
        names.append(name)
    for orig in df.columns:
        df[orig] = _convert_series(df[orig])
    df.columns = names
    return df


def _execute(sql_mysql: str) -> pd.DataFrame:
    """执行 MySQL 方言 SQL 并返回 ClickHouse 查询结果."""
    ch_sql = _to_clickhouse_sql(sql_mysql)
    return query_df(get_client(), ch_sql)


class _Finance:
    """聚宽 finance 模块的本地替身."""

    def __getattr__(self, name: str):
        """按需构建并返回 STK_* / FUND_* 表模型."""
        if name in RUN_QUERY_TABLES:
            return build_model(name)
        raise AttributeError(f"finance has no table {name!r}")

    def __dir__(self):
        """返回对象默认属性与可用 STK_* / FUND_* 表名."""
        return list(super().__dir__()) + list(RUN_QUERY_TABLES)

    def run_query(self, query_object: SqlQuery) -> pd.DataFrame:
        """执行 finance 查询并返回完整结果.

        本地执行时不截断, 去掉聚宽默认 5000 行上限, 但保留用户显式 .limit().
        """
        df = _execute(compile_query(query_object))
        return _postprocess(df)

    def run_offset_query(self, query_object: SqlQuery) -> pd.DataFrame:
        """执行 offset 查询并返回完整结果.

        本地执行时不截断, 去掉聚宽 20 万行上限, 通过 ORDER BY id 一次返回全部匹配.
        """
        df = _execute(compile_query(query_object.order_by(text("id"))))
        return _postprocess(df.reset_index(drop=True))

    def get_table_info(self, table: type | str) -> pd.DataFrame:
        """返回数据表字段信息.

        table 可为模型或聚宽表名字符串, 返回列为 field 与 type.
        """
        if isinstance(table, str):
            jq_name = table.upper().replace("FINANCE.", "")
            model = build_model(jq_name)
        else:
            model = table
        rows = [(c.key, str(c.type)) for c in model.__table__.columns]
        return pd.DataFrame(rows, columns=["field", "type"])


finance = _Finance()
