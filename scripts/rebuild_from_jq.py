"""全量从聚宽重建:income/balance(基本面,按 statDate)+ bar_1d(行情,按标的)。

- income/balance 改为**直接落聚宽**(不再本地推导),5 张基本面表全部逐值精确。
- bar_1d 重拉聚宽原始价(fq=None) + 后复权因子(fq='post'),get_price 前/后复权与聚宽一致。
- 日志输出到 logs/rebuild_*.log;**每次调聚宽 API 后记录剩余配额**。
- 可断点续跑:bar_1d 跳过已存在的标的。

用法:
  uv run python scripts/rebuild_from_jq.py --fundamentals      # 第1步(快)
  uv run python scripts/rebuild_from_jq.py --bars              # 第2步(慢, 建议后台)
  uv run python scripts/rebuild_from_jq.py --fundamentals --bars
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import jqdatasdk  # noqa: E402
from jqdatasdk import auth, get_query_count, query, income, balance  # noqa: E402

from kedu.db import DATABASE, auth_from_env, get_client  # noqa: E402
from kedu.finance_schema import FUND_ONEXCHANGE_TYPES  # noqa: E402
from kedu.schema import MARKET_DDL, data_columns, statdate_table_ddl  # noqa: E402
from kedu import day_materialize as daymat  # noqa: E402

# 与日更口径一致:bar_1d 含股票/指数/场内基金;bar_1m 仅股票/场内基金(指数不入分钟线)。
_BAR_TYPES_SQL = ", ".join(f"'{t}'" for t in ("stock", "index", *FUND_ONEXCHANGE_TYPES))
_BAR1M_TYPES_SQL = ", ".join(f"'{t}'" for t in ("stock", *FUND_ONEXCHANGE_TYPES))

LOG = logging.getLogger("rebuild")
Q2END = {"q1": "-03-31", "q2": "-06-30", "q3": "-09-30", "q4": "-12-31"}


def setup_logging() -> Path:
    (ROOT / "logs").mkdir(exist_ok=True)
    logfile = ROOT / "logs" / f"rebuild_{dt.datetime.now():%Y%m%d_%H%M%S}.log"
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(message)s",
        handlers=[logging.FileHandler(logfile, encoding="utf-8"), logging.StreamHandler()])
    return logfile


def spare() -> int:
    return get_query_count()["spare"]


def qlog(label: str, rows: int) -> None:
    """每次聚宽调用后:记录行数 + 剩余配额。"""
    LOG.info(f"{label}: {rows} 行 | 剩余配额 {spare():,}")


def jq_auth() -> None:
    auth(os.getenv("JQDATA_USER"), os.getenv("JQDATA_PASSWORD"))
    LOG.info(f"auth ok | 初始剩余配额 {spare():,}")


def _prep(df: pd.DataFrame, date_cols: list[str]) -> pd.DataFrame:
    df = df.copy()
    if "id" in df.columns:
        df["id"] = pd.to_numeric(df["id"], errors="coerce").fillna(0).astype("int64")
    df["code"] = df["code"].astype(str)
    for d in date_cols:
        df[d] = pd.to_datetime(df[d], errors="coerce").dt.date
    return df


def gen_periods(start_year: int):
    today = dt.date.today()
    cur_q = (today.month - 1) // 3 + 1
    # 当季报告通常下季才出;保险起见拉到"上一季"为止由 get_fundamentals 自行返回(空则跳过)
    quarters, years = [], []
    for y in range(start_year, today.year + 1):
        for qn in range(1, 5):
            if (y, qn) <= (today.year, cur_q):
                quarters.append(f"{y}q{qn}")
    years = [str(y) for y in range(start_year, today.year)]  # 到去年(今年年报未出)
    return quarters, years


def pull_flow(client, model, single_tbl: str, acc_tbl: str, quarters, years) -> None:
    """单季 statDate='YYYYqN' -> single_tbl;年度 statDate='YYYY' -> acc_tbl。"""
    cols = data_columns(model)
    qobj = query(model.id, model.code, model.pubDate, model.statDate, *[getattr(model, c) for c in cols])
    out = ["id", "code", "statDate", "pubDate", *cols]
    for q in quarters:
        df = jqdatasdk.get_fundamentals(qobj, statDate=q)
        if not df.empty:
            client.insert_df(f"{DATABASE}.{single_tbl}", _prep(df, ["statDate", "pubDate"])[out])
        qlog(f"{single_tbl} {q}", len(df))
    for y in years:
        df = jqdatasdk.get_fundamentals(qobj, statDate=str(y))
        if not df.empty:
            client.insert_df(f"{DATABASE}.{acc_tbl}", _prep(df, ["statDate", "pubDate"])[out])
        qlog(f"{acc_tbl} {y}", len(df))


def pull_snapshot(client, model, tbl: str, quarters) -> None:
    """资产负债表为截面快照:按季 statDate 拉入同一张表(年度=Q4,无需另拉)。"""
    cols = data_columns(model)
    qobj = query(model.id, model.code, model.pubDate, model.statDate, *[getattr(model, c) for c in cols])
    out = ["id", "code", "statDate", "pubDate", *cols]
    for q in quarters:
        df = jqdatasdk.get_fundamentals(qobj, statDate=q)
        if not df.empty:
            client.insert_df(f"{DATABASE}.{tbl}", _prep(df, ["statDate", "pubDate"])[out])
        qlog(f"{tbl} {q}", len(df))


def rebuild_fundamentals(client, start_year: int) -> None:
    LOG.info("==== 重建基本面 income/balance(直接落聚宽)====")
    for name in ("income_statement", "income_statement_acc", "balance_sheet"):
        client.command(f"DROP TABLE IF EXISTS {DATABASE}.{name}")
        client.command(statdate_table_ddl(name))
        LOG.info(f"  建表 {name}")
    quarters, years = gen_periods(start_year)
    LOG.info(f"  期间: 季 {quarters[0]}..{quarters[-1]} ({len(quarters)}), 年 {years[0]}..{years[-1]} ({len(years)})")
    LOG.info("-- income --")
    pull_flow(client, income, "income_statement", "income_statement_acc", quarters, years)
    LOG.info("-- balance --")
    pull_snapshot(client, balance, "balance_sheet", quarters)
    for t in ("income_statement", "income_statement_acc", "balance_sheet"):
        client.command(f"OPTIMIZE TABLE {DATABASE}.{t} FINAL")
    # 物化本函数覆盖的 date 模式表(仅 income/balance;cash_flow/indicator 不在本函数范围)。
    # 全量 4 张请用: `python -m kedu.day_materialize full-build --all`。
    # 需 stock_valuation 已就绪(本函数不建估值表);为空时 full_build 会 raise。
    for v in ("income_statement_day", "balance_sheet_day"):
        n = daymat.full_build(client, v, verbose=False)
        LOG.info(f"  物化表 {v}: {n:,} 行")
    for t in ("income_statement", "income_statement_acc", "balance_sheet"):
        n = client.query(f"SELECT count() FROM {DATABASE}.{t}").result_rows[0][0]
        LOG.info(f"  {t}: {n:,} 行")
    LOG.info(f"基本面重建完成 | 剩余配额 {spare():,}")


def rebuild_bars(client, start: str, resume: bool = True, limit_codes: int | None = None) -> None:
    LOG.info("==== 重建 bar_1d(聚宽原始价 fq=None + 后复权因子 fq='post')====")
    client.command(MARKET_DDL["bar_1d"])  # IF NOT EXISTS
    end = dt.date.today().isoformat()
    codes = [r[0] for r in client.query(
        f"SELECT instrument_id FROM {DATABASE}.securities WHERE type IN ({_BAR_TYPES_SQL}) "
        f"ORDER BY instrument_id").result_rows]
    if limit_codes:
        codes = codes[:limit_codes]
        LOG.info(f"  [测试模式] 仅前 {limit_codes} 只")
    done = set()
    if resume:
        done = {str(r[0]) for r in client.query(
            f"SELECT DISTINCT instrument_id FROM {DATABASE}.bar_1d").result_rows}
        LOG.info(f"  断点续跑:已完成 {len(done)} 只,跳过")
    pf = ["open", "close", "high", "low", "pre_close", "high_limit", "low_limit",
          "volume", "money", "avg", "paused"]
    out = ["instrument_id", "date", "open", "close", "high", "low", "pre_close",
           "high_limit", "low_limit", "volume", "money", "avg", "factor", "paused", "is_st"]
    total_rows, n_done = 0, 0
    for code in codes:
        if code in done:
            continue
        raw = jqdatasdk.get_price(code, start_date=start, end_date=end, frequency="daily",
                                  fields=pf, fq=None, panel=False, skip_paused=False)
        qlog(f"raw {code}", 0 if raw is None else len(raw))
        if raw is None or raw.empty:
            n_done += 1
            continue
        fac = jqdatasdk.get_price(code, start_date=start, end_date=end, frequency="daily",
                                  fields=["factor"], fq="post", panel=False, skip_paused=False)
        qlog(f"factor {code}", 0 if fac is None else len(fac))
        raw = raw[raw["close"].notna()].copy()
        if raw.empty:
            n_done += 1
            continue
        raw["factor"] = fac["factor"].reindex(raw.index) if fac is not None else 1.0
        raw["instrument_id"] = code
        raw["date"] = pd.to_datetime(raw.index).date
        raw["paused"] = raw["paused"].fillna(0).astype("uint8")
        raw["is_st"] = 0  # get_price 无 is_st,占位(不影响复权)
        raw["factor"] = raw["factor"].fillna(1.0)
        # 逐票灌整段历史:bar_1d 按月分区,一只票约 250 个月分区,超默认 100 上限 -> 抬到 500。
        client.insert_df(f"{DATABASE}.bar_1d", raw[out],
                         settings={"max_partitions_per_insert_block": 500})
        total_rows += len(raw)
        n_done += 1
        if n_done % 200 == 0:
            LOG.info(f"  进度 {n_done}/{len(codes)-len(done)} 只, 累计 {total_rows:,} 行 | 剩余配额 {spare():,}")
    client.command(f"OPTIMIZE TABLE {DATABASE}.bar_1d FINAL")
    n = client.query(f"SELECT count() FROM {DATABASE}.bar_1d").result_rows[0][0]
    LOG.info(f"bar_1d 重建完成: {n:,} 行 | 剩余配额 {spare():,}")


def rebuild_bars_1m(client, start_year: int = 2005, resume: bool = True,
                    limit_codes: int | None = None, only_year: int | None = None,
                    min_spare: int = 2_000_000) -> None:
    """重建 bar_1m(聚宽原始分钟价 fq=None + 日线后复权因子 fq='post',按日广播到分钟)。

    量巨大(全市场全历史 ~数十亿行 / 数十亿配额,约需数十天)。按 (标的, 年) 切块、
    可断点续跑(跳过已存在年份),单块约 6 万行/标的年,避免单次 get_price 过大。
    剩余配额低于 min_spare 时**优雅停止**(不撞聚宽报错);次日重跑同命令即自动 resume。
    可用 --bars-1m-year 单年批处理、--limit-codes 测试。不做 OPTIMIZE FINAL(表过大,交后台合并)。"""
    LOG.info("==== 重建 bar_1m(fq=None 分钟价 + 日线 fq='post' 因子)====")
    client.command(MARKET_DDL["bar_1m"])  # IF NOT EXISTS
    today = dt.date.today()
    requested_start = dt.date(start_year, 1, 1)
    securities = client.query(
        f"SELECT instrument_id, start_date, end_date FROM {DATABASE}.securities "
        f"WHERE type IN ({_BAR1M_TYPES_SQL}) ORDER BY instrument_id").result_rows
    if limit_codes:
        securities = securities[:limit_codes]
        LOG.info(f"  [测试模式] 仅前 {limit_codes} 只")
    pf = ["open", "close", "high", "low", "pre_close", "high_limit", "low_limit",
          "volume", "money", "avg", "paused"]
    out = ["instrument_id", "datetime", "open", "close", "high", "low", "pre_close",
           "high_limit", "low_limit", "volume", "money", "avg", "factor", "paused"]
    total_rows = 0
    for ci, (code, start_date, end_date) in enumerate(securities, 1):
        code_start = max(start_date or requested_start, requested_start)
        code_end = min(end_date or today, today)
        if code_start > code_end:
            continue
        if spare() < min_spare:
            LOG.info(f"  剩余配额 {spare():,} < {min_spare:,},安全停止于第 {ci}/{len(securities)} 只 "
                     f"({code});累计 {total_rows:,} 行。次日重跑同命令将自动 resume。")
            return
        done_years = set()
        if resume:
            done_years = {int(r[0]) for r in client.query(
                f"SELECT DISTINCT toYear(datetime) FROM {DATABASE}.bar_1m "
                f"WHERE instrument_id='{code}'").result_rows}
        years = [only_year] if only_year else range(code_start.year, code_end.year + 1)
        for y in years:
            if y in done_years:
                continue
            year_start = max(code_start, dt.date(y, 1, 1))
            year_end = min(code_end, dt.date(y, 12, 31))
            if year_start > year_end:
                continue
            # 端点须含当日盘中:裸日期 "YYYY-12-31" 被聚宽按 00:00:00 解释,首根分钟在 09:31,
            # 否则会丢掉**交易日 12-31**整段 09:31-15:00 分钟线。23:59:00 收口对日线因子拉取(下方
            # frequency='daily')亦无副作用(仍含当日日线)。
            s = year_start.isoformat()
            e = f"{year_end.isoformat()} 23:59:00"
            raw = jqdatasdk.get_price(code, start_date=s, end_date=e, frequency="1m",
                                      fields=pf, fq=None, panel=False, skip_paused=False)
            if raw is None or raw.empty:
                continue
            raw = raw[raw["close"].notna()].copy()
            if raw.empty:
                continue
            fac = jqdatasdk.get_price(code, start_date=s, end_date=e, frequency="daily",
                                      fields=["factor"], fq="post", panel=False, skip_paused=False)
            raw["instrument_id"] = code
            raw["datetime"] = pd.to_datetime(raw.index)
            if fac is not None and not fac.empty:
                fmap = {pd.Timestamp(t).date(): v for t, v in zip(fac.index, fac["factor"])}
                raw["factor"] = raw["datetime"].dt.date.map(fmap)
            else:
                raw["factor"] = 1.0
            raw["factor"] = raw["factor"].ffill().fillna(1.0)
            raw["paused"] = raw["paused"].fillna(0).astype("uint8")
            client.insert_df(f"{DATABASE}.bar_1m", raw[out])
            total_rows += len(raw)
            qlog(f"1m {code} {y}", len(raw))
        if ci % 50 == 0:
            LOG.info(f"  进度 {ci}/{len(securities)} 只, 累计 {total_rows:,} 行 | 剩余配额 {spare():,}")
    n = client.query(f"SELECT count() FROM {DATABASE}.bar_1m").result_rows[0][0]
    LOG.info(f"bar_1m 当前 {n:,} 行 | 剩余配额 {spare():,}")


def rebuild_is_st(client, limit_codes: int | None = None, min_spare: int = 2_000_000) -> None:
    """逐票回补 is_st(ST 状态),复用 backfill_jq.sync_is_st(断点续传、退市票锚 end_date)。"""
    import scripts.backfill_jq as bk
    LOG.info("==== 回补 is_st(聚宽 get_extras('is_st'))====")
    bk.sync_is_st(client, limit_codes=limit_codes, min_spare=min_spare)
    n = client.query(f"SELECT count() FROM {DATABASE}.is_st").result_rows[0][0]
    LOG.info(f"is_st 当前 {n:,} 行 | 剩余配额 {spare():,}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--fundamentals", action="store_true")
    p.add_argument("--bars", action="store_true")
    p.add_argument("--is-st", action="store_true", help="逐票回补 is_st(ST 状态),断点续传")
    p.add_argument("--bars-1m", action="store_true", help="重建 bar_1m 分钟线(量巨大)")
    p.add_argument("--bars-1m-start-year", type=int, default=2005)
    p.add_argument("--bars-1m-year", type=int, help="只回补某一年(分批/并行用)")
    p.add_argument("--min-spare", type=int, default=2_000_000,
                   help="bar_1m:剩余配额低于此值时优雅停止(默认 200 万,约一只票全历史的量)")
    p.add_argument("--start-year", type=int, default=2005)
    p.add_argument("--bars-start", default="2005-01-01")
    p.add_argument("--no-resume", action="store_true")
    p.add_argument("--limit-codes", type=int, help="仅重建前 N 只(测试用)")
    args = p.parse_args()
    if not (args.fundamentals or args.bars or args.is_st or args.bars_1m):
        p.error("至少指定 --fundamentals / --bars / --is-st / --bars-1m")

    logfile = setup_logging()
    LOG.info(f"日志 -> {logfile}")
    jq_auth()
    auth_from_env()
    client = get_client()
    t0 = time.time()
    if args.fundamentals:
        rebuild_fundamentals(client, args.start_year)
    if args.bars:
        rebuild_bars(client, args.bars_start, resume=not args.no_resume, limit_codes=args.limit_codes)
    if args.is_st:
        rebuild_is_st(client, limit_codes=args.limit_codes, min_spare=args.min_spare)
    if args.bars_1m:
        rebuild_bars_1m(client, args.bars_1m_start_year, resume=not args.no_resume,
                        limit_codes=args.limit_codes, only_year=args.bars_1m_year,
                        min_spare=args.min_spare)
    LOG.info(f"全部完成,用时 {time.time()-t0:.0f}s | 剩余配额 {spare():,}")


if __name__ == "__main__":
    main()
