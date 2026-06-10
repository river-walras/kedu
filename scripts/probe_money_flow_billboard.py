"""Probe live jqdatasdk semantics for money_flow_pro and billboard.

This is a temporary investigation script. It intentionally does not touch
ClickHouse; it only calls live jqdatasdk and writes a structured JSON report
that can be used to finalize local schema, query semantics, dtypes and row
ordering.

Usage:
  uv run --env-file .env python scripts/probe_money_flow_billboard.py
  uv run --env-file .env python scripts/probe_money_flow_billboard.py --include-risky
  uv run --env-file .env python scripts/probe_money_flow_billboard.py --json-out /tmp/kedu_probe.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import inspect
import json
import math
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import jqdatasdk  # noqa: E402
from jqdatasdk import get_query_count  # noqa: E402

BASE_FIELDS = [
    "inflow_xl",
    "inflow_l",
    "inflow_m",
    "inflow_s",
    "outflow_xl",
    "outflow_l",
    "outflow_m",
    "outflow_s",
]
NET_FIELDS = ["netflow_xl", "netflow_l", "netflow_m", "netflow_s"]
ALL_FIELDS = [*BASE_FIELDS, *NET_FIELDS]


def _auth() -> None:
    user = os.getenv("JQDATA_USER")
    password = os.getenv("JQDATA_PASSWORD")
    if not user or not password:
        raise SystemExit(
            "Missing JQDATA_USER/JQDATA_PASSWORD. Run with `uv run --env-file .env ...`."
        )
    jqdatasdk.auth(user, password)


def _jsonable(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        value = float(value)
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    if value is pd.NaT:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _sample_type(series: pd.Series) -> str | None:
    for value in series.tolist():
        try:
            if pd.isna(value):
                continue
        except (TypeError, ValueError):
            pass
        return f"{type(value).__module__}.{type(value).__name__}"
    return None


def _head_records(df: pd.DataFrame, n: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in df.head(n).to_dict("records"):
        rows.append({str(k): _jsonable(v) for k, v in row.items()})
    return rows


def _order_matches(df: pd.DataFrame, cols: list[str]) -> bool | None:
    if df.empty or not set(cols).issubset(df.columns):
        return None
    sorted_idx = df.sort_values(cols, kind="stable").index.tolist()
    return sorted_idx == df.index.tolist()


def _duplicate_report(
    df: pd.DataFrame, key_sets: list[list[str]], preview: int
) -> dict[str, Any]:
    report: dict[str, Any] = {}
    for keys in key_sets:
        name = "+".join(keys)
        if not set(keys).issubset(df.columns):
            report[name] = {"available": False}
            continue
        mask = df.duplicated(keys, keep=False)
        dup = df.loc[mask]
        samples: list[dict[str, Any]] = []
        if not dup.empty:
            sizes = (
                df.groupby(keys, dropna=False)
                .size()
                .reset_index(name="n")
                .sort_values("n", ascending=False)
            )
            samples = _head_records(sizes[sizes["n"] > 1], preview)
        report[name] = {
            "available": True,
            "duplicated_rows": int(mask.sum()),
            "duplicated_groups_sample": samples,
        }
    if len(df.columns) > 0:
        mask = df.duplicated(list(df.columns), keep=False)
        report["all_columns"] = {"duplicated_rows": int(mask.sum())}
    return report


def _frame_report(df: Any, preview: int) -> dict[str, Any]:
    if df is None:
        return {"result_type": "None"}
    if not isinstance(df, pd.DataFrame):
        return {"result_type": type(df).__name__, "repr": repr(df)}

    report: dict[str, Any] = {
        "result_type": "DataFrame",
        "shape": [int(df.shape[0]), int(df.shape[1])],
        "columns": [str(c) for c in df.columns],
        "dtypes": {str(c): str(t) for c, t in df.dtypes.items()},
        "sample_types": {str(c): _sample_type(df[c]) for c in df.columns},
        "index": {
            "type": type(df.index).__name__,
            "dtype": str(getattr(df.index, "dtype", "")),
            "name": _jsonable(df.index.name),
        },
        "null_counts": {str(c): int(df[c].isna().sum()) for c in df.columns},
        "head": _head_records(df, preview),
    }
    for col in ("day", "time"):
        if col in df.columns:
            report[f"{col}_sample_values"] = [
                _jsonable(v) for v in df[col].head(preview)
            ]
            report[f"{col}_sample_types"] = [
                f"{type(v).__module__}.{type(v).__name__}"
                for v in df[col].head(preview)
            ]
            if not df.empty:
                report[f"{col}_min"] = _jsonable(df[col].min())
                report[f"{col}_max"] = _jsonable(df[col].max())
                report[f"{col}_head_tail"] = {
                    "head": [_jsonable(v) for v in df[col].head(preview).tolist()],
                    "tail": [_jsonable(v) for v in df[col].tail(preview).tolist()],
                }
    return report


def _exception_report(exc: BaseException) -> dict[str, str]:
    return {
        "type": type(exc).__name__,
        "module": type(exc).__module__,
        "message": str(exc),
        "repr": repr(exc),
    }


def _query_count() -> dict[str, Any]:
    try:
        return dict(get_query_count())
    except Exception as exc:  # noqa: BLE001
        return {"error": _exception_report(exc)}


def _signature(fn: Callable[..., Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for name, param in inspect.signature(fn).parameters.items():
        default = "<empty>" if param.default is inspect._empty else repr(param.default)
        out.append({"name": name, "kind": str(param.kind), "default": default})
    return out


def _call_case(
    label: str,
    fn: Callable[..., Any],
    kwargs: dict[str, Any],
    preview: int,
    extra_analyzer: Callable[[Any], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    print(f"probe: {label}", flush=True)
    case: dict[str, Any] = {
        "label": label,
        "kwargs": {k: _jsonable(v) for k, v in kwargs.items()},
        "query_count_before": _query_count(),
    }
    try:
        result = fn(**kwargs)
    except Exception as exc:  # noqa: BLE001
        case["ok"] = False
        case["exception"] = _exception_report(exc)
    else:
        case["ok"] = True
        case["result"] = _frame_report(result, preview)
        if extra_analyzer is not None:
            case["analysis"] = extra_analyzer(result)
    case["query_count_after"] = _query_count()
    return case


def _money_analysis(result: Any) -> dict[str, Any]:
    if not isinstance(result, pd.DataFrame):
        return {}
    return {
        "order_matches": {
            "code_time": _order_matches(result, ["code", "time"]),
            "time_code": _order_matches(result, ["time", "code"]),
            "time_only": _order_matches(result, ["time"]),
        },
        "has_code_column": "code" in result.columns,
        "has_time_column": "time" in result.columns,
    }


def _billboard_analysis(result: Any, preview: int) -> dict[str, Any]:
    if not isinstance(result, pd.DataFrame):
        return {}
    key_sets = [
        ["code", "day"],
        ["code", "day", "direction", "rank", "sales_depart_name"],
        ["code", "day", "direction", "rank", "abnormal_code", "sales_depart_name"],
        [
            "code",
            "day",
            "direction",
            "rank",
            "abnormal_code",
            "abnormal_name",
            "sales_depart_name",
        ],
    ]
    return {
        "order_matches": {
            "day_code": _order_matches(result, ["day", "code"]),
            "day_code_direction_rank": _order_matches(
                result, ["day", "code", "direction", "rank"]
            ),
            "code_day_direction_rank": _order_matches(
                result, ["code", "day", "direction", "rank"]
            ),
        },
        "duplicates": _duplicate_report(result, key_sets, preview),
    }


def _money_cases(args: argparse.Namespace) -> list[tuple[str, dict[str, Any]]]:
    code = args.money_code
    start = args.money_start
    end = args.money_end
    cases: list[tuple[str, dict[str, Any]]] = [
        (
            "money.single.fields_none.daily",
            {
                "security_list": code,
                "start_date": start,
                "end_date": end,
                "frequency": "daily",
                "fields": None,
                "data_type": "money",
            },
        ),
        (
            "money.multi.fields_none.daily",
            {
                "security_list": [code, args.money_code_2],
                "start_date": start,
                "end_date": end,
                "frequency": "daily",
                "fields": None,
                "data_type": "money",
            },
        ),
        (
            "money.single.base_fields.1d",
            {
                "security_list": code,
                "start_date": start,
                "end_date": end,
                "frequency": "1d",
                "fields": BASE_FIELDS,
                "data_type": "money",
            },
        ),
        (
            "money.single.shuffled_subset_with_netflow",
            {
                "security_list": code,
                "start_date": start,
                "end_date": end,
                "frequency": "daily",
                "fields": ["netflow_s", "inflow_xl", "outflow_s"],
                "data_type": "money",
            },
        ),
        (
            "money.single.netflow_only",
            {
                "security_list": code,
                "start_date": start,
                "end_date": end,
                "frequency": "daily",
                "fields": ["netflow_xl"],
                "data_type": "money",
            },
        ),
        (
            "money.count_trade_end",
            {
                "security_list": code,
                "end_date": end,
                "frequency": "daily",
                "fields": BASE_FIELDS,
                "count": 2,
                "data_type": "money",
            },
        ),
        (
            "money.count_non_trade_end",
            {
                "security_list": code,
                "end_date": args.money_non_trade_end,
                "frequency": "daily",
                "fields": BASE_FIELDS,
                "count": 2,
                "data_type": "money",
            },
        ),
        (
            "money.multi_reversed_codes_count2",
            {
                "security_list": [args.money_code_2, code],
                "end_date": end,
                "frequency": "daily",
                "fields": ["inflow_xl"],
                "count": 2,
                "data_type": "money",
            },
        ),
        (
            "money.start_and_count",
            {
                "security_list": code,
                "start_date": start,
                "end_date": end,
                "frequency": "daily",
                "fields": BASE_FIELDS,
                "count": 2,
                "data_type": "money",
            },
        ),
    ]
    for data_type in ("volume", "deal"):
        cases.append(
            (
                f"money.data_type_{data_type}",
                {
                    "security_list": code,
                    "start_date": start,
                    "end_date": end,
                    "frequency": "daily",
                    "fields": BASE_FIELDS,
                    "data_type": data_type,
                },
            )
        )
    cases.append(
        (
            "money.invalid_data_type",
            {
                "security_list": code,
                "start_date": start,
                "end_date": end,
                "frequency": "daily",
                "fields": BASE_FIELDS,
                "data_type": "invalid",
            },
        )
    )
    if args.include_risky:
        cases.extend(
            [
                (
                    "money.count_zero_stock_limited",
                    {
                        "security_list": code,
                        "end_date": end,
                        "frequency": "daily",
                        "fields": ["inflow_xl"],
                        "count": 0,
                        "data_type": "money",
                    },
                ),
                (
                    "money.minute_probe_may_require_paid_data",
                    {
                        "security_list": code,
                        "end_date": args.money_minute_end,
                        "frequency": "1m",
                        "fields": ["inflow_xl"],
                        "count": 1,
                        "data_type": "money",
                    },
                ),
            ]
        )
    return cases


def _billboard_cases(args: argparse.Namespace) -> list[tuple[str, dict[str, Any]]]:
    code = args.billboard_code
    day = args.billboard_day
    non_trade = args.billboard_non_trade_end
    cases: list[tuple[str, dict[str, Any]]] = [
        (
            "billboard.docs_all_market_count1",
            {"stock_list": None, "end_date": day, "count": 1},
        ),
        (
            "billboard.single_str_count1",
            {"stock_list": code, "end_date": day, "count": 1},
        ),
        (
            "billboard.list_count1",
            {"stock_list": [code, args.billboard_code_2], "end_date": day, "count": 1},
        ),
        (
            "billboard.start_end_same_day",
            {"stock_list": None, "start_date": day, "end_date": day},
        ),
        (
            "billboard.single_start_end_range",
            {
                "stock_list": code,
                "start_date": day,
                "end_date": args.billboard_range_end,
            },
        ),
        (
            "billboard.single_count3_end_range",
            {"stock_list": code, "end_date": args.billboard_range_end, "count": 3},
        ),
        (
            "billboard.single_start_only",
            {"stock_list": code, "start_date": day},
        ),
        (
            "billboard.single_start_count",
            {"stock_list": code, "start_date": day, "count": 1},
        ),
        (
            "billboard.single_start_end_count",
            {"stock_list": code, "start_date": day, "end_date": day, "count": 1},
        ),
        (
            "billboard.empty_list_count1",
            {"stock_list": [], "end_date": day, "count": 1},
        ),
        (
            "billboard.non_trade_end_count1",
            {"stock_list": None, "end_date": non_trade, "count": 1},
        ),
        (
            "billboard.single_no_dates_stock_limited",
            {"stock_list": code},
        ),
    ]
    if args.include_risky:
        cases.append(("billboard.no_dates_all_market_risky", {"stock_list": None}))
        cases.append(
            (
                "billboard.count_zero_all_market_risky",
                {"stock_list": None, "end_date": day, "count": 0},
            )
        )
    return cases


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe live jqdatasdk semantics for get_money_flow_pro and get_billboard_list."
    )
    parser.add_argument(
        "--json-out", default="", help="Optional path for the full JSON report."
    )
    parser.add_argument(
        "--preview", type=int, default=8, help="Rows to include in dataframe previews."
    )
    parser.add_argument(
        "--include-risky",
        action="store_true",
        help="Also run probes that may return larger result sets or paid minute-data errors.",
    )
    parser.add_argument("--skip-money-flow", action="store_true")
    parser.add_argument("--skip-billboard", action="store_true")
    parser.add_argument("--money-code", default="000001.XSHE")
    parser.add_argument("--money-code-2", default="000002.XSHE")
    parser.add_argument("--money-start", default="2024-02-26")
    parser.add_argument("--money-end", default="2024-03-01")
    parser.add_argument("--money-non-trade-end", default="2024-03-03")
    parser.add_argument("--money-minute-end", default="2024-02-28 14:55:00")
    parser.add_argument("--billboard-code", default="688786.XSHG")
    parser.add_argument("--billboard-code-2", default="000001.XSHE")
    parser.add_argument("--billboard-day", default="2022-08-01")
    parser.add_argument("--billboard-range-end", default="2022-08-05")
    parser.add_argument("--billboard-non-trade-end", default="2022-08-07")
    args = parser.parse_args()

    _auth()
    report: dict[str, Any] = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "jqdatasdk_version": getattr(jqdatasdk, "__version__", None),
        "query_count_start": _query_count(),
        "signatures": {
            "get_money_flow_pro": _signature(jqdatasdk.get_money_flow_pro),
            "get_billboard_list": _signature(jqdatasdk.get_billboard_list),
        },
        "money_flow_pro": [],
        "billboard": [],
        "query_count_end": None,
    }

    if not args.skip_money_flow:
        for label, kwargs in _money_cases(args):
            report["money_flow_pro"].append(
                _call_case(
                    label,
                    jqdatasdk.get_money_flow_pro,
                    kwargs,
                    args.preview,
                    _money_analysis,
                )
            )

    if not args.skip_billboard:
        for label, kwargs in _billboard_cases(args):
            report["billboard"].append(
                _call_case(
                    label,
                    jqdatasdk.get_billboard_list,
                    kwargs,
                    args.preview,
                    lambda result: _billboard_analysis(result, args.preview),
                )
            )

    report["query_count_end"] = _query_count()

    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + "\n", encoding="utf-8")
        print(f"wrote report: {path}", flush=True)
    else:
        print(text)


if __name__ == "__main__":
    main()
