"""get_fundamentals 引擎 + 多日/多季报告期接口.

- get_fundamentals: 用 vendored get_fundamentals_sql(kedu._jqsdk) 产出聚宽同款
  MySQL SQL, 经 sqlglot 转译为 ClickHouse 方言后本地执行, 返回与聚宽一致的 DataFrame.
- get_fundamentals_continuously: 用 vendored 的 continuously SQL 生成器, 多交易日批量.
- get_history_fundamentals: 聚宽该接口为服务端实现, 无可复用 SQL, 按文档语义自实现,
  数据源为已与聚宽逐字段一致的本地报告期表(单季 / *_acc 年度 / balance 快照).
不自写 SQL 解析器.
"""
from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence

import pandas as pd
import sqlglot

from ._jqsdk import (
    SqlQuery,
    fundamentals_redundant_continuously_query_to_sql,
    get_fundamentals_sql,
    remove_duplicated_tables,
)
from .db import DATABASE, get_client, query_df

# 聚宽逻辑表(query 对象里的表名)-> (本地单季表, 本地年度/累计表)
_LOGICAL_TO_LOCAL = {
    "income_statement_day": ("income_statement", "income_statement_acc"),
    "cash_flow_statement_day": ("cash_flow_statement", "cash_flow_statement_acc"),
    "financial_indicator_day": ("financial_indicator", "financial_indicator_acc"),
    "balance_sheet_day": ("balance_sheet", "balance_sheet"),
}
_DATE_COLS = {"day", "statDate", "pubDate"}


def _to_clickhouse_sql(mysql_sql: str) -> str:
    """将 MySQL 方言 SQL 转译为 ClickHouse 方言 SQL."""
    return sqlglot.transpile(mysql_sql, read="mysql", write="clickhouse")[0]


def _strip_default_limit(sql: str, query_object) -> str:
    """移除聚宽 SQL 生成器内置的默认 LIMIT.

    仅当用户未显式调用 .limit() 时移除尾部 LIMIT, 保持本地接口不截断.
    """
    if getattr(query_object, "limit_value", None):
        return sql  # 用户显式限制,保留
    return re.sub(r"\s+LIMIT\s+\d+(\s+OFFSET\s+\d+)?\s*$", "", sql, flags=re.IGNORECASE)


def _strip_table_prefix(col: str) -> str:
    """移除列名前的表名前缀.

    例如 `income_statement_day.code` 转为 `code`, 无前缀列保持原样.
    """
    return col.rsplit(".", 1)[-1]


def _postprocess_fundamentals(df: pd.DataFrame) -> pd.DataFrame:
    """整理基本面查询结果.

    列名去表前缀, 同名列去重, dtype 对齐聚宽, 财务列为 float64,
    日期列为 datetime, code 为字符串.
    """
    stripped = [_strip_table_prefix(c) for c in df.columns]
    seen: dict[str, int] = {}
    deduped = []
    for orig, base in zip(df.columns, stripped):
        name = base if base not in seen else f"{base}.{seen[base]}"
        seen[base] = seen.get(base, 0) + 1
        deduped.append(name)
    df.columns = deduped
    for name, base in zip(deduped, stripped):
        if base in _DATE_COLS:
            df[name] = pd.to_datetime(df[name], errors="coerce")
        elif base == "code":
            df[name] = df[name].astype("string")
        elif base == "id":
            pass
        else:
            df[name] = pd.to_numeric(df[name], errors="coerce").astype("float64")
    return df


_META_COLS = {"code", "day", "statDate", "pubDate", "id"}


