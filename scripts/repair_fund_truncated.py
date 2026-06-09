"""确认/修复基金报告期表中「单日 pub_date 超 20 万行被截断」的日期(临时工具)。

按日期分块无法再分单个 pub_date(年报截止日如 03-31 全市场持仓同日披露 >20 万行),
旧回补会静默截断到 20 万。本脚本按 id keyset 分页绕过单次 20 万上限,从 live 拉全该日数据、
与本地 ClickHouse 行数比对,并(默认)重灌补全(ReplacingMergeTree 按 id 去重,幂等)。

用法:
  # 自动扫描可疑日(本地按 pub_date 计数 >= 阈值)并修复
  uv run --env-file .env python scripts/repair_fund_truncated.py --table FUND_PORTFOLIO_STOCK
  # 指定日期、只确认不写库
  uv run --env-file .env python scripts/repair_fund_truncated.py --table FUND_PORTFOLIO_STOCK \
      --dates 2021-03-31,2022-03-31 --check
  # 其它报告期表
  ... --table FUND_PORTFOLIO_BOND   /  --table FUND_FIN_INDICATOR
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jqdatasdk import get_query_count  # noqa: E402

from kedu.db import DATABASE, auth_from_env, get_client  # noqa: E402
from kedu.finance_schema import RUN_QUERY_TABLES, schema_from_model  # noqa: E402
import scripts.backfill_stk as stk  # noqa: E402  (复用 _model/_pull_paged_by_id/_prep)
from scripts.backfill_jq import jq_auth  # noqa: E402


def _col_name(model) -> str:
    """报告期表的单日键:优先 pub_date,退 date/day/end_date。"""
    cols = {c.name for c in model.__table__.columns}
    for c in ("pub_date", "date", "day", "end_date"):
        if c in cols:
            return c
    raise SystemExit(f"{model.__name__}: 找不到可用的日期列")


def main() -> None:
    p = argparse.ArgumentParser(description="确认/修复基金报告期表的单日截断(>20 万行/日)。")
    p.add_argument("--table", default="FUND_PORTFOLIO_STOCK", help="FUND_* 逻辑表名")
    p.add_argument("--dates", help="逗号分隔 YYYY-MM-DD;缺省则自动扫描可疑日")
    p.add_argument("--threshold", type=int, default=195_000, help="自动扫描:本地单日计数 >= 此值视为可疑")
    p.add_argument("--check", action="store_true", help="只确认不写库")
    args = p.parse_args()

    table = args.table.upper()
    if table not in RUN_QUERY_TABLES:
        raise SystemExit(f"未知表 {table};可选:{', '.join(k for k in RUN_QUERY_TABLES if k.startswith('FUND'))}")
    ch = RUN_QUERY_TABLES[table]

    jq_auth()
    auth_from_env()
    client = get_client()
    model = stk._model(table)
    schema = schema_from_model(model)
    colname = _col_name(model)
    col = getattr(model, colname)

    if args.dates:
        days = [dt.date.fromisoformat(d.strip()) for d in args.dates.split(",")]
    else:
        rows = client.query(
            f"SELECT {colname}, count() c FROM {DATABASE}.{ch} "
            f"GROUP BY {colname} HAVING c >= {args.threshold} ORDER BY {colname}").result_rows
        days = [r[0] for r in rows]
        print(f"自动扫描 {ch}.{colname} >= {args.threshold:,} 的可疑日:{len(days)} 个")
    if not days:
        print("无可疑日(或表为空),退出。")
        return

    fixed = 0
    for d in days:
        local_n = client.query(
            f"SELECT count() FROM {DATABASE}.{ch} WHERE {colname} = toDate('{d.isoformat()}')"
        ).result_rows[0][0]
        full = stk._pull_paged_by_id(model, col, d, d)   # live 按 id 分页拉全
        live_n = len(full)
        flag = "截断" if live_n > local_n else "完整"
        action = ""
        if live_n > local_n and not args.check:
            client.insert_df(f"{DATABASE}.{ch}", stk._prep(full, schema))
            action = " -> 已补全"
            fixed += 1
        print(f"  {d} {colname}: 本地 {local_n:,} / live 全量 {live_n:,} [{flag}]{action} "
              f"| spare {get_query_count()['spare']:,}", flush=True)

    if fixed and not args.check:
        client.command(f"OPTIMIZE TABLE {DATABASE}.{ch} FINAL")
        print(f"已补全 {fixed} 个日期并 OPTIMIZE FINAL。")
    print("DONE")


if __name__ == "__main__":
    main()
