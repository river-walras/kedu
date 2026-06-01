"""聚宽增量更新驱动(pm2 调度)。新架构:5 张基本面表 + bar_1d 全部直接落聚宽。

  1) securities          ← get_all_securities(['stock'])
  2) 报告期基本面增量     ← get_fundamentals(income/balance/cash_flow/indicator, statDate=近 N 季 + 近 2 年)
  3) stock_valuation     ← 自 max(day)+1 起逐交易日
  4) bar_1d 增量          ← get_price(fq=None 原始价 + fq='post' 因子) 逐交易日

所有写入 ReplacingMergeTree,可重复执行。income/balance 不再本地推导,
date 模式由 *_day 视图现算(pubDate<=day 取 max statDate)。
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import jqdatasdk  # noqa: E402
from jqdatasdk import (get_all_securities, get_query_count, get_trade_days,  # noqa: E402
                       income, balance, indicator, cash_flow)

from kedu.db import DATABASE, auth_from_env, get_client  # noqa: E402
import scripts.backfill_jq as bk  # noqa: E402
import scripts.backfill_stk as stk  # noqa: E402


def recent_quarters(n_quarters: int = 8) -> tuple[list[str], list[str]]:
    today = dt.date.today()
    qs = []
    y, q = today.year, (today.month - 1) // 3 + 1
    for _ in range(n_quarters):
        qs.append(f"{y}q{q}")
        q -= 1
        if q == 0:
            q = 4
            y -= 1
    years = [str(today.year), str(today.year - 1)]
    return qs[::-1], years


def update_securities(client) -> None:
    df = get_all_securities(["stock"]).reset_index().rename(columns={"index": "instrument_id"})
    df["start_date"] = pd.to_datetime(df["start_date"]).dt.date
    df["end_date"] = pd.to_datetime(df["end_date"]).dt.date
    df["type"] = "stock"
    for c in ("exchange", "board_type", "industry_code", "sector_code", "round_lot", "status"):
        if c not in df:
            df[c] = None
    cols = ["instrument_id", "display_name", "name", "start_date", "end_date", "type",
            "exchange", "board_type", "industry_code", "sector_code", "round_lot", "status"]
    client.command(f"TRUNCATE TABLE {DATABASE}.securities")
    client.insert_df(f"{DATABASE}.securities", df[cols])
    print(f"  securities: {len(df)}")


def update_bars(client, start: str, end: str) -> None:
    """逐交易日增量写 bar_1d:fq=None 原始价 + fq='post' 后复权因子(聚宽口径)。

    后复权因子以 IPO 为基准、随时间累乘,新除权只抬高其后日期的因子,历史日不变,
    故按日增量拉取即可,无需回改历史。"""
    days = [d.strftime("%Y-%m-%d") for d in get_trade_days(start_date=start, end_date=end)]
    if not days:
        print("  bar_1d: up to date")
        return
    windows = client.query(
        f"SELECT instrument_id, start_date, end_date FROM {DATABASE}.securities "
        f"ORDER BY instrument_id").result_rows
    pf = ["open", "close", "high", "low", "pre_close", "high_limit", "low_limit", "volume", "money", "avg", "paused"]
    out = ["instrument_id", "date", "open", "close", "high", "low", "pre_close",
           "high_limit", "low_limit", "volume", "money", "avg", "factor", "paused", "is_st"]
    total = 0
    for d in days:
        day = dt.date.fromisoformat(d)
        codes = [
            code for code, start_date, end_date in windows
            if (start_date is None or start_date <= day) and (end_date is None or day <= end_date)
        ]
        if not codes:
            continue
        raw = jqdatasdk.get_price(codes, end_date=d, count=1, frequency="daily", fields=pf,
                                  fq=None, panel=False, skip_paused=False)
        fac = jqdatasdk.get_price(codes, end_date=d, count=1, frequency="daily", fields=["factor"],
                                  fq="post", panel=False, skip_paused=False)
        if raw is None or raw.empty:
            continue
        raw = raw.rename(columns={"code": "instrument_id", "time": "date"})
        fac = fac.rename(columns={"code": "instrument_id", "time": "date"})
        m = raw.merge(fac[["instrument_id", "factor"]], on="instrument_id", how="left")
        m = m[m["close"].notna()].copy()
        m["date"] = pd.to_datetime(m["date"]).dt.date
        m["paused"] = m["paused"].fillna(0).astype("uint8")
        m["is_st"] = 0
        m["factor"] = m["factor"].fillna(1.0)
        client.insert_df(f"{DATABASE}.bar_1d", m[out])
        total += len(m)
    print(f"  bar_1d: +{total} 行 / {len(days)} 天")


def update_bars_1m(client, start: str, end: str, batch: int = 300) -> None:
    """逐交易日增量写 bar_1m:fq=None 原始分钟价 + 当日后复权因子(日线 fq='post',按 code 广播到分钟)。

    分钟量大(全市场每日 ~132 万行),按 batch 只数分批拉 get_price(frequency='1m');
    后复权因子在同一交易日内对每只票为常数,故按日取一次广播即可。"""
    days = [d.strftime("%Y-%m-%d") for d in get_trade_days(start_date=start, end_date=end)]
    if not days:
        print("  bar_1m: up to date")
        return
    windows = client.query(
        f"SELECT instrument_id, start_date, end_date FROM {DATABASE}.securities "
        f"ORDER BY instrument_id").result_rows
    pf = ["open", "close", "high", "low", "pre_close", "high_limit", "low_limit", "volume", "money", "avg", "paused"]
    out = ["instrument_id", "datetime", "open", "close", "high", "low", "pre_close",
           "high_limit", "low_limit", "volume", "money", "avg", "factor", "paused"]
    total = 0
    for d in days:
        day = dt.date.fromisoformat(d)
        codes = [
            code for code, start_date, end_date in windows
            if (start_date is None or start_date <= day) and (end_date is None or day <= end_date)
        ]
        if not codes:
            print(f"  bar_1m {d}: +0")
            continue
        fac = jqdatasdk.get_price(codes, end_date=d, count=1, frequency="daily", fields=["factor"],
                                  fq="post", panel=False, skip_paused=False)
        fmap = {}
        if fac is not None and not fac.empty:
            fac = fac.rename(columns={"code": "instrument_id"})
            fmap = dict(zip(fac["instrument_id"], fac["factor"]))
        day_rows = 0
        for i in range(0, len(codes), batch):
            chunk = codes[i:i + batch]
            raw = jqdatasdk.get_price(chunk, start_date=d, end_date=d, frequency="1m", fields=pf,
                                      fq=None, panel=False, skip_paused=False)
            if raw is None or raw.empty:
                continue
            raw = raw.rename(columns={"code": "instrument_id", "time": "datetime"})
            raw = raw[raw["close"].notna()].copy()
            if raw.empty:
                continue
            raw["datetime"] = pd.to_datetime(raw["datetime"])
            raw["factor"] = raw["instrument_id"].map(fmap).fillna(1.0)
            raw["paused"] = raw["paused"].fillna(0).astype("uint8")
            client.insert_df(f"{DATABASE}.bar_1m", raw[out])
            day_rows += len(raw)
        total += day_rows
        print(f"  bar_1m {d}: +{day_rows}")
    print(f"  bar_1m: +{total} 行 / {len(days)} 天")


def _max_day(client, table, col):
    return client.query(f"SELECT max({col}) FROM {DATABASE}.{table}").result_rows[0][0]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--skip-bars", action="store_true", help="跳过 bar_1d 行情线更新")
    p.add_argument("--skip-bars-1m", action="store_true", help="跳过 bar_1m 分钟线更新")
    p.add_argument("--skip-stk", action="store_true", help="跳过 STK 报告期原始表(finance.run_query 底表)增量")
    p.add_argument("--skip-is-st", action="store_true", help="跳过 is_st(ST 状态)增量")
    p.add_argument("--quarters-back", type=int, default=8)
    p.add_argument("--stk-overlap-days", type=int, default=180)
    args = p.parse_args()

    bk.jq_auth()
    auth_from_env()
    client = get_client()
    today = dt.date.today().isoformat()

    print("== 0) trade_days ==")
    bk.sync_trade_days(client)

    print("== 1) securities ==")
    update_securities(client)

    if not args.skip_is_st:
        print("== 1.5) is_st 增量(每票自 max(date)+1 起,退市票跳过)==")
        bk.sync_is_st(client, today=today)

    qs, years = recent_quarters(args.quarters_back)
    print(f"== 2) 报告期基本面增量 (quarters {qs[0]}..{qs[-1]}, years {years}) ==")
    bk.backfill_statement(client, income, "income_statement", "income_statement_acc", qs, years, skip=False)
    bk.backfill_statement(client, balance, "balance_sheet", "balance_sheet", qs, years, skip=False)
    bk.backfill_statement(client, cash_flow, "cash_flow_statement", "cash_flow_statement_acc", qs, years, skip=False)
    bk.backfill_statement(client, indicator, "financial_indicator", "financial_indicator_acc", qs, years, skip=False)

    print("== 3) stock_valuation 增量 ==")
    last = _max_day(client, "stock_valuation", "day")
    vstart = (last + dt.timedelta(days=1)).isoformat() if last else "2005-01-01"
    vdates = [d.strftime("%Y-%m-%d") for d in get_trade_days(start_date=vstart, end_date=today)]
    bk.backfill_valuation(client, vdates, skip=True)

    if not args.skip_bars:
        print("== 4) bar_1d 增量 ==")
        lastb = _max_day(client, "bar_1d", "date")
        bstart = (lastb + dt.timedelta(days=1)).isoformat() if lastb else "2005-01-01"
        update_bars(client, bstart, today)

    if not args.skip_stk:
        print("== 5) STK 报告期原始表同步 (finance.run_query 底表;空表→全量,有数据→增量) ==")
        stk.sync_all(client, overlap_days=args.stk_overlap_days)

    for t in ("trade_days", "income_statement", "income_statement_acc", "balance_sheet",
              "cash_flow_statement", "cash_flow_statement_acc",
              "financial_indicator", "financial_indicator_acc", "stock_valuation", "bar_1d"):
        client.command(f"OPTIMIZE TABLE {DATABASE}.{t} FINAL")

    # bar_1m 最重、最耗配额:放在最后,确保其余必要更新先全部完成
    if not args.skip_bars_1m:
        print("== 6) bar_1m 增量(最后)==")
        lastb1m = _max_day(client, "bar_1m", "datetime")
        # 空表只从今日起增量;历史回补走 rebuild_from_jq.py --bars-1m(量巨大、单独跑)
        b1mstart = (lastb1m.date() + dt.timedelta(days=1)).isoformat() if lastb1m else today
        update_bars_1m(client, b1mstart, today)

    print("query count:", get_query_count())
    print("UPDATE DONE")


if __name__ == "__main__":
    main()