def get_fundamentals(query_object: SqlQuery, date: str | dt.date | int | None = None,
                     statDate: str | dt.date | int | None = None) -> pd.DataFrame:
    """本地复刻 jqdatasdk.get_fundamentals.

    date 与 statDate 二选一. 本地执行时不截断, 会去掉聚宽默认 10000 行上限.
    """
    cli = get_client()
    mysql_sql = _strip_default_limit(
        get_fundamentals_sql(query_object, date=date, statDate=statDate,
                             prev_trade_date=lambda d: _prev_trade_date(d, cli)),
        query_object)
    df = query_df(cli, _to_clickhouse_sql(mysql_sql))
    df = _postprocess_fundamentals(df)
    # date 模式下,「_day」视图会按交易日宇宙为尚无任何报告的次新股产出全 NaN 行;
    # 聚宽不返回这类股票,故丢弃所有数据列均为 NaN 的行(与聚宽成员资格一致)。
    if date is not None and not df.empty:
        data_cols = [c for c in df.columns if c not in _META_COLS]
        if data_cols:
            df = df[~df[data_cols].isna().all(axis=1)].reset_index(drop=True)
    return df


def compiled_sql(query_object: SqlQuery, date: str | dt.date | int | None = None,
                 statDate: str | dt.date | int | None = None) -> tuple[str, str]:
    """返回调试用的 MySQL SQL 与 ClickHouse SQL."""
    cli = get_client()
    mysql_sql = get_fundamentals_sql(query_object, date=date, statDate=statDate,
                                     prev_trade_date=lambda d: _prev_trade_date(d, cli))
    return mysql_sql, _to_clickhouse_sql(mysql_sql)


# ---------------------------------------------------------------------------
# 本地交易日历
# ---------------------------------------------------------------------------
def _local_trade_days(end_date, count: int, client) -> list[dt.date]:
    """返回 stock_valuation 中不晚于 end_date 的最近 count 个交易日.

    返回值按升序排列, end_date 为非交易日时自动回退到最近交易日.
    """
    end = pd.to_datetime(end_date).date() if end_date else dt.date.today()
    rows = client.query(
        f"SELECT DISTINCT day FROM {DATABASE}.stock_valuation "
        f"WHERE day <= toDate('{end.isoformat()}') ORDER BY day DESC LIMIT {int(count)}"
    ).result_rows
    days = [r[0] for r in rows]
    return sorted(days)


def _prev_trade_date(date: dt.date, client) -> dt.date:
    """返回 stock_valuation 中不晚于 date 的最近交易日.

    替代聚宽 CalendarService.get_previous_trade_date(原会惰性联网); 无更早交易日时原样返回。
    date 已是交易日则返回自身。数据源与 _local_trade_days 同为本地 stock_valuation。
    """
    d = pd.to_datetime(date).date()
    rows = client.query(
        f"SELECT max(day) FROM {DATABASE}.stock_valuation "
        f"WHERE day <= toDate('{d.isoformat()}')"
    ).result_rows
    if rows and rows[0][0] is not None:
        return pd.to_datetime(rows[0][0]).date()
    return d


def get_fundamentals_continuously(query_object: SqlQuery, end_date: str | dt.date | None = None,
                                  count: int = 1, panel: bool = True) -> pd.DataFrame:
    """本地复刻 jqdatasdk.get_fundamentals_continuously.

    pandas>0.25 一律返回 DataFrame, panel 形参忽略. 列为 day, code, <字段>,
    按 (code, day) 升序, 整数索引. 本地执行时不截断, 会去掉默认 10000 行上限.
    """
    assert count, "count is required"
    cli = get_client()
    trade_days = _local_trade_days(end_date, count, cli)
    if not trade_days:
        return pd.DataFrame(columns=["day", "code"])

    mysql_sql = fundamentals_redundant_continuously_query_to_sql(query_object, trade_days)
    mysql_sql = _strip_default_limit(remove_duplicated_tables(mysql_sql), query_object)
    df = query_df(cli, _to_clickhouse_sql(mysql_sql))
    df = _postprocess_fundamentals(df)

    if "code" in df.columns and "day" in df.columns:
        df = df.sort_values(["code", "day"]).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# get_history_fundamentals
# ---------------------------------------------------------------------------
_QEND = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}


def _qend_date(year: int, q: int) -> dt.date:
    """返回指定年份和季度对应的季末日期."""
    m, d = _QEND[q]
    return dt.date(year, m, d)


