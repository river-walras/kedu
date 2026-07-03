"""回补/刷新 get_call_auction 集合竞价 tick 到 ClickHouse。

目标表 jqdata.call_auction(ReplacingMergeTree, (code,time) 每日唯一)。逐票自各自游标
max(time) 之后补到 today:缺失票补全历史、已满票只补新交易日,与 bar_1m 同款自愈式续传。

覆盖 type ∈ (stock, index, 场内基金)。各 type 起始下限:
  - stock 2010-01-01(已实证股票集合竞价无 2010 前数据)
  - index / 场内基金 2017-01-01
单票全历史 <10000 行,一次 get_call_auction 调用即覆盖;剩余配额低于 min_spare 优雅停止,
重跑同命令按各票 max(time) 幂等续传。overlap_days>0 时自 max(time)-overlap 重拉,覆盖
盘后 15:00→24:00 校对期的修正(ReplacingMergeTree 按 _ingested_at 顶旧值)。

初次全量回补与日更增量共用 backfill();update_jqdata.py 以小 overlap 调用它做日更。
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

from kedu.call_auction import CALL_AUCTION_FIELDS  # noqa: E402
from kedu.calendar import _today_cn  # noqa: E402
from kedu.db import DATABASE, auth_from_env, get_client  # noqa: E402
from kedu.finance_schema import FUND_ONEXCHANGE_TYPES  # noqa: E402
from kedu.schema import MARKET_DDL  # noqa: E402
from scripts.backfill_jq import jq_auth  # noqa: E402

STOCK_START = dt.date(2010, 1, 1)
OTHER_START = dt.date(2017, 1, 1)  # 指数 / 场内基金
CA_TYPES = ("stock", "index", *FUND_ONEXCHANGE_TYPES)

# 数值字段(current/volume/money + 20 档);time、code 单独处理。
_NUM_FIELDS = [f for f in CALL_AUCTION_FIELDS if f != "time"]
# 写入列序(与表列名一致,clickhouse-connect 按列名映射)。
_INSERT_COLS = ["time", "code", *_NUM_FIELDS]


def _type_floor(sec_type: str) -> dt.date:
    """按 type 返回集合竞价历史起始下限。"""
    return STOCK_START if sec_type == "stock" else OTHER_START


def _prepare(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """将 live get_call_auction 结果整理为写入 DataFrame(列序 _INSERT_COLS)。"""
    out = pd.DataFrame({"time": pd.to_datetime(df["time"]), "code": str(code)})
    for f in _NUM_FIELDS:
        col = df[f] if f in df.columns else pd.Series([None] * len(df))
        out[f] = pd.to_numeric(col, errors="coerce").astype("float64")
    return out[_INSERT_COLS]


def backfill(
    client,
    end_date: str | dt.date | None = None,
    overlap_days: int = 0,
    min_spare: int = 2_000_000,
) -> None:
    """逐票回补/续传 call_auction 到 end_date(默认 today)。

    overlap_days>0 时自各票 max(time)-overlap 重拉,覆盖盘后校对期修正;=0 只补新交易日。
    """
    client.command(MARKET_DDL["call_auction"])
    today = _today_cn()
    target_end = (
        today
        if end_date is None
        else (
            end_date if isinstance(end_date, dt.date) else pd.Timestamp(end_date).date()
        )
    )

    inlist = ", ".join(f"'{t}'" for t in CA_TYPES)
    secs = client.query(
        f"SELECT instrument_id, type, start_date, end_date FROM {DATABASE}.securities "
        f"WHERE type IN ({inlist}) ORDER BY instrument_id"
    ).result_rows
    maxmap = dict(
        client.query(
            f"SELECT code, max(time) FROM {DATABASE}.call_auction GROUP BY code"
        ).result_rows
    )

    total = pulled = 0
    for ci, (code, sec_type, start_date, end_dt) in enumerate(secs, 1):
        sp = get_query_count()["spare"]
        if sp < min_spare:
            print(
                f"  call_auction: 剩余配额不足 {min_spare:,},优雅停止于第 {ci}/{len(secs)} 只 "
                f"({code});累计 +{total:,} 行,重跑续传。",
                flush=True,
            )
            return
        floor = _type_floor(sec_type)
        code_start = max(start_date or floor, floor)
        code_end = min(end_dt or target_end, target_end)
        have = maxmap.get(code)
        if have is None:
            fill_start = code_start
        elif overlap_days:
            fill_start = have.date() - dt.timedelta(days=overlap_days)
        else:
            fill_start = have.date() + dt.timedelta(days=1)
        fill_start = max(fill_start, code_start)
        if fill_start > code_end:
            continue

        df = jqdatasdk.get_call_auction(
            code, fill_start.isoformat(), code_end.isoformat()
        )
        if df is None or df.empty:
            continue
        prepared = _prepare(df, code)
        # 单票全历史横跨上百个月分区(PARTITION BY toYYYYMM(time)),一次 INSERT block 会超
        # max_partitions_per_insert_block(默认 100)。按年切块写入(每块 ≤12 月分区),与 bar_1m 一致。
        for _, chunk in prepared.groupby(prepared["time"].dt.year, sort=True):
            client.insert_df(f"{DATABASE}.call_auction", chunk)
        total += len(df)
        pulled += 1
        if pulled % 200 == 0:
            print(
                f"  call_auction: {pulled} 票已拉,累计 +{total:,} 行 | "
                f"spare {get_query_count()['spare']:,} | 进度 {ci}/{len(secs)}",
                flush=True,
            )
    print(
        f"  call_auction: 本轮 {pulled} 票写入 +{total:,} 行(补到 {target_end})",
        flush=True,
    )


def main() -> None:
    p = argparse.ArgumentParser(description="回补 get_call_auction 集合竞价数据。")
    p.add_argument("--end-date", default=None)
    p.add_argument(
        "--overlap-days",
        type=int,
        default=0,
        help="自各票 max(time)-N 起重拉,覆盖盘后校对修正(默认 0=仅补新)",
    )
    p.add_argument(
        "--min-spare",
        type=int,
        default=2_000_000,
        help="剩余配额低于此值优雅停止(重跑续传)",
    )
    args = p.parse_args()

    auth_from_env()
    jq_auth()
    client = get_client()
    backfill(
        client,
        end_date=args.end_date,
        overlap_days=args.overlap_days,
        min_spare=args.min_spare,
    )
    print("query count:", get_query_count())
    print("DONE")


if __name__ == "__main__":
    main()
