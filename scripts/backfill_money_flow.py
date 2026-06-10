"""回补/刷新 get_money_flow_pro 日频资金流向到 ClickHouse。

目标表: jqdata.money_flow_pro，长表键为 (time, code, data_type)。
三种 data_type(money/volume/deal) 分开拉取；每行写入 8 个基础字段与 4 个
netflow 派生字段。分钟资金流向属于聚宽付费模块，本脚本只处理 daily/1d。

默认按每个 (code, data_type) 的 max(time) 断点续传；--refresh 则重拉指定窗口。
日常增量由 scripts/update_jqdata.py 回拉最近若干交易日覆盖修正。
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jqdatasdk  # noqa: E402
from jqdatasdk import get_query_count  # noqa: E402

from kedu.db import DATABASE, auth_from_env, get_client  # noqa: E402
from kedu.money_flow import ALL_FIELDS, BASE_FIELDS, DATA_TYPES  # noqa: E402
from kedu.schema import MARKET_DDL  # noqa: E402
from scripts.backfill_jq import jq_auth  # noqa: E402

START = dt.date(2015, 1, 1)
BATCH = 200
WINDOW_DAYS = 30


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


def _q(value: object) -> str:
    return "'" + str(value).replace("'", "\\'") + "'"


def _batches(seq: list[str], size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _stock_codes(client) -> list[str]:
    return [
        r[0]
        for r in client.query(
            f"SELECT instrument_id FROM {DATABASE}.securities "
            f"WHERE type = 'stock' ORDER BY instrument_id"
        ).result_rows
    ]


def _max_by_code(client, data_type: str) -> dict[str, dt.date]:
    rows = client.query(
        f"SELECT code, max(toDate(time)) FROM {DATABASE}.money_flow_pro "
        f"WHERE data_type = {_q(data_type)} GROUP BY code"
    ).result_rows
    return {code: day for code, day in rows if day is not None}


def _trade_days(start: dt.date, end: dt.date) -> list[dt.date]:
    days = jqdatasdk.get_trade_days(
        start_date=start.isoformat(), end_date=end.isoformat()
    )
    return [pd.Timestamp(d).date() for d in days]


def _date_windows(
    start: dt.date, end: dt.date, window_days: int
) -> list[tuple[dt.date, dt.date]]:
    days = _trade_days(start, end)
    if not days:
        return []
    out = []
    for i in range(0, len(days), window_days):
        chunk = days[i : i + window_days]
        out.append((chunk[0], chunk[-1]))
    return out


def _prepare(df: pd.DataFrame, data_type: str) -> pd.DataFrame:
    if "time" not in df.columns:
        df = df.reset_index().rename(columns={"index": "time"})
    out = df.copy()
    out["time"] = pd.to_datetime(out["time"]).dt.tz_localize(None)
    out["code"] = out["code"].astype(str)
    out["data_type"] = data_type
    for c in BASE_FIELDS:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    for suffix in ("xl", "l", "m", "s"):
        out[f"netflow_{suffix}"] = out[f"inflow_{suffix}"] - out[f"outflow_{suffix}"]
    for c in ALL_FIELDS:
        out[c] = pd.to_numeric(out[c], errors="coerce").astype("float64")
    return out[["time", "code", "data_type", *ALL_FIELDS]]


def _pull(
    codes: list[str], start: dt.date, end: dt.date, data_type: str
) -> pd.DataFrame | None:
    return jqdatasdk.get_money_flow_pro(
        codes,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        frequency="daily",
        fields=BASE_FIELDS,
        data_type=data_type,
    )


def backfill(
    client,
    start_date: str | dt.date | None = None,
    end_date: str | dt.date | None = None,
    data_types: list[str] | None = None,
    batch: int = BATCH,
    window_days: int = WINDOW_DAYS,
    refresh: bool = False,
    min_spare: int = 2_000_000,
) -> None:
    """回补 money_flow_pro；refresh=True 时重拉窗口，否则按每票 max(time) 续传。"""
    client.command(MARKET_DDL["money_flow_pro"])
    start = max(_to_date(start_date) or START, START)
    end = _to_date(end_date) or _today_cn()
    if end < start:
        print(f"money_flow_pro: 空窗口 {start}..{end}")
        return

    dtypes = data_types or list(DATA_TYPES)
    bad = [d for d in dtypes if d not in DATA_TYPES]
    if bad:
        raise ValueError(f"bad data_type: {bad}")

    codes = _stock_codes(client)
    if not codes:
        print("money_flow_pro: securities 中无 stock，请先同步 securities")
        return

    total_rows = 0
    for data_type in dtypes:
        have = {} if refresh else _max_by_code(client, data_type)
        grouped: dict[dt.date, list[str]] = defaultdict(list)
        for code in codes:
            code_start = start
            if code in have:
                code_start = max(start, have[code] + dt.timedelta(days=1))
            if code_start <= end:
                grouped[code_start].append(code)

        todo = sum(len(v) for v in grouped.values())
        print(
            f"money_flow_pro[{data_type}]: 待拉 {todo}/{len(codes)} 票 "
            f"({start}..{end}, refresh={refresh})",
            flush=True,
        )
        for code_start in sorted(grouped):
            windows = _date_windows(code_start, end, window_days)
            if not windows:
                continue
            for win_start, win_end in windows:
                for chunk in _batches(grouped[code_start], batch):
                    if get_query_count()["spare"] < min_spare:
                        print(
                            f"money_flow_pro[{data_type}]: 剩余配额不足 {min_spare:,},"
                            f"停止于 {win_start} {chunk[0]}(重跑续传)",
                            flush=True,
                        )
                        return
                    df = _pull(chunk, win_start, win_end, data_type)
                    if df is None or df.empty:
                        continue
                    out = _prepare(df, data_type)
                    client.insert_df(f"{DATABASE}.money_flow_pro", out)
                    total_rows += len(out)
                print(
                    f"  {data_type} {win_start}..{win_end}: 累计 +{total_rows:,} 行 | "
                    f"spare {get_query_count()['spare']:,}",
                    flush=True,
                )
    print(f"money_flow_pro: 本轮写入 +{total_rows:,} 行", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description="回补 get_money_flow_pro 日频资金流向。")
    p.add_argument("--start-date", default=START.isoformat())
    p.add_argument("--end-date", default=None)
    p.add_argument(
        "--data-types", default="money,volume,deal", help="逗号分隔: money,volume,deal"
    )
    p.add_argument("--batch", type=int, default=BATCH, help="每次请求股票数")
    p.add_argument(
        "--window-days", type=int, default=WINDOW_DAYS, help="每次请求交易日窗口"
    )
    p.add_argument("--refresh", action="store_true", help="重拉指定窗口")
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
        data_types=[x.strip() for x in args.data_types.split(",") if x.strip()],
        batch=args.batch,
        window_days=args.window_days,
        refresh=args.refresh,
        min_spare=args.min_spare,
    )
    print("query count:", get_query_count())
    print("DONE")


if __name__ == "__main__":
    main()
