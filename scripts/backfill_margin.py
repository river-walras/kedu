"""从聚宽抓取融资融券数据到 ClickHouse(读侧见 kedu.margin):

  - mtss                  ← get_mtss(当日标的并集, d, d) 逐交易日(全局 max(date) 游标续传)
  - margin_target_raw     ← get_margincash_stocks(d) / get_marginsec_stocks(d) 逐交易日快照(staging)
  - margin_target_history ← margin_target_raw gaps-and-islands 折叠成区间('cash'/'sec')

为什么逐日全局游标:融资融券所有标的同日更新,逐日(每日一次列表 + 当日并集的 mtss 分批)比逐股
自愈更省调用。get_margincash_stocks/get_marginsec_stocks 整表一次返回(调用极省);get_mtss 按
**当日标的并集**(staging 已 walk 出的 cash∪sec)分批拉,远比扫全市场在市股省配额、且贴数据语义。

进度与续传:mtss 游标 = mtss 表全局 max(date);标的列表游标 = margin_target_raw 的 max(date)。
逐日边界检查剩余配额,低于 min_spare 优雅停止、重跑续传(种子与日更同一路径,游标保证只取增量)。
ReplacingMergeTree 幂等(mtss 按 (sec_code,date)、staging 按 (type,stock,date)),重叠插入安全。

标的列表先 walk,故 mtss 当日并集可直接读 staging;mtss 仅补到 staging 已覆盖日,避免 universe 不完整。
列表 walk 终点取「≥today 的下一个交易日」(聚宽每天 21:00 披露下一交易日列表),让 date=None 贴 live;
历史显式日期始终精确。

用法(反复跑即可,断点续传):
  uv run --env-file .env python scripts/backfill_margin.py
  可选:--skip-mtss / --skip-targets / --min-spare N / --mtss-batch N
彻底重灌:先 TRUNCATE mtss 或 margin_target_raw,再跑本命令(见空表自动从 2010 重走)。
日常增量由 update_jqdata.py 调 sync() 一并更新。
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jqdatasdk  # noqa: E402
from jqdatasdk import get_query_count  # noqa: E402

from kedu.db import DATABASE, auth_from_env, get_client  # noqa: E402
from kedu.schema import MARGIN_DDL  # noqa: E402
import scripts.backfill_jq as bk  # noqa: E402  (复用 jq_auth)
import scripts.backfill_industry as bi  # noqa: E402  (复用 _resolve_today/_trade_days/_max_date/_batches/_fold)

MARGIN_START = "2010-01-01"   # 历史范围 2010 至今(reference/margin/*)
MTSS_BATCH = 2500             # get_mtss 单次股票数(守 5000 行/次上限)
FLUSH_ROWS = 300_000          # staging 累计行数阈值,日边界落盘

# 标的列表逐日快照 staging(折叠成区间后承载读侧 margin_target_history)。
RAW_DDL = f"""CREATE TABLE IF NOT EXISTS {DATABASE}.margin_target_raw (
  type String, stock String, date Date,
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at)
ORDER BY (type, stock, date)"""

MTSS_COLS = ["sec_code", "date", "fin_value", "fin_buy_value", "fin_refund_value",
             "sec_value", "sec_sell_value", "sec_refund_value", "fin_sec_value"]


def _next_walk_end(client, today_d: dt.date) -> dt.date:
    """标的列表 walk 终点:≥today 的下一个交易日(聚宽 21:00 披露下一交易日);无则止于 today。"""
    nxt = client.query(
        f"SELECT min(day) FROM {DATABASE}.trade_days WHERE day > '{today_d.isoformat()}'"
    ).result_rows[0][0]
    return bi._as_date(nxt) if nxt else today_d


# ---------------------------------------------------------------------------
# (a) 标的列表逐日 walk → staging
# ---------------------------------------------------------------------------
def walk_margin_targets(client, today=None, min_spare: int = 2_000_000) -> None:
    """逐交易日调 get_margincash_stocks / get_marginsec_stocks 落 staging margin_target_raw。

    游标 = staging 的 max(date);整日处理完才落盘(配额检查在日边界),低于 min_spare 优雅停止,重跑续传。
    """
    client.command(RAW_DDL)
    today_d = bi._resolve_today(today)
    have = bi._max_date(client, "margin_target_raw")
    start_d = (have + dt.timedelta(days=1)) if have else dt.date.fromisoformat(MARGIN_START)
    end_d = _next_walk_end(client, today_d)
    days = bi._trade_days(client, start_d.isoformat(), end_d.isoformat())
    if not days:
        print(f"  margin_target_raw: 无待补交易日(已到 {have})")
        return

    buf: list[tuple] = []
    total = 0

    def _flush():
        nonlocal buf, total
        if buf:
            client.insert_df(f"{DATABASE}.margin_target_raw",
                             pd.DataFrame(buf, columns=["type", "stock", "date"]))
            total += len(buf)
            buf = []

    for di, d in enumerate(days, 1):
        if get_query_count()["spare"] < min_spare:
            _flush()
            print(f"  margin_target_raw: 剩余配额不足 {min_spare:,},优雅停止于 {d}"
                  f"(已补到 {days[di - 2] if di > 1 else have},重跑续传)", flush=True)
            return
        iso = d.isoformat()
        for stock in (jqdatasdk.get_margincash_stocks(date=iso) or []):
            buf.append(("cash", str(stock), d))
        for stock in (jqdatasdk.get_marginsec_stocks(date=iso) or []):
            buf.append(("sec", str(stock), d))
        if len(buf) >= FLUSH_ROWS:
            _flush()
        if di % 100 == 0 or di == len(days):
            print(f"  margin_target_raw {d}: {di}/{len(days)} 日 | buf {len(buf):,} | "
                  f"spare {get_query_count()['spare']:,}", flush=True)
    _flush()
    print(f"  margin_target_raw: 本轮 walk 到 {days[-1]}(累计入库 +{total:,} 行)", flush=True)


def build_margin_target_history(client, today=None) -> None:
    """折叠 margin_target_raw → margin_target_history(gaps-and-islands),TRUNCATE+reload,读侧不 FINAL。"""
    client.command(MARGIN_DDL["margin_target_history"])
    df, _ = bi._fold(client, "margin_target_raw", ["type", "stock"], today)
    if df is None:
        print("  build_margin_target_history: staging 为空,先跑 walk_margin_targets")
        return
    out = df[["type", "stock", "start_date", "end_date"]]
    client.command(f"TRUNCATE TABLE {DATABASE}.margin_target_history")
    client.insert_df(f"{DATABASE}.margin_target_history", out)
    n_cash = int((out["type"] == "cash").sum())
    print(f"  margin_target_history: {len(out)} 区间(cash {n_cash} / sec {len(out) - n_cash})"
          f" / {out['stock'].nunique()} 股", flush=True)


# ---------------------------------------------------------------------------
# (b) mtss 逐日全局游标 + 当日标的并集分批
# ---------------------------------------------------------------------------
def _day_universe(client, iso: str) -> list[str]:
    """某交易日的融资∪融券标的(staging 当日去重),作为 get_mtss 的 universe。"""
    return [r[0] for r in client.query(
        f"SELECT DISTINCT stock FROM {DATABASE}.margin_target_raw WHERE date = '{iso}' ORDER BY stock"
    ).result_rows]


def backfill_mtss(client, today=None, batch: int = MTSS_BATCH, min_spare: int = 2_000_000) -> None:
    """逐交易日自 mtss 全局 max(date)+1 补到 staging 已覆盖日(取当日标的并集分批拉 get_mtss)。"""
    client.command(MARGIN_DDL["mtss"])
    today_d = bi._resolve_today(today)
    cov = bi._max_date(client, "margin_target_raw")   # mtss universe 来源,仅补到此
    if cov is None:
        print("  mtss: 标的 staging 为空,先 walk 标的列表")
        return
    end_d = min(cov, today_d)
    have = bi._max_date(client, "mtss")
    start_d = (have + dt.timedelta(days=1)) if have else dt.date.fromisoformat(MARGIN_START)
    days = bi._trade_days(client, start_d.isoformat(), end_d.isoformat())
    if not days:
        print(f"  mtss: 无待补交易日(已到 {have})")
        return

    total = 0
    for di, d in enumerate(days, 1):
        if get_query_count()["spare"] < min_spare:
            print(f"  mtss: 剩余配额不足 {min_spare:,},优雅停止于 {d}"
                  f"(已补到 {days[di - 2] if di > 1 else have},重跑续传)", flush=True)
            return
        iso = d.isoformat()
        universe = _day_universe(client, iso)
        if not universe:
            continue
        parts: list[pd.DataFrame] = []
        for chunk in bi._batches(universe, batch):
            df = jqdatasdk.get_mtss(chunk, start_date=iso, end_date=iso)
            if df is not None and not df.empty:
                parts.append(df)
        if not parts:
            continue
        day = pd.concat(parts, ignore_index=True)
        day["date"] = pd.to_datetime(day["date"]).dt.date
        for c in MTSS_COLS:
            if c not in day.columns:
                day[c] = None
        day["sec_code"] = day["sec_code"].astype(str)
        client.insert_df(f"{DATABASE}.mtss", day[MTSS_COLS])
        total += len(day)
        if di % 100 == 0 or di == len(days):
            print(f"  mtss {d}: 累计 +{total:,} 行 | {di}/{len(days)} 日 | "
                  f"spare {get_query_count()['spare']:,}", flush=True)
    print(f"  mtss: 本轮 +{total:,} 行(补到 {end_d})", flush=True)


# ---------------------------------------------------------------------------
# 唯一对外入口:一站式增量(反复跑即可,断点续传)
# ---------------------------------------------------------------------------
def sync(client, today=None, batch: int = MTSS_BATCH, min_spare: int = 2_000_000) -> None:
    """融资融券一站式更新。标的列表先 walk + 折叠(mtss 当日并集依赖之),再补 mtss。"""
    print("== 融资/融券标的列表逐日 walk(自 max(date) 续传)==")
    walk_margin_targets(client, today=today, min_spare=min_spare)
    print("== 折叠标的区间 ==")
    build_margin_target_history(client, today=today)
    print("== mtss 逐日补齐(当日标的并集分批)==")
    backfill_mtss(client, today=today, batch=batch, min_spare=min_spare)


def main() -> None:
    p = argparse.ArgumentParser(
        description="融资融券增量更新:反复跑即可——历史没补完就续传(只下新交易日),补完后每次只补新日。")
    p.add_argument("--skip-mtss", action="store_true", help="跳过 mtss 逐股明细")
    p.add_argument("--skip-targets", action="store_true", help="跳过融资/融券标的列表 walk + 折叠")
    p.add_argument("--mtss-batch", type=int, default=MTSS_BATCH, help="get_mtss 单次股票数")
    p.add_argument("--min-spare", type=int, default=2_000_000, help="逐日剩余配额低于此值优雅停止")
    args = p.parse_args()

    bk.jq_auth()
    auth_from_env()
    client = get_client()
    if not args.skip_targets:
        walk_margin_targets(client, min_spare=args.min_spare)
        build_margin_target_history(client)
    if not args.skip_mtss:
        backfill_mtss(client, batch=args.mtss_batch, min_spare=args.min_spare)
    print("query count:", get_query_count())
    print("DONE")


if __name__ == "__main__":
    main()
