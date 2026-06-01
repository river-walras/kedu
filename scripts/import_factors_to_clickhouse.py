#!/usr/bin/env python3
from __future__ import annotations

import argparse
import calendar
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import clickhouse_connect
import pandas as pd
from jqdatasdk import auth, get_factor_values, get_query_count, get_trade_days


ROOT = Path(__file__).resolve().parents[1]
DATABASE = "jqdata"
TABLE = "factors"
DEFAULT_CATEGORIES = "basics,emotion,growth,pershare,quality,risk"
DEFAULT_INCREMENTAL_START = date(2010, 1, 1)
JQDATA_FACTOR_VALUE_LIMIT = 200_000
JQDATA_MAX_RETRIES = 5
JQDATA_RETRY_BASE_SECONDS = 2.0


@dataclass(frozen=True)
class StockWindow:
    code: str
    start_date: date
    end_date: date


@dataclass(frozen=True)
class LocalImportResult:
    observed: dict[date, set[str]]
    local_factors: set[str]


def quote_ident(value: str) -> str:
    return "`" + value.replace("`", "``") + "`"


def quote_sql_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def load_env(path: Path = ROOT / ".env") -> None:
    """no-op:环境变量改由 `uv run --env-file .env` 注入,不再用 python-dotenv。"""
    return None


def get_client():
    load_env()
    return clickhouse_connect.get_client(
        host=os.getenv("CLICKHOUSE_HOST", "localhost"),
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_USER", "default"),
        password=os.getenv("CLICKHOUSE_PASSWORD", ""),
    )


def jq_auth() -> None:
    load_env()
    user = os.environ["JQDATA_USER"]
    password = os.environ["JQDATA_PASSWORD"]
    auth(user, password)
    print(f"jqdata query count: {get_query_count()}")


