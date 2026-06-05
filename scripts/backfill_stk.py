"""STK 报告期表(finance.run_query 接口底表)统一同步:所有 6 张 stk 表走同一路径。

每张表幂等同步:
  - 表不存在 → 据聚宽模型列类型建表(CREATE IF NOT EXISTS);
  - 表为空   → 从 jqdatasdk 全量回补(按 end_date/pub_date 年份分块);
  - 有数据   → 按 pub_date 近窗口增量(含 report_type=1 重述,ReplacingMergeTree 去重)。

覆盖 income/balance/cashflow + fin_forcast/audit_opinion/report_disclosure(规范名,去别名)。
`--full` 强制全量(按年跳过已有、可续传);`--drop-finance` DROP 3 张 finance_* 表。
每次调用聚宽 API 后打印剩余额度。
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jqdatasdk import finance, get_query_count, query  # noqa: E402

from kedu.db import DATABASE, auth_from_env, get_client  # noqa: E402
from kedu.finance_schema import STK_TABLES, new_table_ddl, schema_from_model  # noqa: E402
from scripts.backfill_jq import jq_auth  # noqa: E402

FINANCE_TABLES = ["finance_income_statement", "finance_balance_sheet", "finance_cashflow_statement"]

# 同步的规范逻辑表(按 CH 表名去重,剔除别名)
STK_SYNC_TABLES: list[str] = []
_seen_ch: set[str] = set()
for _jq, _ch in STK_TABLES.items():
    if _ch not in _seen_ch:
        _seen_ch.add(_ch)
        STK_SYNC_TABLES.append(_jq)


def qlog(msg: str) -> None:
    print(f"{msg} | spare quota: {get_query_count()}")


def _model(jq_name: str):
    return getattr(finance, jq_name)


def _chunk_col(cols: set[str]) -> str | None:
    """全量按年分块所用日期列(取报告期/披露日,无则用交易日 date)。"""
    for c in ("end_date", "pub_date", "date"):
        if c in cols:
            return c
    return None


def _watermark_col(cols: set[str]) -> str | None:
    """增量水位列:**优先 pub_date** —— 财报表靠披露日水位才能补到「旧报告期、新披露/重述」;
    市场汇总表(STK_MT_TOTAL / STK_EXCHANGE_TRADE_INFO)无 pub_date,退到交易日 date。"""
    for c in ("pub_date", "date", "end_date"):
        if c in cols:
            return c
    return None


def _prep(df: pd.DataFrame, schema: list[tuple[str, str]]) -> pd.DataFrame:
    df = df.copy()
    for col, ctype in schema:
        if col not in df.columns:
            df[col] = None
            continue
        if "Date" in ctype:
            s = pd.to_datetime(df[col], errors="coerce")
            df[col] = s.dt.date.where(s.notna(), None)
        elif "Int" in ctype or "UInt" in ctype:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
        elif "Float" in ctype or "Decimal" in ctype:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
        else:
            df[col] = df[col].astype("object").where(df[col].notna(), None)
    df["id"] = pd.to_numeric(df["id"], errors="coerce").fillna(0).astype("int64")
    return df[[c for c, _ in schema]]


def backfill_table(client, jq_name: str, schema: list[tuple[str, str]],
                   start_year: int, end_year: int, skip: bool) -> None:
    """全量回补:按 end_date(或 pub_date) 年份分块拉取(单次≤20 万行)。skip=True 跳过已有年份(续传)。"""
    ch = STK_TABLES[jq_name]
    model = _model(jq_name)
    cols = {c for c, _ in schema}
    chunk_col = _chunk_col(cols)

    if chunk_col is None:
        df = finance.run_offset_query(query(model))
        qlog(f"  {ch}: pulled {len(df)} (no chunk col)")
        if not df.empty:
            client.insert_df(f"{DATABASE}.{ch}", _prep(df, schema))
        return

    col = getattr(model, chunk_col)
    for y in range(start_year, end_year + 1):
        lo, hi = f"{y}-01-01", f"{y}-12-31"
        if skip:
            cnt = client.query(
                f"SELECT count() FROM {DATABASE}.{ch} "
                f"WHERE {chunk_col} >= toDate('{lo}') AND {chunk_col} <= toDate('{hi}')"
            ).result_rows[0][0]
            if cnt:
                print(f"  {ch} {y}: skip ({cnt} rows)")
                continue
        df = finance.run_offset_query(query(model).filter(col >= lo, col <= hi))
        qlog(f"  {ch} {y}: {len(df)} rows")
        if not df.empty:
            client.insert_df(f"{DATABASE}.{ch}", _prep(df, schema))


def incremental(client, jq_name: str, schema: list[tuple[str, str]], overlap_days: int = 180) -> int:
    """按水位列近窗口增量 upsert(ReplacingMergeTree 幂等,含 report_type=1 重述)。

    水位列优先 pub_date(财报表补「旧期新披露/重述」);市场汇总表无 pub_date 时退到 date。
    """
    ch = STK_TABLES[jq_name]
    model = _model(jq_name)
    cur = _watermark_col({c for c, _ in schema})
    if cur is None:
        qlog(f"  {ch}: 无水位列,跳过增量")
        return 0
    mx = client.query(f"SELECT max({cur}) FROM {DATABASE}.{ch}").result_rows[0][0]
    since = (mx - dt.timedelta(days=overlap_days)).isoformat() if mx else "2005-01-01"
    df = finance.run_offset_query(query(model).filter(getattr(model, cur) >= since))
    if df.empty:
        qlog(f"  {ch}: +0 (since {since})")
        return 0
    client.insert_df(f"{DATABASE}.{ch}", _prep(df, schema))
    qlog(f"  {ch}: +{len(df)} (since {since})")
    return len(df)


# 起始年下探到 1990(沪深建市):STK_STATUS_CHANGE 的"已发行未上市/正常上市"等
# IPO 状态事件 pub_date 可早至 2000 年前,默认 2005 会漏拉(财报类表无 pre-2005 数据,
# 早年空块仅多几次返回 0 行的查询,配额可忽略)。
def sync_table(client, jq_name: str, full: bool = False, start_year: int = 1990,
               end_year: int | None = None, overlap_days: int = 180) -> None:
    """统一同步单表:建表(若无) → 空表/强制全量回补,否则增量。"""
    ch = STK_TABLES[jq_name]
    end_year = end_year or dt.date.today().year
    try:
        model = _model(jq_name)
        schema = schema_from_model(model)
    except Exception as e:
        print(f"  {jq_name}: skip 模型不可用 ({str(e)[:60]})")
        return
    client.command(new_table_ddl(ch, schema))  # 表不存在则建(空 ClickHouse 自动建表)
    cnt = client.query(f"SELECT count() FROM {DATABASE}.{ch}").result_rows[0][0]
    try:
        if cnt == 0:
            qlog(f"  {ch}: 空表 → 全量回补")
            backfill_table(client, jq_name, schema, start_year, end_year, skip=False)
        elif full:
            qlog(f"  {ch}: 强制全量({cnt} 行,按年续传)")
            backfill_table(client, jq_name, schema, start_year, end_year, skip=True)
        else:
            incremental(client, jq_name, schema, overlap_days)
    except Exception as e:
        print(f"  ERROR {ch}: {str(e)[:120]}")
        return
    client.command(f"OPTIMIZE TABLE {DATABASE}.{ch} FINAL")


def sync_all(client, tables=None, full: bool = False, start_year: int = 1990,
             end_year: int | None = None, overlap_days: int = 180) -> None:
    for jq_name in (tables or STK_SYNC_TABLES):
        print(f"== {jq_name} ({STK_TABLES[jq_name]}) ==")
        sync_table(client, jq_name, full, start_year, end_year, overlap_days)


def drop_finance(client) -> None:
    for t in FINANCE_TABLES:
        client.command(f"DROP TABLE IF EXISTS {DATABASE}.{t}")
        print(f"  dropped {t}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--tables", help="逗号分隔逻辑表名,缺省=全部 6 张 STK 表")
    p.add_argument("--full", action="store_true", help="强制全量(按年跳过已有、可续传)")
    p.add_argument("--start-year", type=int, default=1990)
    p.add_argument("--end-year", type=int, default=dt.date.today().year)
    p.add_argument("--overlap-days", type=int, default=180)
    p.add_argument("--drop-finance", action="store_true", help="DROP 3 张 finance_* 表后退出")
    args = p.parse_args()

    auth_from_env()
    client = get_client()
    if args.drop_finance:
        print("== DROP finance_* ==")
        drop_finance(client)
        return

    jq_auth()
    tables = [t.strip().upper() for t in args.tables.split(",")] if args.tables else None
    print("== STK 同步(空表→全量,有数据→增量)==")
    sync_all(client, tables, args.full, args.start_year, args.end_year, args.overlap_days)
    qlog("SYNC DONE")


if __name__ == "__main__":
    main()
