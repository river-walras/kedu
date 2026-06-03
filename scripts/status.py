"""数据新鲜度核查:打印每张表的行数、最新数据日期、最近写入时间。

用法(凭证经 .env 注入):
    uv run --env-file .env python scripts/status.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kedu.db import DATABASE, auth_from_env, get_client  # noqa: E402

# (表, 业务日期列);None 表示无业务日期列,仅看行数 / 写入时间
TABLES = [
    ("trade_days", "day"),
    ("securities", None),
    ("bar_1d", "date"),
    ("stock_valuation", "day"),
    ("income_statement", "statDate"),
    ("income_statement_acc", "statDate"),
    ("balance_sheet", "statDate"),
    ("cash_flow_statement", "statDate"),
    ("cash_flow_statement_acc", "statDate"),
    ("financial_indicator", "statDate"),
    ("financial_indicator_acc", "statDate"),
    ("stk_income_statement", "pub_date"),
    ("stk_balance_sheet", "pub_date"),
    ("stk_cashflow_statement", "pub_date"),
    ("stk_fin_forcast", "pub_date"),
    ("stk_audit_opinion", "pub_date"),
    ("stk_report_disclosure", "pub_date"),
    ("industries", None),
    ("industry_history", None),
    ("concepts", None),
    ("concept_history", None),
]


def main() -> None:
    auth_from_env()
    c = get_client()
    print(f"{'table':28s} {'rows':>12s}  {'latest':12s}  last_ingest")
    print("-" * 78)
    for t, col in TABLES:
        try:
            n = c.query(f"SELECT count() FROM {DATABASE}.{t}").result_rows[0][0]
        except Exception as e:
            print(f"{t:28s}  ERR: {e.__class__.__name__}")
            continue
        latest = c.query(f"SELECT max({col}) FROM {DATABASE}.{t}").result_rows[0][0] if col else "-"
        try:
            ing = c.query(f"SELECT max(_ingested_at) FROM {DATABASE}.{t}").result_rows[0][0]
        except Exception:
            ing = "(no _ingested_at)"
        print(f"{t:28s} {n:>12d}  {str(latest):12s}  {ing}")


if __name__ == "__main__":
    main()