def create_table(client) -> None:
    client.command(f"CREATE DATABASE IF NOT EXISTS {quote_ident(DATABASE)}")
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {quote_ident(DATABASE)}.{quote_ident(TABLE)} (
            factor String,
            date Date,
            instrument_id String,
            value Float64,
            updated_at DateTime DEFAULT now()
        )
        ENGINE = ReplacingMergeTree(updated_at)
        ORDER BY (factor, date, instrument_id)
        """
    )


def parse_date(value: str | date | datetime | None) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).lstrip("\ufeff"))


def parse_required_date(value: str) -> date:
    parsed = parse_date(value)
    if parsed is None:
        raise argparse.ArgumentTypeError("date must not be empty")
    return parsed


def parse_csv_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def load_selected_factors(metadata_path: Path, categories: Iterable[str]) -> list[str]:
    df = pd.read_csv(metadata_path, encoding="utf-8-sig")
    if not {"factor", "category"}.issubset(df.columns):
        raise ValueError(f"{metadata_path} must contain factor and category columns")

    selected_categories = set(categories)
    factors = (
        df.loc[df["category"].isin(selected_categories), "factor"]
        .dropna()
        .astype(str)
        .tolist()
    )
    if not factors:
        raise SystemExit(
            f"no factors found for categories: {', '.join(sorted(selected_categories))}"
        )
    return factors


def load_stock_windows(metadata_path: Path) -> list[StockWindow]:
    df = pd.read_csv(metadata_path, encoding="utf-8-sig")
    code_column = df.columns[0]
    required = {code_column, "start_date", "end_date", "type"}
    if not required.issubset(df.columns):
        raise ValueError(
            f"{metadata_path} must contain code, start_date, end_date, and type columns"
        )

    stocks = df.loc[
        df["type"] == "stock", [code_column, "start_date", "end_date"]
    ].copy()
    stocks["start_date"] = pd.to_datetime(stocks["start_date"]).dt.date
    stocks["end_date"] = pd.to_datetime(stocks["end_date"]).dt.date
    return [
        StockWindow(str(row[code_column]), row["start_date"], row["end_date"])
        for _, row in stocks.iterrows()
    ]


def active_codes(windows: list[StockWindow], day: date) -> list[str]:
    return [
        window.code for window in windows if window.start_date <= day <= window.end_date
    ]


def active_code_sets(
    windows: list[StockWindow], dates: Iterable[date]
) -> dict[date, set[str]]:
    return {day: set(active_codes(windows, day)) for day in dates}


def csv_paths(
    factors_dir: Path, start_date: date | None, end_date: date | None
) -> list[Path]:
    paths = sorted(factors_dir.glob("*.csv"))
    if start_date is None and end_date is None:
        return paths

    selected = []
    for path in paths:
        month_start = date.fromisoformat(f"{path.stem}-01")
        month_end = date(
            month_start.year,
            month_start.month,
            calendar.monthrange(month_start.year, month_start.month)[1],
        )
        if start_date is not None and month_end < start_date:
            continue
        if end_date is not None and month_start > end_date:
            continue
        selected.append(path)
    return selected


def read_csv_subset(path: Path, factors: list[str]) -> pd.DataFrame:
    header = pd.read_csv(path, nrows=0, encoding="utf-8-sig")
    factor_columns = [factor for factor in factors if factor in header.columns]
    columns = ["time", "code", *factor_columns]
    return pd.read_csv(path, usecols=columns, encoding="utf-8-sig")


def read_csv_coverage(path: Path, factors: list[str]) -> tuple[pd.DataFrame, set[str]]:
    header = pd.read_csv(path, nrows=0, encoding="utf-8-sig")
    local_factors = {factor for factor in factors if factor in header.columns}
    df = pd.read_csv(path, usecols=["time", "code"], encoding="utf-8-sig")
    return df, local_factors


def csv_to_long(
    df: pd.DataFrame,
    factors: list[str],
    start_date: date | None,
    end_date: date | None,
) -> pd.DataFrame:
    factor_columns = [factor for factor in factors if factor in df.columns]
    if not factor_columns:
        return empty_factor_frame()

    dates = pd.to_datetime(df["time"]).dt.date
    keep = pd.Series(True, index=df.index)
    if start_date is not None:
        keep &= dates >= start_date
    if end_date is not None:
        keep &= dates <= end_date
    if not keep.any():
        return empty_factor_frame()

    id_df = pd.DataFrame(
        {"date": dates.loc[keep], "code": df.loc[keep, "code"].astype(str)},
        index=df.index[keep],
    )
    value_df = df.loc[keep, factor_columns].copy()
    wide_df = pd.concat([id_df, value_df], axis=1)

    long_df = wide_df.melt(
        id_vars=["date", "code"],
        value_vars=factor_columns,
        var_name="factor",
        value_name="value",
    )
    long_df = long_df.dropna(subset=["value"])
    long_df["value"] = pd.to_numeric(long_df["value"], errors="coerce")
    long_df = long_df.dropna(subset=["value"])
    long_df = long_df.rename(columns={"code": "instrument_id"})
    return long_df.loc[:, ["factor", "date", "instrument_id", "value"]]


def empty_factor_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=["factor", "date", "instrument_id", "value"])


def normalize_insert_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return empty_factor_frame()
    result = df.loc[:, ["factor", "date", "instrument_id", "value"]].copy()
    result["factor"] = result["factor"].astype(str)
    result["instrument_id"] = result["instrument_id"].astype(str)
    result["date"] = pd.to_datetime(result["date"]).dt.date
    result["value"] = pd.to_numeric(result["value"], errors="coerce")
    result = result.dropna(subset=["value"])
    return result.drop_duplicates(subset=["factor", "date", "instrument_id"])


def chunked(items: list, size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def insert_rows(client, df: pd.DataFrame, batch_size: int, label: str) -> int:
    df = normalize_insert_frame(df)
    if df.empty:
        print(f"{label}: no non-null rows")
        return 0

    inserted = 0
    for batch_start in range(0, len(df), batch_size):
        batch = df.iloc[batch_start : batch_start + batch_size].copy()
        batch["updated_at"] = datetime.now()
        client.insert_df(f"{DATABASE}.{TABLE}", batch)
        inserted += len(batch)

    print(f"{label}: inserted={inserted}")
    return inserted


def import_local_csv(
    client,
    path: Path,
    factors: list[str],
    start_date: date | None,
    end_date: date | None,
    batch_size: int,
) -> LocalImportResult:
    observed: dict[date, set[str]] = defaultdict(set)
    df = read_csv_subset(path, factors)
    local_factors = {factor for factor in factors if factor in df.columns}
    observe_codes_by_date(df, observed, start_date, end_date)
    insert_rows(
        client,
        csv_to_long(df, factors, start_date, end_date),
        batch_size,
        f"csv {path.name}",
    )
    return LocalImportResult(dict(observed), local_factors)


def scan_local_csv(
    path: Path,
    factors: list[str],
    start_date: date | None,
    end_date: date | None,
) -> LocalImportResult:
    observed: dict[date, set[str]] = defaultdict(set)
    df, local_factors = read_csv_coverage(path, factors)
    observe_codes_by_date(df, observed, start_date, end_date)
    print(f"scan {path.name}: dates={len(observed)}")
    return LocalImportResult(dict(observed), local_factors)


def observe_codes_by_date(
    df: pd.DataFrame,
    observed: dict[date, set[str]],
    start_date: date | None,
    end_date: date | None,
) -> None:
    dates = pd.to_datetime(df["time"]).dt.date
    keep = pd.Series(True, index=df.index)
    if start_date is not None:
        keep &= dates >= start_date
    if end_date is not None:
        keep &= dates <= end_date
    df = df.loc[keep]
    dates = dates.loc[keep]
    for day, codes in df["code"].astype(str).groupby(dates):
        observed[day].update(codes)


def import_local_csv_worker(
    path: Path,
    factors: list[str],
    start_date: date | None,
    end_date: date | None,
    batch_size: int,
) -> LocalImportResult:
    client = get_client()
    return import_local_csv(client, path, factors, start_date, end_date, batch_size)


def scan_local_csv_worker(
    path: Path,
    factors: list[str],
    start_date: date | None,
    end_date: date | None,
) -> LocalImportResult:
    return scan_local_csv(path, factors, start_date, end_date)


def import_local_csvs(
    client,
    paths: list[Path],
    factors: list[str],
    start_date: date | None,
    end_date: date | None,
    batch_size: int,
    workers: int,
    skip_local: bool,
) -> tuple[dict[date, set[str]], set[str]]:
    observed: dict[date, set[str]] = defaultdict(set)
    local_factors: set[str] = set()

    if workers == 1:
        if skip_local:
            results = [
                scan_local_csv(path, factors, start_date, end_date) for path in paths
            ]
        else:
            results = [
                import_local_csv(
                    client, path, factors, start_date, end_date, batch_size
                )
                for path in paths
            ]
    else:
        results = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            if skip_local:
                futures = [
                    executor.submit(
                        scan_local_csv_worker, path, factors, start_date, end_date
                    )
                    for path in paths
                ]
            else:
                futures = [
                    executor.submit(
                        import_local_csv_worker,
                        path,
                        factors,
                        start_date,
                        end_date,
                        batch_size,
                    )
                    for path in paths
                ]
            for future in as_completed(futures):
                results.append(future.result())

    for result in results:
        local_factors.update(result.local_factors)
        for day, codes in result.observed.items():
            observed[day].update(codes)
    return dict(observed), local_factors


def latest_local_date(paths: list[Path]) -> date | None:
    latest: date | None = None
    for path in paths:
        df = pd.read_csv(path, usecols=["time"], encoding="utf-8-sig")
        if df.empty:
            continue
        max_day = pd.to_datetime(df["time"]).dt.date.max()
        if latest is None or max_day > latest:
            latest = max_day
    return latest


def max_factor_date(client, factor: str) -> date | None:
    row = client.query(
        f"""
        SELECT max(date)
        FROM {quote_ident(DATABASE)}.{quote_ident(TABLE)}
        WHERE factor = {quote_sql_string(factor)}
        """
    ).first_row
    value = row[0] if row else None
    return parse_date(value)


def latest_trade_day(today: date | None = None) -> date:
    end = today or date.today()
    days = get_trade_days(end_date=end, count=1)
    if len(days) == 0:
        raise RuntimeError("JQData returned no latest trade day")
    return parse_date(days[-1])  # type: ignore[arg-type]


def month_windows(start: date, end: date):
    cursor = start
    while cursor <= end:
        month_end = date(
            cursor.year, cursor.month, calendar.monthrange(cursor.year, cursor.month)[1]
        )
        yield cursor, min(month_end, end)
        cursor = month_end + timedelta(days=1)


def trade_day_count(start: date, end: date) -> int:
    if start == end:
        return 1
    days = get_trade_days(start_date=start, end_date=end)
    return len(days)


def factor_batches(
    factors: list[str],
    securities_count: int,
    start_date: date,
    end_date: date,
) -> list[list[str]]:
    days = trade_day_count(start_date, end_date)
    if securities_count <= 0 or days <= 0:
        return []
    batch_size = max(1, JQDATA_FACTOR_VALUE_LIMIT // (securities_count * days))
    return list(chunked(factors, batch_size))


def ensure_query_quota() -> None:
    count = get_query_count()
    spare = None
    if isinstance(count, dict):
        if "spare" in count:
            spare = count["spare"]
        elif {"limit", "total"}.issubset(count):
            spare = count["limit"] - count["total"]
    if spare is not None and spare <= 0:
        raise RuntimeError(f"JQData query quota exhausted: {count}")


def factor_values_to_long(result: dict, requested_factors: list[str]) -> pd.DataFrame:
    frames = []
    for factor in requested_factors:
        factor_df = result.get(factor)
        if factor_df is None or factor_df.empty:
            continue
        long_df = factor_df.stack().reset_index()
        long_df.columns = ["date", "instrument_id", "value"]
        long_df = long_df.dropna(subset=["value"])
        if long_df.empty:
            continue
        long_df["factor"] = factor
        frames.append(long_df.loc[:, ["factor", "date", "instrument_id", "value"]])
    if not frames:
        return empty_factor_frame()
    return pd.concat(frames, ignore_index=True)


def download_factor_values(
    securities: list[str],
    factors: list[str],
    start_date: date,
    end_date: date,
) -> pd.DataFrame:
    if not securities or not factors or start_date > end_date:
        return empty_factor_frame()
    result = None
    for attempt in range(1, JQDATA_MAX_RETRIES + 1):
        try:
            ensure_query_quota()
            result = get_factor_values(
                securities=securities,
                factors=factors,
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
            )
            break
        except Exception as exc:
            if attempt == JQDATA_MAX_RETRIES:
                raise
            sleep_seconds = JQDATA_RETRY_BASE_SECONDS * 2 ** (attempt - 1)
            print(
                "retry jqdata "
                f"{start_date}~{end_date} codes={len(securities)} factors={len(factors)} "
                f"attempt={attempt}/{JQDATA_MAX_RETRIES}: {exc}"
            )
            time.sleep(sleep_seconds)
    if result is None:
        return empty_factor_frame()
    return factor_values_to_long(result, factors)


def fill_missing_from_csv_range(
    client,
    observed_by_date: dict[date, set[str]],
    local_factors: set[str],
    stock_windows: list[StockWindow],
    factors: list[str],
    batch_size: int,
) -> int:
    if not observed_by_date:
        return 0

    inserted = 0
    active_by_date = active_code_sets(stock_windows, observed_by_date)
    known_local_factors = [factor for factor in factors if factor in local_factors]
    jq_only_factors = [factor for factor in factors if factor not in local_factors]
    for day in sorted(observed_by_date):
        active = sorted(active_by_date[day])
        if not active:
            continue
        missing_codes = sorted(set(active) - observed_by_date[day])
        pending = [
            ("missing", missing_codes, known_local_factors),
            ("jq-only", active, jq_only_factors),
        ]
        for label, codes, pending_factors in pending:
            if not codes:
                continue
            for code_batch in chunked(codes, 800):
                for factor_batch in factor_batches(
                    pending_factors, len(code_batch), day, day
                ):
                    data = download_factor_values(code_batch, factor_batch, day, day)
                    inserted += insert_rows(
                        client,
                        data,
                        batch_size,
                        f"fill {label} {day} codes={len(code_batch)} factors={len(factor_batch)}",
                    )
    return inserted


def incremental_update(
    client,
    stock_windows: list[StockWindow],
    factors: list[str],
    start_date: date | None,
    end_date: date | None,
    batch_size: int,
    lower_bound: date | None = None,
) -> int:
    target_end = end_date or latest_trade_day()
    inserted = 0
    for factor in factors:
        current_max = max_factor_date(client, factor)
        effective_start = (
            current_max + timedelta(days=1)
            if current_max
            else start_date or DEFAULT_INCREMENTAL_START
        )
        if lower_bound is not None and effective_start < lower_bound:
            effective_start = lower_bound
        if effective_start > target_end:
            print(f"incremental {factor}: up to date through {current_max}")
            continue

        for window_start, window_end in month_windows(effective_start, target_end):
            codes = sorted(
                {
                    code
                    for day in pd.date_range(window_start, window_end).date
                    for code in active_codes(stock_windows, day)
                }
            )
            for code_batch in chunked(codes, 800):
                data = download_factor_values(
                    code_batch, [factor], window_start, window_end
                )
                inserted += insert_rows(
                    client,
                    data,
                    batch_size,
                    f"incremental {factor} {window_start}~{window_end}",
                )
    return inserted


def full_mode(
    client,
    args,
    factors: list[str],
    stock_windows: list[StockWindow],
) -> None:
    paths = csv_paths(args.factors_dir, args.start_date, args.end_date)
    observed, local_factors = import_local_csvs(
        client,
        paths,
        factors,
        args.start_date,
        args.end_date,
        args.batch_size,
        args.workers,
        args.skip_local,
    )
    fill_missing_from_csv_range(
        client, observed, local_factors, stock_windows, factors, args.batch_size
    )


def auto_mode(
    client,
    args,
    factors: list[str],
    stock_windows: list[StockWindow],
) -> None:
    paths = csv_paths(args.factors_dir, args.start_date, args.end_date)
    observed, local_factors = import_local_csvs(
        client,
        paths,
        factors,
        args.start_date,
        args.end_date,
        args.batch_size,
        args.workers,
        args.skip_local,
    )
    fill_missing_from_csv_range(
        client, observed, local_factors, stock_windows, factors, args.batch_size
    )

    local_latest = latest_local_date(paths)
    lower_bound = local_latest + timedelta(days=1) if local_latest else args.start_date
    if lower_bound is None:
        lower_bound = DEFAULT_INCREMENTAL_START
    incremental_update(
        client,
        stock_windows,
        factors,
        args.start_date,
        args.end_date,
        args.batch_size,
        lower_bound=lower_bound,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import and update JQData factors into ClickHouse."
    )
    parser.add_argument(
        "--mode", choices=["auto", "full", "incremental"], default="auto"
    )
    parser.add_argument(
        "--categories",
        default=DEFAULT_CATEGORIES,
        help="Comma-separated factor categories.",
    )
    parser.add_argument("--start-date", type=parse_required_date)
    parser.add_argument("--end-date", type=parse_required_date)
    parser.add_argument("--factors-dir", type=Path, default=Path("data/factors"))
    parser.add_argument(
        "--factors-metadata", type=Path, default=Path("factors_metadata.csv")
    )
    parser.add_argument(
        "--securities-metadata", type=Path, default=Path("securities_metadata.csv")
    )
    parser.add_argument("--batch-size", type=int, default=50_000)
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Parallel workers for local CSV imports. Defaults to 1.",
    )
    parser.add_argument(
        "--skip-local",
        action="store_true",
        help="Skip local CSV inserts, but still scan CSV coverage for fill and incremental steps.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.start_date and args.end_date and args.start_date > args.end_date:
        raise SystemExit("--start-date must be <= --end-date")
    if args.batch_size <= 0:
        raise SystemExit("--batch-size must be positive")
    if args.workers <= 0:
        raise SystemExit("--workers must be positive")

    categories = parse_csv_list(args.categories)
    factors = load_selected_factors(args.factors_metadata, categories)
    stock_windows = load_stock_windows(args.securities_metadata)

    client = get_client()
    create_table(client)

    if args.mode in {"auto", "full"}:
        print(f"selected factors={len(factors)}, categories={','.join(categories)}")

    if args.mode == "incremental":
        jq_auth()
        incremental_update(
            client,
            stock_windows,
            factors,
            args.start_date,
            args.end_date,
            args.batch_size,
        )
    elif args.mode == "full":
        jq_auth()
        full_mode(client, args, factors, stock_windows)
    elif args.mode == "auto":
        jq_auth()
        auto_mode(client, args, factors, stock_windows)


if __name__ == "__main__":
    main()
