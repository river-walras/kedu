"""从聚宽 get_fundamentals 直接回补/更新:
  - income_statement / income_statement_acc          ← 逐季 / 逐年
  - balance_sheet                                    ← 逐季(截面快照)
  - cash_flow_statement / cash_flow_statement_acc    ← 逐季 / 逐年
  - financial_indicator / financial_indicator_acc    ← 逐季 / 逐年
  - stock_valuation                                  ← 逐交易日

无 code 过滤即返回全市场(实测 <1 万行/次)。幂等:ReplacingMergeTree 去重;已存在的 statDate/day 默认跳过。
通用 backfill_statement 适用 income/balance/cash_flow/indicator;balance 传 single==acc 同表即可。
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jqdatasdk  # noqa: E402
from jqdatasdk import auth, get_query_count, query, indicator, valuation, cash_flow, income, balance  # noqa: E402

from kedu.db import DATABASE, auth_from_env, get_client  # noqa: E402
from kedu.schema import data_columns  # noqa: E402

Q2END = {"q1": "-03-31", "q2": "-06-30", "q3": "-09-30", "q4": "-12-31"}


def jq_auth() -> None:
    auth(os.getenv("JQDATA_USER"), os.getenv("JQDATA_PASSWORD"))
    print("query count:", get_query_count())


def _quarter_end(q: str) -> str:
    return q[:4] + Q2END[q[4:].lower()]


def _prep(df: pd.DataFrame, date_cols: list[str]) -> pd.DataFrame:
    df = df.copy()
    if "id" in df.columns:
        df["id"] = pd.to_numeric(df["id"], errors="coerce").fillna(0).astype("int64")
    df["code"] = df["code"].astype(str)
    for d in date_cols:
        df[d] = pd.to_datetime(df[d], errors="coerce").dt.date
    return df


def _existing(client, table: str, key: str) -> set:
    return {str(r[0]) for r in client.query(f"SELECT DISTINCT {key} FROM {DATABASE}.{table}").result_rows}


def backfill_statement(client, model, single_table: str, acc_table: str,
                       quarters: list[str], years: list[str], skip=True) -> None:
    """通用:full-model 逐季→single_table(单季),逐年→acc_table(报告期累计)。"""
    cols = data_columns(model)
    qobj = query(model.id, model.code, model.pubDate, model.statDate, *[getattr(model, c) for c in cols])
    out = ["id", "code", "statDate", "pubDate", *cols]

    done = _existing(client, single_table, "statDate") if skip else set()
    for q in quarters:
        if skip and _quarter_end(q) in done:
            print(f"  {single_table} {q}: skip"); continue
        df = jqdatasdk.get_fundamentals(qobj, statDate=q)
        if df.empty:
            print(f"  {single_table} {q}: empty"); continue
        client.insert_df(f"{DATABASE}.{single_table}", _prep(df, ["statDate", "pubDate"])[out])
        print(f"  {single_table} {q}: {len(df)}")

    done_acc = _existing(client, acc_table, "statDate") if skip else set()
    for y in years:
        if skip and f"{y}-12-31" in done_acc:
            print(f"  {acc_table} {y}: skip"); continue
        df = jqdatasdk.get_fundamentals(qobj, statDate=str(y))
        if df.empty:
            print(f"  {acc_table} {y}: empty"); continue
        client.insert_df(f"{DATABASE}.{acc_table}", _prep(df, ["statDate", "pubDate"])[out])
        print(f"  {acc_table} {y}: {len(df)}")


def backfill_valuation(client, dates: list[str], skip=True) -> None:
    cols = data_columns(valuation)
    qobj = query(valuation.id, valuation.code, valuation.day, *[getattr(valuation, c) for c in cols])
    out = ["id", "code", "day", *cols]
    done = _existing(client, "stock_valuation", "day") if skip else set()
    for d in dates:
        if skip and str(d) in done:
            print(f"  valuation {d}: skip"); continue
        df = jqdatasdk.get_fundamentals(qobj, date=str(d))
        if df.empty:
            print(f"  valuation {d}: empty"); continue
        client.insert_df(f"{DATABASE}.stock_valuation", _prep(df, ["day"])[out])
        print(f"  valuation {d}: {len(df)}")


def sync_is_st(client, today: str | None = None, limit_codes: int | None = None,
               min_spare: int = 2_000_000) -> None:
    """逐票同步 is_st(ST 状态布尔)到 jqdata.is_st。断点续传、不重下、退市票锚 end_date。

    数据源 jqdatasdk.get_extras('is_st', [code], start, end)。每票:
      - 拉取窗口 = [is_st 表里该票 max(date)+1 (无则 securities.start_date), min(end_date, today)];
      - pull_start > target_end 即已下全(退市票下到退市日 / 当日已更)→ 跳过,不重复下载;
      - 剩余配额低于 min_spare 时优雅停止,跨天重跑同命令自动 resume。
    回补与日更共用此函数:日更时每票只拉新尾、退市票跳过,天然增量。
    """
    from kedu.schema import MARKET_DDL
    client.command(MARKET_DDL["is_st"])
    today_d = dt.date.fromisoformat(today) if today else dt.date.today()

    windows = client.query(
        f"SELECT instrument_id, start_date, end_date FROM {DATABASE}.securities "
        f"ORDER BY instrument_id").result_rows
    last = {r[0]: r[1] for r in client.query(
        f"SELECT instrument_id, max(date) FROM {DATABASE}.is_st GROUP BY instrument_id"
    ).result_rows}

    done = pulled = 0
    for i, (code, start_date, end_date) in enumerate(windows):
        if limit_codes is not None and i >= limit_codes:
            break
        if get_query_count()["spare"] < min_spare:
            print(f"  is_st: 剩余配额不足 {min_spare},优雅停止于第 {i} 只(重跑续传)")
            break
        target_end = min(end_date or today_d, today_d)
        prev = last.get(code)
        pull_start = (prev + dt.timedelta(days=1)) if prev else (start_date or dt.date(2005, 1, 1))
        if pull_start > target_end:
            done += 1
            continue
        s = jqdatasdk.get_extras("is_st", [code], start_date=pull_start.isoformat(),
                                 end_date=target_end.isoformat(), df=True)
        if s is None or s.empty:
            continue
        col = s.iloc[:, 0]
        m = col.notna()
        if not m.any():
            continue
        out = pd.DataFrame({
            "instrument_id": code,
            "date": pd.to_datetime(s.index[m.to_numpy()]).date,
            "is_st": col[m].astype(bool).astype("uint8").to_numpy(),
        })
        client.insert_df(f"{DATABASE}.is_st", out)
        pulled += 1
        if pulled % 200 == 0:
            print(f"  is_st: {pulled} 票已拉 | spare quota: {get_query_count()['spare']}")
    print(f"  is_st: 新拉 {pulled} 票,跳过(已全/退市){done} 票")


def sync_trade_days(client) -> None:
    """同步交易日历到 jqdata.trade_days:空表全量、否则插入 max(day) 之后的新日。幂等。

    get_all_trade_days() 每次返回全量日历(含近未来已排定交易日),按 max(day) 增量插入即可。
    """
    from kedu.schema import MARKET_DDL
    client.command(MARKET_DDL["trade_days"])
    mx = client.query(f"SELECT max(day) FROM {DATABASE}.trade_days").result_rows[0][0]
    all_days = list(jqdatasdk.get_all_trade_days())
    new = [d for d in all_days if mx is None or d > mx]
    if not new:
        print(f"  trade_days: up to date (total {len(all_days)})"); return
    df = pd.DataFrame({"day": pd.Series(pd.to_datetime(new)).dt.date})
    client.insert_df(f"{DATABASE}.trade_days", df)
    print(f"  trade_days: +{len(new)} (total now {len(all_days)})")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--quarters", help="逗号分隔 2024q1,2024q2")
    p.add_argument("--years", help="逗号分隔 2023,2024")
    p.add_argument("--tables", default="income,balance,cash_flow,indicator",
                   help="逗号分隔: income,balance,cash_flow,indicator")
    p.add_argument("--val-start"); p.add_argument("--val-end"); p.add_argument("--val-dates")
    p.add_argument("--no-skip", action="store_true")
    p.add_argument("--optimize", action="store_true")
    p.add_argument("--trade-days", action="store_true", help="同步交易日历 trade_days")
    p.add_argument("--is-st", action="store_true", help="逐票同步 is_st(ST 状态),断点续传")
    p.add_argument("--limit-codes", type=int, help="is_st 仅处理前 N 只(测试用)")
    p.add_argument("--min-spare", type=int, default=2_000_000, help="is_st 剩余配额低于此值优雅停止")
    args = p.parse_args()

    jq_auth()
    auth_from_env()
    client = get_client()
    skip = not args.no_skip
    quarters = [s.strip() for s in args.quarters.split(",")] if args.quarters else []
    years = [s.strip() for s in args.years.split(",")] if args.years else []
    tabs = [s.strip() for s in args.tables.split(",")]

    if args.trade_days:
        print("== trade_days =="); sync_trade_days(client)

    if args.is_st:
        print("== is_st ==")
        sync_is_st(client, limit_codes=args.limit_codes, min_spare=args.min_spare)

    if (quarters or years):
        if "income" in tabs:
            print("== income =="); backfill_statement(client, income, "income_statement", "income_statement_acc", quarters, years, skip)
        if "balance" in tabs:
            print("== balance =="); backfill_statement(client, balance, "balance_sheet", "balance_sheet", quarters, years, skip)
        if "cash_flow" in tabs:
            print("== cash_flow =="); backfill_statement(client, cash_flow, "cash_flow_statement", "cash_flow_statement_acc", quarters, years, skip)
        if "indicator" in tabs:
            print("== indicator =="); backfill_statement(client, indicator, "financial_indicator", "financial_indicator_acc", quarters, years, skip)

    dates: list[str] = []
    if args.val_dates:
        dates = [s.strip() for s in args.val_dates.split(",")]
    elif args.val_start and args.val_end:
        dates = [d.strftime("%Y-%m-%d") for d in jqdatasdk.get_trade_days(start_date=args.val_start, end_date=args.val_end)]
    if dates:
        print("== valuation =="); backfill_valuation(client, dates, skip)

    if args.optimize:
        for t in ("income_statement", "income_statement_acc", "balance_sheet",
                  "cash_flow_statement", "cash_flow_statement_acc", "financial_indicator",
                  "financial_indicator_acc", "stock_valuation"):
            client.command(f"OPTIMIZE TABLE {DATABASE}.{t} FINAL")
    print("query count:", get_query_count())
    print("DONE")


if __name__ == "__main__":
    main()
