"""由 ClickHouse 表结构惰性构建 STK_* 的 SQLAlchemy 声明式模型.

聚宽 `finance.STK_*` 模型需 auth 才能构建. 本地改为 introspect `DESCRIBE jqdata.<t>`,
程序化生成同名同序的 SQLAlchemy 模型. `__tablename__` 设为 ClickHouse 物理表名,
使 jqdatasdk.utils.compile_query 产出的 MySQL SQL 直接命中 CH 表, 经 sqlglot 转译执行.
列序等于 CH 列序与聚宽列序.
"""
from __future__ import annotations

import re

from sqlalchemy import BigInteger, Column, Date, DateTime, Float, Numeric, String
from sqlalchemy.orm import declarative_base

from .db import DATABASE, get_client
from .finance_schema import RUN_QUERY_TABLES

_Base = declarative_base()
_CACHE: dict[str, type] = {}


def _sa_type(ch_type: str):
    """将 ClickHouse 类型字符串映射为 SQLAlchemy 类型."""
    inner = ch_type
    if inner.startswith("Nullable(") and inner.endswith(")"):
        inner = inner[len("Nullable("):-1]
    if inner.startswith("Decimal"):
        m = re.search(r"Decimal\((\d+)\s*,\s*(\d+)\)", inner)
        return Numeric(int(m.group(1)), int(m.group(2))) if m else Numeric()
    if inner.startswith(("Int", "UInt")):
        return BigInteger
    if inner.startswith("Float"):
        return Float
    if inner.startswith("Date32") or inner == "Date":
        return Date
    if inner.startswith("DateTime"):
        return DateTime
    return String


def build_model(jq_name: str) -> type:
    """返回并缓存 STK 逻辑表对应的 SQLAlchemy 模型.

    按 ClickHouse 物理表名缓存,使别名(如 STK_CASHFLOW_STATEMENT /
    STK_CASH_FLOW_STATEMENT)复用同一映射类, 避免 SQLAlchemy 重复定义同表.
    """
    if jq_name not in RUN_QUERY_TABLES:
        raise AttributeError(jq_name)
    ch = RUN_QUERY_TABLES[jq_name]
    if ch in _CACHE:
        return _CACHE[ch]
    desc = get_client().query(f"DESCRIBE {DATABASE}.`{ch}`").result_rows
    attrs: dict = {"__tablename__": ch}
    has_pk = False
    first_col = None
    for row in desc:
        col, ctype = row[0], row[1]
        if col == "_ingested_at":
            continue
        first_col = first_col or col
        pk = col == "id"
        attrs[col] = Column(col, _sa_type(ctype), primary_key=pk)
        has_pk = has_pk or pk
    if not has_pk and first_col:
        # SQLAlchemy 声明式必须有主键;无 id 时以首列充当(仅模型层面)
        attrs[first_col].primary_key = True
    model = type(jq_name, (_Base,), attrs)
    _CACHE[ch] = model
    return model