def _parse_anchor(stat_date, stat_by_year: bool) -> tuple[int, int]:
    """解析 stat_date 为 year 与 quarter.

    年度模式下 quarter 固定为 4.
    """
    if isinstance(stat_date, int):
        return (stat_date, 4)
    s = str(stat_date).strip().lower()
    if stat_by_year:
        return (int(s), 4)
    if "q" in s:
        y, q = s.split("q")
        return (int(y), int(q))
    return (int(s), 4)  # 'YYYY' 不带 q 且非年度 -> 视作 Q4 单季


def _step_back(year: int, quarter: int, interval: str, n: int) -> list[tuple[int, int]]:
    """从指定报告期起回溯 n 个报告期.

    interval 支持 `1q` 与 `1y`, 返回顺序为由旧到新.
    """
    res = []
    y, q = year, quarter
    for _ in range(n):
        res.append((y, q))
        if interval == "1q":
            q -= 1
            if q == 0:
                q = 4
                y -= 1
        else:  # '1y'
            y -= 1
    return res[::-1]


def _field_table_col(f) -> tuple[str, str]:
    """解析字段所属的聚宽逻辑表名与列名.

    支持 InstrumentedAttribute 或 `table.col` 字符串.
    """
    if isinstance(f, str):
        t, c = f.split(".", 1) if "." in f else ("", f)
        return t, c
    col = f.key
    tname = None
    for getter in (
        lambda: f.class_.__tablename__,
        lambda: f.parent.persist_selectable.name,
        lambda: f.property.columns[0].table.name,
    ):
        try:
            tname = getter()
            if tname:
                break
        except Exception:
            continue
    return tname, col


def _q_in(values) -> str:
    """生成 ClickHouse IN 子句使用的字符串字面量列表."""
    return "(" + ", ".join("'" + str(v) + "'" for v in values) + ")"


