"""回补/刷新 get_locked_shares 派生数据集到 ClickHouse `jqdata.locked_shares`。

get_locked_shares 是聚宽独立维护的解禁数据集(含未来「预计」解禁行、num 经送转调整),
**不可**由 STK_LIMITED_SHARES_* 等表重算。按本项目派生接口惯例(同 is_st),直接自 live
get_locked_shares 全量灌入,本地接口仅按 code + 交易日窗口过滤,逐行与 live 一致。

每票一条宽窗口 [2005-01-01, 远期] 即返回其全部(历史+预计)解禁行,按 BATCH 只数批量请求。
幂等:ReplacingMergeTree(code, day) 去重。
  - 默认:跳过表中已有数据的票(断点续传/初次回补)。
  - --refresh:重拉所有票(日更刷新未来预计行随股本变化的 rate)。
剩余配额低于 --min-spare 时优雅停止,重跑同命令续传。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402
from jqdatasdk import get_locked_shares, get_query_count  # noqa: E402

from kedu.db import DATABASE, auth_from_env, get_client  # noqa: E402
from kedu.schema import MARKET_DDL  # noqa: E402
from scripts.backfill_jq import jq_auth  # noqa: E402

FUTURE_END = "2035-12-31"   # 远期上界,确保一并取到未来「预计」解禁行
START = "2005-01-01"


def _stock_codes(client) -> list[str]:
    return [r[0] for r in client.query(
        f"SELECT instrument_id FROM {DATABASE}.securities "
        f"WHERE type='stock' ORDER BY instrument_id").result_rows]


def _have_codes(client) -> set[str]:
    return {r[0] for r in client.query(
        f"SELECT DISTINCT code FROM {DATABASE}.locked_shares").result_rows}


def backfill(client, batch: int = 50, refresh: bool = False,
             min_spare: int = 2_000_000) -> None:
    client.command(MARKET_DDL["locked_shares"])
    codes = _stock_codes(client)
    have = set() if refresh else _have_codes(client)
    todo = [c for c in codes if c not in have]
    print(f"locked_shares: 全 {len(codes)} 票,待拉 {len(todo)}(已有 {len(codes) - len(todo)})")

    pulled = rows = 0
    for i in range(0, len(todo), batch):
        if get_query_count()["spare"] < min_spare:
            print(f"  剩余配额不足 {min_spare},优雅停止于第 {i} 只(重跑续传)")
            break
        chunk = todo[i:i + batch]
        df = get_locked_shares(stock_list=chunk, start_date=START, end_date=FUTURE_END)
        pulled += len(chunk)
        if df is None or df.empty:
            continue
        out = pd.DataFrame({
            "code": df["code"].astype(str),
            "day": pd.to_datetime(df["day"]).dt.date,
            "num": pd.to_numeric(df["num"], errors="coerce").astype("float64"),
            "rate1": pd.to_numeric(df["rate1"], errors="coerce").astype("float64"),
            "rate2": pd.to_numeric(df["rate2"], errors="coerce").astype("float64"),
        })
        client.insert_df(f"{DATABASE}.locked_shares", out)
        rows += len(out)
        if (i // batch) % 20 == 0:
            print(f"  {pulled}/{len(todo)} 票,累计 {rows} 行 | spare: {get_query_count()['spare']}")
    client.command(f"OPTIMIZE TABLE {DATABASE}.locked_shares FINAL")
    print(f"locked_shares: 拉取 {pulled} 票,写入 {rows} 行")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--batch", type=int, default=50, help="每次请求的股票只数")
    p.add_argument("--refresh", action="store_true", help="重拉所有票(日更刷新未来预计行)")
    p.add_argument("--min-spare", type=int, default=2_000_000, help="低于此剩余配额优雅停止")
    args = p.parse_args()

    auth_from_env()
    jq_auth()
    client = get_client()
    backfill(client, batch=args.batch, refresh=args.refresh, min_spare=args.min_spare)


if __name__ == "__main__":
    main()
