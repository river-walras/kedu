"""回补/刷新 get_billboard_list 龙虎榜数据到 ClickHouse。

目标表: jqdata.billboard，普通 MergeTree。每个交易日从 live 拉全市场
get_billboard_list(stock_list=None, start_date=d, end_date=d)，写入前先按 day 删除，
再插入该日完整结果。_position 保存 live 行号，读侧据此还原同日内部顺序。

默认跳过本地已有数据的日期；--refresh 则按日替换指定窗口，可覆盖盘后 20:00/22:00 修正。
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

from kedu.billboard import BILLBOARD_COLUMNS  # noqa: E402
from kedu.db import DATABASE, auth_from_env, get_client  # noqa: E402
from kedu.schema import MARKET_DDL  # noqa: E402
from scripts.backfill_jq import jq_auth  # noqa: E402

START = dt.date(2010, 1, 1)
FLOAT_COLUMNS = [
    "buy_value",
    "buy_rate",
    "sell_value",
    "sell_rate",
    "total_value",
    "net_value",
    "amount",
]


def _to_date(x: str | dt.date | None) -> dt.date | None:
    if x is None:
        return None
    if isinstance(x, dt.datetime):
        return x.date()
    if isinstance(x, dt.date):
        return x
    return pd.Timestamp(x).date()


def _today_cn() -> dt.date:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()


def _trade_days(start: dt.date, end: dt.date) -> list[dt.date]:
    days = jqdatasdk.get_trade_days(
        start_date=start.isoformat(), end_date=end.isoformat()
    )
    return [pd.Timestamp(d).date() for d in days]


def _existing_days(client) -> set[dt.date]:
    rows = client.query(f"SELECT DISTINCT day FROM {DATABASE}.billboard").result_rows
    return {r[0] for r in rows}


def _delete_day(client, day: dt.date) -> None:
    client.command(
        f"ALTER TABLE {DATABASE}.billboard DELETE WHERE day = toDate('{day.isoformat()}') "
        "SETTINGS mutations_sync = 1"
    )


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in BILLBOARD_COLUMNS:
        if c not in out.columns:
            out[c] = None
    out["code"] = out["code"].astype(str)
    out["day"] = pd.to_datetime(out["day"]).dt.date
    out["direction"] = out["direction"].astype(object)
    out["abnormal_name"] = out["abnormal_name"].astype(object)
    out["sales_depart_name"] = out["sales_depart_name"].astype(object)
    out["rank"] = pd.to_numeric(out["rank"], errors="raise").astype("int64")
    out["abnormal_code"] = pd.to_numeric(out["abnormal_code"], errors="raise").astype(
        "int64"
    )
    for c in FLOAT_COLUMNS:
        out[c] = pd.to_numeric(out[c], errors="coerce").astype("float64")
    out["_position"] = range(len(out))
    return out[[*BILLBOARD_COLUMNS, "_position"]]


def backfill(
    client,
    start_date: str | dt.date | None = None,
    end_date: str | dt.date | None = None,
    refresh: bool = False,
    min_spare: int = 2_000_000,
) -> None:
    """回补 billboard；refresh=True 时按日 DELETE+INSERT 覆盖指定窗口。"""
    client.command(MARKET_DDL["billboard"])
    start = max(_to_date(start_date) or START, START)
    end = _to_date(end_date) or _today_cn()
    if end < start:
        print(f"billboard: 空窗口 {start}..{end}")
        return

    days = _trade_days(start, end)
    done = set() if refresh else _existing_days(client)
    todo = [d for d in days if refresh or d not in done]
    print(
        f"billboard: 待拉 {len(todo)}/{len(days)} 日({start}..{end}, refresh={refresh})"
    )

    rows = 0
    for i, day in enumerate(todo, 1):
        if get_query_count()["spare"] < min_spare:
            print(
                f"billboard: 剩余配额不足 {min_spare:,},停止于 {day}(重跑续传)",
                flush=True,
            )
            return
        iso = day.isoformat()
        df = jqdatasdk.get_billboard_list(stock_list=None, start_date=iso, end_date=iso)
        _delete_day(client, day)
        if df is not None and not df.empty:
            out = _prepare(df)
            client.insert_df(f"{DATABASE}.billboard", out)
            rows += len(out)
        if i % 20 == 0 or i == len(todo):
            print(
                f"  billboard {day}: {i}/{len(todo)} 日,累计 +{rows:,} 行 | "
                f"spare {get_query_count()['spare']:,}",
                flush=True,
            )
    print(f"billboard: 本轮写入 +{rows:,} 行", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description="回补 get_billboard_list 龙虎榜数据。")
    p.add_argument("--start-date", default=START.isoformat())
    p.add_argument("--end-date", default=None)
    p.add_argument("--refresh", action="store_true", help="按日替换指定窗口")
    p.add_argument(
        "--min-spare", type=int, default=2_000_000, help="剩余配额低于此值优雅停止"
    )
    args = p.parse_args()

    auth_from_env()
    jq_auth()
    client = get_client()
    backfill(
        client,
        start_date=args.start_date,
        end_date=args.end_date,
        refresh=args.refresh,
        min_spare=args.min_spare,
    )
    print("query count:", get_query_count())
    print("DONE")


if __name__ == "__main__":
    main()
