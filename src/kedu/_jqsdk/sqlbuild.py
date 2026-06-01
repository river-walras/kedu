# coding: utf-8
"""Vendored 自 jqdatasdk 的 SQL 构造层(去 jqdatasdk 运行时依赖).

源:jqdatasdk.utils(SqlQuery / query / compile_query / to_date / remove_duplicated_tables)
与 jqdatasdk.finance_service(get_fundamentals_sql / fundamentals_redundant_continuously_query_to_sql)。
逐字搬运,仅做两点改写:
- 删去 py2(six)兼容分支,py3.13 下 str/bytes 直接判定;
- get_fundamentals_sql 的「估值表 + statDate」分支原会惰性联网调 CalendarService.get_previous_trade_date,
  改为由调用方注入 prev_trade_date(本地 ClickHouse 交易日查询),不再触网。
"""
from __future__ import annotations

import datetime
import re
from collections.abc import Callable

from sqlalchemy.orm import Query, scoped_session, sessionmaker

from .finance_tables import (  # noqa: F401  (StockValuation 等供 get_table_class 使用)
    BalanceSheetDay,
    BankIndicatorAcc,
    CashFlowStatementDay,
    FinancialIndicatorDay,
    IncomeStatementDay,
    InsuranceIndicatorAcc,
    SecurityIndicatorAcc,
    StockValuation,
)

FUNDAMENTAL_RESULT_LIMIT = 10000


# ---------------------------------------------------------------------------
# utils.py: query / compile_query / to_date / remove_duplicated_tables
# ---------------------------------------------------------------------------
def remove_duplicated_tables(sql: str) -> str:
    """去除 sql 中 from 重复的表名: from a, b, a --> from a, b."""
    assert isinstance(sql, str), "sql类型有误"
    table_names = re.findall("from(.*?) where", sql, re.S) or re.findall("FROM(.*?)WHERE", sql, re.S)
    assert table_names, "未从sql语句中发现对应表名"
    unique_table_names = list(set(table_names[0].strip().replace(" ", "").split(",")))
    unique_sql = sql.replace(table_names[0], " " + ",".join(unique_table_names) + " ")
    return unique_sql


_sql_session = scoped_session(sessionmaker())


class SqlQuery(Query):
    """记录 limit/offset 的 sqlalchemy Query 子类(供 SQL 生成器读取原始上限)."""

    limit_value = None
    offset_value = None

    def limit(self, limit):
        self.limit_value = limit
        return super().limit(limit)

    def offset(self, offset):
        self.offset_value = offset
        return super().offset(offset)


def query(*args, **kwargs) -> SqlQuery:
    """构造一个带会话的 SqlQuery, 对齐 jqdatasdk.query."""
    return SqlQuery(args, **kwargs).with_session(_sql_session)


def compile_query(query) -> str:
    """把一个 sqlalchemy query object 编译成 mysql 风格的 sql 语句."""
    from pymysql.converters import conversions, encoders, escape_item
    from sqlalchemy.dialects import mysql as mysql_dialetct

    dialect = mysql_dialetct.dialect()
    statement = query.statement
    compile_kwargs = {"render_postcompile": True}
    comp = statement.compile(dialect=dialect, compile_kwargs=compile_kwargs)
    comp_params = comp.params
    params = []
    for k in comp.positiontup:
        v = comp_params[k]
        v = escape_item(v, conversions, encoders)
        params.append(v)
    return comp.string % tuple(params)


def to_date(value):
    """转化为 datetime.date 类型."""
    if not value:
        return value
    if isinstance(value, str):
        date = value[:10]
        try:
            separator = date[4]
            if separator in {'-', '/'}:
                return datetime.date(*map(int, date.split(separator)))
            else:
                return datetime.date(int(value[:4]), int(value[4:6]), int(value[6:8]))
        except Exception:
            pass
    elif isinstance(value, datetime.datetime):
        return value.date()
    elif isinstance(value, datetime.date):
        return value
    elif isinstance(value, int):
        return to_date(str(value))
    elif isinstance(value, bytes):
        return to_date(value.decode('utf8'))
    raise ValueError("无效的日期：{!r}".format(value))


