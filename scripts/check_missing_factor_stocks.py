from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import date
from pathlib import Path

import pyarrow.csv as pacsv


def parse_date(value: str) -> date:
    return date.fromisoformat(value.lstrip("\ufeff"))


def load_stock_windows(path: Path) -> list[tuple[str, date, date]]:
    windows: list[tuple[str, date, date]] = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["type"] != "stock":
                continue
            code = row[""]
            windows.append((code, parse_date(row["start_date"]), parse_date(row["end_date"])))
    return windows


def active_codes_by_date(windows: list[tuple[str, date, date]], dates: set[date]) -> dict[date, set[str]]:
    active: dict[date, set[str]] = {}
    for day in dates:
        active[day] = {code for code, start, end in windows if start <= day <= end}
    return active


def read_codes_by_date(path: Path) -> dict[date, set[str]]:
    table = pacsv.read_csv(
        path,
        read_options=pacsv.ReadOptions(column_names=None),
        convert_options=pacsv.ConvertOptions(include_columns=["time", "code"]),
    )

    times = table["time"].to_pylist()
    codes = table["code"].to_pylist()

    codes_by_date: dict[date, set[str]] = defaultdict(set)
    for raw_day, code in zip(times, codes, strict=True):
        codes_by_date[parse_date(str(raw_day))].add(str(code).lstrip("\ufeff"))
    return dict(codes_by_date)


def check_missing(metadata_path: Path, factors_dir: Path, output_path: Path) -> int:
    windows = load_stock_windows(metadata_path)
    factor_paths = sorted(factors_dir.glob("*.csv"))

    observed_by_file: list[tuple[Path, dict[date, set[str]]]] = []
    all_dates: set[date] = set()
    for path in factor_paths:
        codes_by_date = read_codes_by_date(path)
        observed_by_file.append((path, codes_by_date))
        all_dates.update(codes_by_date)

    active_by_date = active_codes_by_date(windows, all_dates)

    missing_rows = 0
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "file",
                "date",
                "expected_count",
                "actual_count",
                "missing_count",
                "missing_codes",
            ],
        )
        writer.writeheader()
        for path, codes_by_date in observed_by_file:
            for day in sorted(codes_by_date):
                expected = active_by_date[day]
                actual = codes_by_date[day]
                missing = sorted(expected - actual)
                if not missing:
                    continue
                writer.writerow(
                    {
                        "file": str(path),
                        "date": day.isoformat(),
                        "expected_count": len(expected),
                        "actual_count": len(actual),
                        "missing_count": len(missing),
                        "missing_codes": ";".join(missing),
                    }
                )
                missing_rows += 1

    return missing_rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--metadata", type=Path, default=Path("securities_metadata.csv"))
    parser.add_argument("--factors-dir", type=Path, default=Path("data/factors"))
    parser.add_argument("--output", type=Path, default=Path("data/factors_missing_stocks.csv"))
    args = parser.parse_args()

    missing_rows = check_missing(args.metadata, args.factors_dir, args.output)
    print(f"Wrote {missing_rows} rows to {args.output}")


if __name__ == "__main__":
    main()
