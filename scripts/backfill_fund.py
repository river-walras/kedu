"""基金数据独立回补到 ClickHouse(读侧见 kedu.finance / get_all_securities / get_extras / get_price)。

覆盖三块,反复跑即可断点续传:
  1) 10 张 FUND_* finance.run_query 底表  ← stk.sync_all(tables=FUND_SYNC_TABLES)
       空表→全量(from 2005,按分块列年份分块;FUND_NET_VALUE/FUND_SHARE_DAILY 按月+二分),
       有数据→水位增量。ReplacingMergeTree 幂等。
  2) securities(股票+指数+**场内基金**)   ← update_jqdata.update_securities(全量重载,保留基金细分 type)
  3) 场内基金行情 bar_1d / bar_1m         ← rebuild_from_jq.rebuild_bars / rebuild_bars_1m
       **逐票断点续传**:已在 bar 表中的票(全部股票/指数)自动跳过,仅拉新增的场内基金票,
       故不会重拉股票。fq=None 原始价 + 日线 fq='post' 因子,与股票同口径(基金后复权基准一致)。

分钟线最重最耗配额、放最后;剩余配额低于 --min-spare 优雅停止,重跑续传(可跨多天)。

用法(分阶段,互不依赖):
  # 1) 种子 finance 表 + securities(快)
  uv run --env-file .env python scripts/backfill_fund.py --skip-bars --skip-bars-1m
  # 2) 场内基金日线(securities 须先就绪)
  uv run --env-file .env python scripts/backfill_fund.py --skip-finance --skip-bars-1m
  # 3) 场内基金分钟线(配额重,可跨天续传)
  uv run --env-file .env python scripts/backfill_fund.py --skip-finance --skip-bars
日常增量由 update_jqdata.py 一并更新(--skip-fund-finance 仅跳过 10 张 finance 表)。
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jqdatasdk import get_query_count  # noqa: E402

from kedu.db import DATABASE, auth_from_env, get_client  # noqa: E402
from kedu.finance_schema import FUND_SYNC_TABLES  # noqa: E402
import scripts.backfill_jq as bk  # noqa: E402  (jq_auth)
import scripts.backfill_stk as stk  # noqa: E402  (sync_all 同步引擎)
import scripts.rebuild_from_jq as rb  # noqa: E402  (逐票断点续传的 bar 重建)
import scripts.update_jqdata as uj  # noqa: E402  (update_securities)


def main() -> None:
    p = argparse.ArgumentParser(
        description="基金数据独立回补:finance 表 + securities + 场内基金 bar。反复跑即可断点续传。")
    p.add_argument("--tables", help="逗号分隔的 FUND 逻辑表名,缺省=全部 10 张")
    p.add_argument("--full", action="store_true", help="finance 表强制全量(按年跳过已有、可续传)")
    p.add_argument("--start-year", type=int, default=1998,
                   help="finance 表全量起始年(中国公募基金始于 1998;老基金的分红/组合/持仓等报告"
                        "早于 2005,故下探到 1998;净值/份额/货基日报等无 pre-2005 数据,空年仅多几次空查询)")
    p.add_argument("--end-year", type=int, default=dt.date.today().year)
    p.add_argument("--overlap-days", type=int, default=180, help="finance 表增量水位回拉天数")
    p.add_argument("--bars-start", default="2005-01-01", help="bar_1d 重建起始日")
    p.add_argument("--bars-1m-start-year", type=int, default=2005, help="bar_1m 重建起始年")
    p.add_argument("--skip-finance", action="store_true", help="跳过 10 张 FUND_* finance 表")
    p.add_argument("--skip-securities", action="store_true", help="跳过 securities 全量重载")
    p.add_argument("--skip-bars", action="store_true", help="跳过场内基金 bar_1d")
    p.add_argument("--skip-bars-1m", action="store_true", help="跳过场内基金 bar_1m(最重)")
    p.add_argument("--no-resume", action="store_true", help="bar 重建不跳过已有票/年(慎用)")
    p.add_argument("--min-spare", type=int, default=2_000_000,
                   help="bar_1m 剩余配额低于此值优雅停止(重跑续传)")
    args = p.parse_args()

    bk.jq_auth()
    auth_from_env()
    client = get_client()

    if not args.skip_finance:
        tables = [t.strip().upper() for t in args.tables.split(",")] if args.tables else FUND_SYNC_TABLES
        print("== 基金 finance.run_query 底表同步 (FUND_*;空表→全量 from 2005,有数据→增量) ==")
        stk.sync_all(client, tables=tables, full=args.full, start_year=args.start_year,
                     end_year=args.end_year, overlap_days=args.overlap_days)

    if not args.skip_securities:
        print("== securities 全量重载(股票+指数+场内基金,保留基金细分 type)==")
        uj.update_securities(client)

    if not args.skip_bars:
        # 逐票续传:已在 bar_1d 的股票/指数自动跳过,仅拉新增场内基金票(securities 须先就绪)。
        print("== 场内基金 bar_1d(逐票续传,跳过已有票)==")
        rb.setup_logging()
        rb.rebuild_bars(client, args.bars_start, resume=not args.no_resume)

    if not args.skip_bars_1m:
        print("== 场内基金 bar_1m(逐票续传,配额不足优雅停止)==")
        rb.setup_logging()
        rb.rebuild_bars_1m(client, args.bars_1m_start_year, resume=not args.no_resume,
                           min_spare=args.min_spare)

    # finance 表已在 sync_all 内逐表 OPTIMIZE;此处补 securities/bar_1d(bar_1m 过大不 FINAL)。
    if not args.skip_securities:
        client.command(f"OPTIMIZE TABLE {DATABASE}.securities FINAL")
    if not args.skip_bars:
        client.command(f"OPTIMIZE TABLE {DATABASE}.bar_1d FINAL")

    print("query count:", get_query_count())
    print("FUND BACKFILL DONE")


if __name__ == "__main__":
    main()