# ---------------------------------------------------------------------------
# finance_service.py: get_fundamentals_sql 及其私有 helpers
# ---------------------------------------------------------------------------
def get_tables_from_sql(sql: str) -> list:
    """从 sql 中提取涉及的逻辑表名(_day / _acc / stock_valuation)."""
    m = re.findall(
        r'cash_flow_statement_day|balance_sheet_day|financial_indicator_day|'
        r'income_statement_day|stock_valuation|bank_indicator_acc|'
        r'security_indicator_acc|insurance_indicator_acc',
        sql
    )
    return list(set(m))


def get_table_class(tablename: str):
    """按 __tablename__ 返回对应模型类."""
    for t in (
        BalanceSheetDay, CashFlowStatementDay, FinancialIndicatorDay,
        IncomeStatementDay, StockValuation, BankIndicatorAcc,
        SecurityIndicatorAcc, InsuranceIndicatorAcc,
    ):
        if t.__tablename__ == tablename:
            return t


def get_fundamentals_sql(query_object, date=None, statDate=None, *,
                         prev_trade_date: Callable[[datetime.date], datetime.date] | None = None) -> str:
    """生成聚宽同款 get_fundamentals MySQL SQL.

    估值表 + statDate 分支需要「不晚于 statDate 的最近交易日」, 由 prev_trade_date 注入
    (本地 ClickHouse 查询); 未注入而又走到该分支时直接 raise, 不静默触网。
    """
    if not isinstance(query_object, Query):
        raise AssertionError(
            "query_object must be a sqlalchemy's Query object."
            " But what passed in was: " + str(type(query_object))
        )

    stat_date = statDate
    assert (not date) ^ (not stat_date), "(statDate, date) only one param is required"

    if query_object.limit_value:
        limit = min(FUNDAMENTAL_RESULT_LIMIT, query_object.limit_value)
    else:
        limit = FUNDAMENTAL_RESULT_LIMIT
    offset = query_object.offset_value
    query_object = query_object.limit(None).offset(None)

    tablenames = get_tables_from_sql(str(query_object.statement))
    tables = [get_table_class(name) for name in tablenames]

    by_year = False
    only_year = bool({
        "bank_indicator_acc",
        "security_indicator_acc",
        "insurance_indicator_acc"
    } & set(tablenames))

    if only_year:
        if date:
            date = None
            stat_date = str(datetime.date.min)
        elif stat_date:
            if isinstance(stat_date, str):
                stat_date = stat_date.lower()
                if 'q' in stat_date:
                    stat_date = '0001-01-01'
                else:
                    stat_date = '{}-12-31'.format(int(stat_date))
            elif isinstance(stat_date, int):
                stat_date = '{}-12-31'.format(stat_date)

            stat_date = to_date(stat_date)
        else:
            today = datetime.date.today()
            yesteryear = today.year - 1
            stat_date = datetime.date(yesteryear, 12, 31)
    elif stat_date:
        if isinstance(stat_date, str):
            stat_date = stat_date.lower()
            if 'q' in stat_date:
                stat_date = (stat_date.replace('q1', '-03-31')
                             .replace('q2', '-06-30')
                             .replace('q3', '-09-30')
                             .replace('q4', '-12-31'))
            else:
                year = int(stat_date)
                by_year = True
                stat_date = '%s-12-31' % year
        elif isinstance(stat_date, int):
            year = int(stat_date)
            by_year = True
            stat_date = '%s-12-31' % year

        stat_date = to_date(stat_date)

    # 不晚于 stat_date 的一个交易日
    trade_day_not_after_stat_date = None
    for table in tables:
        if date:
            query_object = query_object.filter(table.day == date)
        else:
            if hasattr(table, 'statDate'):
                query_object = query_object.filter(table.statDate == stat_date)
            else:
                # 估值表, 在非交易日没有数据; 传入非交易日时取前一个交易日
                assert table is StockValuation
                if trade_day_not_after_stat_date is None:
                    if prev_trade_date is None:
                        raise RuntimeError(
                            "估值表 + statDate 查询需要本地交易日历; 调用方未注入 prev_trade_date"
                        )
                    trade_day_not_after_stat_date = prev_trade_date(stat_date)
                query_object = query_object.filter(table.day == trade_day_not_after_stat_date)

    # 连表
    for table in tables[1:]:
        query_object = query_object.filter(table.code == tables[0].code)

    # 恢复 offset, limit
    query_object = query_object.limit(limit).offset(offset)

    # 编译 query 对象为纯 sql
    sql = compile_query(query_object)

    if stat_date:
        if by_year:
            sql = sql.replace('balance_sheet_day', 'balance_sheet')\
                     .replace('financial_indicator_day', 'financial_indicator_acc')\
                     .replace('income_statement_day', 'income_statement_acc')\
                     .replace('cash_flow_statement_day', 'cash_flow_statement_acc')
        else:
            for t in ('balance_sheet_day', 'financial_indicator_day', 'income_statement_day',
                      'cash_flow_statement_day'):
                sql = sql.replace(t, t[:-4])
        sql = re.sub(r'(cash_flow_statement|balance_sheet|income_statement|financial_indicator|'
                     r'financial_indicator_acc|income_statement_acc|cash_flow_statement_acc)\.`?day`?\b',
                     r'\1.statDate', sql)
    return sql


