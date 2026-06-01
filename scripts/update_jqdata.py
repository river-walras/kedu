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


def update_bars_1m(client, today: str, min_spare: int = 2_000_000) -> None:
    """逐票补齐 bar_1m 到 today:每票自其 max(datetime) 之后(无数据则自上市日)补到 today。

    fq=None 原始分钟价 + 日线 fq='post' 因子(按日广播到分钟),按 (票, 年) 切块拉
    get_price(frequency='1m')。游标取**各票各自的 max(datetime)**,而非全局最新日:
    故缺失的票自动补全历史、已满的票只补新交易日,二者用同一逻辑收敛到全覆盖。

    全市场全历史量巨大(数十亿行/配额),剩余配额低于 min_spare 时**优雅停止**;
    bar_1m 为 ReplacingMergeTree((instrument_id, datetime) 去重),重叠插入幂等,
    重跑同命令即按各票 max 自动 resume。
    注:游标按 code 取 max,假设各票数据沿时间向前累积(无内部年份空洞);
    当前数据状态(票要么全历史齐、要么完全缺)满足此前提。"""
    today_d = dt.date.fromisoformat(today)
    REQ_START = dt.date(2005, 1, 1)
    secs = client.query(
        f"SELECT instrument_id, start_date, end_date FROM {DATABASE}.securities "
        f"WHERE type='stock' ORDER BY instrument_id").result_rows
    maxmap = dict(client.query(
        f"SELECT instrument_id, max(datetime) FROM {DATABASE}.bar_1m "
        f"GROUP BY instrument_id").result_rows)
    pf = ["open", "close", "high", "low", "pre_close", "high_limit", "low_limit", "volume", "money", "avg", "paused"]
    out = ["instrument_id", "datetime", "open", "close", "high", "low", "pre_close",
           "high_limit", "low_limit", "volume", "money", "avg", "factor", "paused"]
    total = 0
    for ci, (code, start_date, end_date) in enumerate(secs, 1):
        sp = get_query_count()["spare"]
        if sp < min_spare:
            print(f"  bar_1m: 剩余配额不足 {min_spare:,},优雅停止于第 {ci}/{len(secs)} 只 "
                  f"({code});累计 +{total:,} 行,重跑续传。", flush=True)
            return
        code_start = max(start_date or REQ_START, REQ_START)
        code_end = min(end_date or today_d, today_d)
        have = maxmap.get(code)
        fill_start = (have.date() + dt.timedelta(days=1)) if have else code_start
        if fill_start > code_end:
            continue
        code_rows = 0
        for y in range(fill_start.year, code_end.year + 1):
            ys = max(fill_start, dt.date(y, 1, 1)).isoformat()
            ye = min(code_end, dt.date(y, 12, 31)).isoformat()
            raw = jqdatasdk.get_price(code, start_date=ys, end_date=ye, frequency="1m", fields=pf,
                                      fq=None, panel=False, skip_paused=False)
            if raw is None or raw.empty:
                continue
            raw = raw[raw["close"].notna()].copy()
            if raw.empty:
                continue
            fac = jqdatasdk.get_price(code, start_date=ys, end_date=ye, frequency="daily",
                                      fields=["factor"], fq="post", panel=False, skip_paused=False)
            raw["instrument_id"] = code
            raw["datetime"] = pd.to_datetime(raw.index)
            if fac is not None and not fac.empty:
                fmap = {pd.Timestamp(t).date(): v for t, v in zip(fac.index, fac["factor"])}
                raw["factor"] = raw["datetime"].dt.date.map(fmap)
            else:
                raw["factor"] = 1.0
            raw["factor"] = raw["factor"].ffill().fillna(1.0)
            raw["paused"] = raw["paused"].fillna(0).astype("uint8")
            client.insert_df(f"{DATABASE}.bar_1m", raw[out])
            code_rows += len(raw)
            # 每 (票,年) 落盘即吐一行,确保重活阶段持续有日志(全历史票一只会打多行)
            print(f"  bar_1m {code} {y}: +{len(raw):,} 行", flush=True)
        total += code_rows
        # 空请求票(已满、仅差未发布的今日)不刷屏,仅每 100 只打一次心跳
        if code_rows == 0 and ci % 100 == 0:
            print(f"  bar_1m 心跳 {ci}/{len(secs)} 只(均已最新,累计 +{total:,} 行)| spare {sp:,}", flush=True)
        elif code_rows:
            print(f"  bar_1m {code} 完成: +{code_rows:,} 行(到 {code_end})| 进度 {ci}/{len(secs)} | spare {sp:,}", flush=True)
    print(f"  bar_1m: +{total:,} 行(逐票补到 {today})", flush=True)


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
    p.add_argument("--min-spare", type=int, default=2_000_000,
                   help="bar_1m 逐票补齐时剩余配额低于此值优雅停止(重跑续传)")
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

    # bar_1m 最重、最耗配额:放在最后,确保其余必要更新先全部完成。
    # 逐票自各自 max(datetime) 补到 today:缺失票补全历史、已满票只补新日,
    # 配额不足优雅停止,重跑同命令自动续传(全市场全历史需跨多天)。
    if not args.skip_bars_1m:
        print("== 6) bar_1m 逐票补齐(最后)==")
        update_bars_1m(client, today, min_spare=args.min_spare)

    print("query count:", get_query_count())
    print("UPDATE DONE")


if __name__ == "__main__":
    main()
