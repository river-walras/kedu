"""Vendored jqdatasdk SQL 构造层 + 查询表面模型(去运行时 jqdatasdk 依赖).

仅 copy 出本项目实际复用的那部分:查询模型别名(query / income / balance / ...)与
SQL 生成器(get_fundamentals_sql / fundamentals_redundant_continuously_query_to_sql)及
其 helpers(compile_query / to_date / remove_duplicated_tables)。详见 sqlbuild.py。
"""
from __future__ import annotations

from .sqlbuild import (
    SqlQuery,
    balance,
    cash_flow,
    compile_query,
    fundamentals_redundant_continuously_query_to_sql,
    get_fundamentals_sql,
    income,
    indicator,
    query,
    remove_duplicated_tables,
    to_date,
    valuation,
)

__all__ = [
    "query",
    "SqlQuery",
    "income",
    "balance",
    "cash_flow",
    "indicator",
    "valuation",
    "get_fundamentals_sql",
    "fundamentals_redundant_continuously_query_to_sql",
    "remove_duplicated_tables",
    "compile_query",
    "to_date",
]