def fundamentals_redundant_continuously_query_to_sql(query, trade_day) -> str:
    """生成多交易日 get_fundamentals_continuously 的 MySQL SQL (trade_day 为交易日列表)."""
    from .fundamentals_tables_gen import (
        BankIndicatorAcc,
        CashFlowStatement,
        FinancialIndicator,
        IncomeStatement,
        InsuranceIndicatorAcc,
        SecurityIndicatorAcc,
        StockValuation,
    )
    from .fundamentals_tables_gen import (
        BalanceSheet,
    )

    if query.limit_value:
        limit = min(FUNDAMENTAL_RESULT_LIMIT, query.limit_value)
    else:
        limit = FUNDAMENTAL_RESULT_LIMIT
    offset = query.offset_value
    query = query.limit(None).offset(None)

    def get_table_class(tablename):
        for t in (BalanceSheet, CashFlowStatement, FinancialIndicator,
                  IncomeStatement, StockValuation, BankIndicatorAcc, SecurityIndicatorAcc,
                  InsuranceIndicatorAcc):
            if t.__tablename__ == tablename:
                return t

    def get_tables_from_sql(sql):
        m = re.findall(
            r'cash_flow_statement_day|balance_sheet_day|financial_indicator_day|'
            r'income_statement_day|stock_valuation|bank_indicator_acc|'
            r'security_indicator_acc|insurance_indicator_acc',
            sql
        )
        return list(set(m))
    # 从 query 对象获取表对象
    tablenames = get_tables_from_sql(str(query.statement))
    tables = [get_table_class(name) for name in tablenames]
    query = query.filter(StockValuation.day.in_(trade_day))
    # 根据 stock_valuation 表的 code 和 day 字段筛选
    for table in tables:
        if table is not StockValuation:
            query = query.filter(StockValuation.code == table.code)
            if hasattr(table, 'day'):
                query = query.filter(StockValuation.day == table.day)
            else:
                query = query.filter(StockValuation.day == table.statDate)

    # 连表
    for table in tables[1:]:
        query = query.filter(table.code == tables[0].code)

    # 恢复 offset, limit
    query = query.limit(limit).offset(offset)
    sql = compile_query(query)
    # 默认添加查询 code 和 day 作为 panel 索引
    sql = sql.replace(
        'SELECT ',
        'SELECT DISTINCT stock_valuation.day AS day, stock_valuation.code as code, '
    )
    return sql


# 模型别名(对齐 jqdatasdk 顶层导出的查询表面)
balance = balance_sheet = BalanceSheetDay
income = income_statement = IncomeStatementDay
cash_flow = cash_flow_statement = CashFlowStatementDay
indicator = financial_indicator = FinancialIndicatorDay
valuation = stock_valuation = StockValuation
bank_indicator = bank_indicator_acc = BankIndicatorAcc
security_indicator = security_indicator_acc = SecurityIndicatorAcc
insurance_indicator = insurance_indicator_acc = InsuranceIndicatorAcc