def get_history_fundamentals(security: str | Sequence[str], fields: Sequence,
                             watch_date: str | dt.date | None = None,
                             stat_date: str | int | None = None, count: int = 1,
                             interval: str = "1q", stat_by_year: bool = False) -> pd.DataFrame:
    """本地复刻 jqdatasdk.get_history_fundamentals.

    fields 取自 income/balance/cash_flow/indicator 的列. watch_date 与 stat_date 二选一.
    单季模式取本地单季表与 balance 季末快照, 年度模式取 *_acc 与 balance 年末.
    每个股票与报告期一行, 缺失即缺行, 列为 code, statDate 与 fields.
    """
    assert security, "security is required"
    assert fields, "fields is required"
    assert (not watch_date) ^ (not stat_date), "watch_date 与 stat_date 有且只能有一个"
    if stat_by_year:
        assert interval == "1y", 'stat_by_year=True的时候,interval必须等于"1y"'
    cli = get_client()

    codes = [security] if isinstance(security, str) else list(security)
    watch = pd.to_datetime(watch_date).date() if watch_date else None

    # 字段按所属逻辑表分组,记录原始顺序
    order: list[str] = []
    by_logical: dict[str, list[str]] = {}
    for f in fields:
        tname, col = _field_table_col(f)
        if tname not in _LOGICAL_TO_LOCAL:
            raise ValueError(f"unsupported field table: {tname} ({col})")
        by_logical.setdefault(tname, [])
        if col not in by_logical[tname]:
            by_logical[tname].append(col)
        if col not in order:
            order.append(col)
    acc = stat_by_year
    involved = {tname: _LOGICAL_TO_LOCAL[tname][1 if acc else 0] for tname in by_logical}

    # 注:聚宽 watch_date 模式 *会* 返回已退市标的的历史报告期(实测 600091.XSHG 退市于
    # 2022-06-21,watch_date=2022-08-15 仍返回其 2021Q4/2022Q1),故此处不按退市过滤,
    # 成员资格完全由「pubDate<=watch 且各涉及表均有该报告期」的数据驱动,与聚宽一致。

    # 1) 取各涉及表的可见 (code, statDate);行集 = 所有涉及表的交集(聚宽逐表均需有该报告期)
    def _where(extra_periods=None):
        """生成当前查询模式下的 WHERE 条件."""
        w = f"code IN {_q_in(codes)}"
        if watch is not None:
            w += f" AND pubDate <= toDate('{watch.isoformat()}')"
        if extra_periods is not None:
            w += f" AND statDate IN {_q_in([d.isoformat() for d in extra_periods])}"
        elif watch is not None:
            lb = dt.date(watch.year - (count + 2 if interval == '1y' else 3), 1, 1)
            w += f" AND statDate >= toDate('{lb.isoformat()}')"
        return w

    if stat_date is not None:
        ay, aq = _parse_anchor(stat_date, stat_by_year)
        periods = _step_back(ay, aq, interval, count)
        all_qends = sorted({_qend_date(y, q) for (y, q) in periods})
        where = _where(all_qends)
    else:
        where = _where()  # 先按 pubDate/下界拉,后定锚

    subs: dict[str, pd.DataFrame] = {}
    keysets: list[set] = []
    for tname, local_tbl in involved.items():
        cols = by_logical[tname]
        sel = ", ".join(["code", "statDate", *[f"`{c}`" for c in cols]])
        sub = query_df(cli, f"SELECT {sel} FROM {DATABASE}.{local_tbl} WHERE {where}")
        if sub.empty:
            return pd.DataFrame(columns=["code", "statDate", *order])
        sub["statDate"] = pd.to_datetime(sub["statDate"]).dt.date
        sub = sub.drop_duplicates(["code", "statDate"])
        subs[local_tbl] = sub
        keysets.append(set(zip(sub["code"], sub["statDate"])))
    inter = set.intersection(*keysets)  # (code, statDate) 须在所有涉及表中均存在

    # 2) 每票锚定报告期 -> 最终行集(交集内 ∩ 各票报告期序列)
    if stat_date is not None:
        keep = {(c, _qend_date(y, q)) for c in codes for (y, q) in periods} & inter
    else:
        # 全局最新 count 个报告期(市场范围内 pubDate<=watch 的最新报告期回溯 count 期)。
        # 聚宽 watch_date 模式按「每票自身最近 count 期」返回,但只纳入「最新报告期落在
        # 全局最近 count 期内」的标的——长期停更/早年退市股(锚期过旧)不返回。
        gmax = None
        for local_tbl in involved.values():
            r = cli.query(
                f"SELECT max(statDate) FROM {DATABASE}.{local_tbl} "
                f"WHERE pubDate <= toDate('{watch.isoformat()}')"
            ).result_rows
            if r and r[0][0] is not None:
                g = pd.to_datetime(r[0][0]).date()
                gmax = g if gmax is None else max(gmax, g)
        gmin = None
        if gmax is not None:
            gperiods = {_qend_date(y, q)
                        for (y, q) in _step_back(gmax.year, (gmax.month - 1) // 3 + 1, interval, count)}
            gmin = min(gperiods)

        by_code: dict[str, list] = {}
        for c, sd in inter:
            by_code.setdefault(c, []).append(sd)
        keep = set()
        for c, sds in by_code.items():
            anchor = max(sds)
            if gmin is not None and anchor < gmin:
                continue  # 该票最新报告期早于全局最近 count 期 -> 聚宽不返回
            for (y, q) in _step_back(anchor.year, (anchor.month - 1) // 3 + 1, interval, count):
                if (c, _qend_date(y, q)) in inter:
                    keep.add((c, _qend_date(y, q)))
    if not keep:
        return pd.DataFrame(columns=["code", "statDate", *order])

    # 3) 以行集为基,left-join 各表取值
    merged = pd.DataFrame(sorted(keep), columns=["code", "statDate"])
    for sub in subs.values():
        merged = merged.merge(sub, on=["code", "statDate"], how="left")

    # 4) 整理:列序、dtype、排序(不截断)
    for c in order:
        if c not in merged.columns:
            merged[c] = pd.NA
        merged[c] = pd.to_numeric(merged[c], errors="coerce").astype("float64")
    merged["code"] = merged["code"].astype("string")
    merged["statDate"] = pd.to_datetime(merged["statDate"])
    out = merged[["code", "statDate", *order]].sort_values(["code", "statDate"]).reset_index(drop=True)
    return out
