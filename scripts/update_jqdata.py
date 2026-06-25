"""聚宽增量更新驱动(pm2 调度)。新架构:5 张基本面表 + bar_1d 全部直接落聚宽。

  1) securities          ← get_all_securities(['stock'])
  2) 报告期基本面增量     ← get_fundamentals(income/balance/cash_flow/indicator, statDate=近 N 季 + 近 2 年)
  3) stock_valuation     ← 自 max(day)+1 起逐交易日
  4) bar_1d 增量          ← get_price(fq=None 原始价 + fq='post' 因子) 逐交易日

所有写入 ReplacingMergeTree,可重复执行。income/balance 不再本地推导,
date 模式由 *_day 视图现算(pubDate<=day 取 max statDate)。
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import jqdatasdk  # noqa: E402
from jqdatasdk import (get_all_securities, get_query_count, get_trade_days,  # noqa: E402
                       income, balance, indicator, cash_flow)

from kedu.db import DATABASE, auth_from_env, get_client  # noqa: E402
from kedu.finance_schema import FUND_ONEXCHANGE_TYPES, FUND_SYNC_TABLES  # noqa: E402
from kedu.calendar import _today_cn, get_trade_days as local_trade_days  # noqa: E402
from kedu import day_materialize as daymat  # noqa: E402
import scripts.backfill_jq as bk  # noqa: E402
import scripts.backfill_stk as stk  # noqa: E402
import scripts.backfill_industry as bi  # noqa: E402
import scripts.backfill_index as bx  # noqa: E402
import scripts.backfill_margin as bm  # noqa: E402
import scripts.backfill_locked_shares as bls  # noqa: E402
import scripts.backfill_money_flow as bmf  # noqa: E402
import scripts.backfill_billboard as bbl  # noqa: E402

# 支持日/分钟行情的 type(bar_1d/bar_1m 取价范围):股票 + 指数 + 场内基金。
BAR_TYPES = ("stock", "index", *FUND_ONEXCHANGE_TYPES)


def recent_quarters(n_quarters: int = 8) -> tuple[list[str], list[str]]:
    today = dt.date.today()
    qs = []
    y, q = today.year, (today.month - 1) // 3 + 1
    for _ in range(n_quarters):
        qs.append(f"{y}q{q}")
        q -= 1
        if q == 0:
            q = 4
            y -= 1
    years = [str(today.year), str(today.year - 1)]
    return qs[::-1], years


def update_securities(client) -> None:
    """股票 + 指数 + 场内基金一并落 securities(type 区分)。指数供 get_all_securities(['index'])、
    场内基金供 get_all_securities(['fund']/['etf']...);三者日线由 update_bars(按 BAR_TYPES)带上,
    指数成分/权重/估值宇宙用 type='index'。"""
    parts = []
    for t in ("stock", "index"):
        d = get_all_securities([t]).reset_index().rename(columns={"index": "instrument_id"})
        d["type"] = t   # 股票/指数:统一 type
        parts.append(d)
    # 场内基金:get_all_securities(['fund']) 已带细分类 type(etf/lof/mmf/reits/fja/fjb/fjm),
    # 必须**保留**该 type、不覆盖,否则 get_all_securities(['etf'] 等)的 parity 会崩。
    fd = get_all_securities(["fund"]).reset_index().rename(columns={"index": "instrument_id"})
    parts.append(fd)
    df = pd.concat(parts, ignore_index=True)
    df["start_date"] = pd.to_datetime(df["start_date"]).dt.date
    df["end_date"] = pd.to_datetime(df["end_date"]).dt.date
    for c in ("exchange", "board_type", "industry_code", "sector_code", "round_lot", "status"):
        if c not in df:
            df[c] = None
    cols = ["instrument_id", "display_name", "name", "start_date", "end_date", "type",
            "exchange", "board_type", "industry_code", "sector_code", "round_lot", "status"]
    client.command(f"TRUNCATE TABLE {DATABASE}.securities")
    client.insert_df(f"{DATABASE}.securities", df[cols])
    n_idx = int((df["type"] == "index").sum())
    n_stk = int((df["type"] == "stock").sum())
    n_fund = int(df["type"].isin(FUND_ONEXCHANGE_TYPES).sum())
    print(f"  securities: {len(df)}(stock {n_stk} / index {n_idx} / 场内基金 {n_fund})")


_BAR_PF = ["open", "close", "high", "low", "pre_close", "high_limit", "low_limit", "volume", "money", "avg", "paused"]


def _pull_day_bars(codes: list[str], d: str):
    """单日拉 raw(fq=None) + factor(fq='post');整批 get_price 失败则二分降级,
    防某些细分类(如部分基金)返回异常拖垮整批。返回 (raw_df, fac_df),失败码自动剔除。"""
    try:
        raw = jqdatasdk.get_price(codes, end_date=d, count=1, frequency="daily", fields=_BAR_PF,
                                  fq=None, panel=False, skip_paused=False)
        fac = jqdatasdk.get_price(codes, end_date=d, count=1, frequency="daily", fields=["factor"],
                                  fq="post", panel=False, skip_paused=False)
        return raw, fac
    except Exception as e:  # noqa: BLE001
        if len(codes) <= 1:
            print(f"  bar_1d {d} {codes}: 跳过(失败 {type(e).__name__}: {str(e)[:80]})", flush=True)
            return None, None
        mid = len(codes) // 2
        r1, f1 = _pull_day_bars(codes[:mid], d)
        r2, f2 = _pull_day_bars(codes[mid:], d)
        raws = [x for x in (r1, r2) if x is not None and not x.empty]
        facs = [x for x in (f1, f2) if x is not None and not x.empty]
        return (pd.concat(raws, ignore_index=True) if raws else None,
                pd.concat(facs, ignore_index=True) if facs else None)


def update_bars(client, start: str, end: str) -> None:
    """逐交易日增量写 bar_1d:fq=None 原始价 + fq='post' 后复权因子(聚宽口径)。

    覆盖股票/指数/场内基金(securities 中 type ∈ BAR_TYPES)。后复权因子以 IPO 为基准、随时间
    累乘,新除权只抬高其后日期的因子,历史日不变,故按日增量拉取即可,无需回改历史。"""
    days = [d.strftime("%Y-%m-%d") for d in get_trade_days(start_date=start, end_date=end)]
    if not days:
        print("  bar_1d: up to date")
        return
    inlist = ", ".join(f"'{t}'" for t in BAR_TYPES)
    windows = client.query(
        f"SELECT instrument_id, start_date, end_date FROM {DATABASE}.securities "
        f"WHERE type IN ({inlist}) ORDER BY instrument_id").result_rows
    out = ["instrument_id", "date", "open", "close", "high", "low", "pre_close",
           "high_limit", "low_limit", "volume", "money", "avg", "factor", "paused", "is_st"]
    total = 0
    for d in days:
        day = dt.date.fromisoformat(d)
        codes = [
            code for code, start_date, end_date in windows
            if (start_date is None or start_date <= day) and (end_date is None or day <= end_date)
        ]
        if not codes:
            continue
        raw, fac = _pull_day_bars(codes, d)
        if raw is None or raw.empty:
            continue
        raw = raw.rename(columns={"code": "instrument_id", "time": "date"})
        fac = fac.rename(columns={"code": "instrument_id", "time": "date"}) if fac is not None else None
        if fac is not None and not fac.empty:
            m = raw.merge(fac[["instrument_id", "factor"]], on="instrument_id", how="left")
        else:
            m = raw.copy()
            m["factor"] = 1.0
        m = m[m["close"].notna()].copy()
        m["date"] = pd.to_datetime(m["date"]).dt.date
        m["paused"] = m["paused"].fillna(0).astype("uint8")
        m["is_st"] = 0
        m["factor"] = m["factor"].fillna(1.0)
        client.insert_df(f"{DATABASE}.bar_1d", m[out])
        total += len(m)
    print(f"  bar_1d: +{total} 行 / {len(days)} 天")


def update_bars_1m(client, today: str, min_spare: int = 2_000_000) -> None:
    """逐票补齐 bar_1m 到 today:每票自其 max(datetime) 之后(无数据则自上市日)补到 today。

    fq=None 原始分钟价 + 日线 fq='post' 因子(按日广播到分钟),按 (票, 年) 切块拉
    get_price(frequency='1m')。游标取**各票各自的 max(datetime)**,而非全局最新日:
    故缺失的票自动补全历史、已满的票只补新交易日,二者用同一逻辑收敛到全覆盖。

    全市场全历史量巨大(数十亿行/配额),剩余配额低于 min_spare 时**优雅停止**;
    bar_1m 为 ReplacingMergeTree((instrument_id, datetime) 去重),重叠插入幂等,
    重跑同命令即按各票 max 自动 resume。
    注:游标按 code 取 max,假设各票数据沿时间向前累积(无内部年份空洞);
    当前数据状态(票要么全历史齐、要么完全缺)满足此前提。"""
    today_d = dt.date.fromisoformat(today)
    REQ_START = dt.date(2005, 1, 1)
    # 股票 + 场内基金(均有分钟线);指数不入 bar_1m(沿历史口径)。无分钟线的细分类(如部分 mmf)
    # get_price 返回空、自动跳过。
    bar1m_types = ", ".join(f"'{t}'" for t in ("stock", *FUND_ONEXCHANGE_TYPES))
    secs = client.query(
        f"SELECT instrument_id, start_date, end_date FROM {DATABASE}.securities "
        f"WHERE type IN ({bar1m_types}) ORDER BY instrument_id").result_rows
    maxmap = dict(client.query(
        f"SELECT instrument_id, max(datetime) FROM {DATABASE}.bar_1m "
        f"GROUP BY instrument_id").result_rows)
    pf = ["open", "close", "high", "low", "pre_close", "high_limit", "low_limit", "volume", "money", "avg", "paused"]
    out = ["instrument_id", "datetime", "open", "close", "high", "low", "pre_close",
           "high_limit", "low_limit", "volume", "money", "avg", "factor", "paused"]
    total = 0
    for ci, (code, start_date, end_date) in enumerate(secs, 1):
        sp = get_query_count()["spare"]
        if sp < min_spare:
            print(f"  bar_1m: 剩余配额不足 {min_spare:,},优雅停止于第 {ci}/{len(secs)} 只 "
                  f"({code});累计 +{total:,} 行,重跑续传。", flush=True)
            return
        code_start = max(start_date or REQ_START, REQ_START)
        code_end = min(end_date or today_d, today_d)
        have = maxmap.get(code)
        fill_start = (have.date() + dt.timedelta(days=1)) if have else code_start
        if fill_start > code_end:
            continue
        code_rows = 0
        for y in range(fill_start.year, code_end.year + 1):
            ys = max(fill_start, dt.date(y, 1, 1)).isoformat()
            # 端点必须含**当日盘中**:裸日期 "YYYY-12-31" 被聚宽按 00:00:00 解释,而首根分钟在 09:31,
            # 故会把当日整段 09:31-15:00 排除 -> 任何**交易日的 12-31**永远拉不到(游标随后越过该日,
            # 该日被永久遗漏)。改为当日 23:59:00 收口,使最后一日的分钟线被完整纳入。
            ye = f"{min(code_end, dt.date(y, 12, 31)).isoformat()} 23:59:00"
            raw = jqdatasdk.get_price(code, start_date=ys, end_date=ye, frequency="1m", fields=pf,
                                      fq=None, panel=False, skip_paused=False)
            if raw is None or raw.empty:
                continue
            raw = raw[raw["close"].notna()].copy()
            if raw.empty:
                continue
            fac = jqdatasdk.get_price(code, start_date=ys, end_date=ye, frequency="daily",
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
            code_rows += len(raw)
            # 每 (票,年) 落盘即吐一行,确保重活阶段持续有日志(全历史票一只会打多行)
            print(f"  bar_1m {code} {y}: +{len(raw):,} 行", flush=True)
        total += code_rows
        # 空请求票(已满、仅差未发布的今日)不刷屏,仅每 100 只打一次心跳
        if code_rows == 0 and ci % 100 == 0:
            print(f"  bar_1m 心跳 {ci}/{len(secs)} 只(均已最新,累计 +{total:,} 行)| spare {sp:,}", flush=True)
        elif code_rows:
            print(f"  bar_1m {code} 完成: +{code_rows:,} 行(到 {code_end})| 进度 {ci}/{len(secs)} | spare {sp:,}", flush=True)
    print(f"  bar_1m: +{total:,} 行(逐票补到 {today})", flush=True)


def _table_exists(client, table) -> bool:
    return bool(client.query(f"EXISTS TABLE {DATABASE}.{table}").result_rows[0][0])


def _max_day(client, table, col):
    if not _table_exists(client, table):
        return None
    return client.query(f"SELECT max({col}) FROM {DATABASE}.{table}").result_rows[0][0]


def _recent_trade_start(end: str, count: int) -> str:
    days = list(local_trade_days(end_date=end, count=max(1, count)))
    return days[0].isoformat() if days else end


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--skip-bars", action="store_true", help="跳过 bar_1d 行情线更新")
    p.add_argument("--skip-bars-1m", action="store_true", help="跳过 bar_1m 分钟线更新")
    p.add_argument("--skip-stk", action="store_true", help="跳过 STK 报告期原始表(finance.run_query 底表)增量")
    p.add_argument("--skip-fund-finance", action="store_true",
                   help="仅跳过 10 张基金 finance.run_query 底表(FUND_*)增量;"
                        "场内基金 securities 仍随 step1 加载、基金 bar 随 --skip-bars/--skip-bars-1m 控制")
    p.add_argument("--skip-is-st", action="store_true", help="跳过 is_st(ST 状态)增量")
    p.add_argument("--skip-industry", action="store_true", help="跳过行业/概念分类刷新(industries/industry_history/concepts/concept_history)")
    p.add_argument("--skip-index", action="store_true", help="跳过指数成分/权重/估值增量(index_member_history/index_weights/index_valuation)")
    p.add_argument("--skip-margin", action="store_true", help="跳过融资融券增量(mtss/margin_target_history)")
    p.add_argument("--skip-locked-shares", action="store_true", help="跳过限售解禁数据集刷新(locked_shares)")
    p.add_argument("--skip-money-flow", action="store_true", help="跳过日频资金流向刷新(money_flow_pro)")
    p.add_argument("--skip-billboard", action="store_true", help="跳过龙虎榜刷新(billboard)")
    p.add_argument("--skip-day-tables", action="store_true",
                   help="跳过 *_day 物化表增量刷新(income/cash_flow/indicator/balance _day)")
    p.add_argument("--day-lookback-days", type=int, default=760,
                   help="*_day 物化刷新回溯天数;须 >= 报告重拉窗口(quarters-back 对应),"
                        "以覆盖迟到披露/重述对历史 (code,day) as-of 取值的传播")
    p.add_argument("--quarters-back", type=int, default=8)
    p.add_argument("--stk-overlap-days", type=int, default=180)
    p.add_argument("--bars-overlap-days", type=int, default=10,
                   help="bar_1d 增量回拉天数:自 max(day)-N 起重拉,覆盖盘中快照/当日定值修正"
                        "(ReplacingMergeTree 按 _ingested_at 顶旧值)")
    p.add_argument("--money-flow-overlap-days", type=int, default=5,
                   help="money_flow_pro 日更回拉最近 N 个交易日,覆盖盘后修正")
    p.add_argument("--billboard-overlap-days", type=int, default=5,
                   help="billboard 日更回拉最近 N 个交易日,覆盖 20:00/22:00 修正")
    p.add_argument("--min-spare", type=int, default=2_000_000,
                   help="bar_1m 逐票补齐时剩余配额低于此值优雅停止(重跑续传)")
    p.add_argument("--only-bars-1m", action="store_true",
                   help="只跑 bar_1m 逐票补齐,跳过其余全部步骤(逐票自 max(datetime) 续传)")
    args = p.parse_args()

    bk.jq_auth()
    auth_from_env()
    client = get_client()
    today = _today_cn().isoformat()

    if args.only_bars_1m:
        print("== bar_1m 逐票补齐(only)==")
        update_bars_1m(client, today, min_spare=args.min_spare)
        print("query count:", get_query_count())
        print("UPDATE DONE")
        return

    print("== 0) trade_days ==")
    bk.sync_trade_days(client)

    print("== 1) securities ==")
    update_securities(client)

    if not args.skip_is_st:
        print("== 1.5) is_st 增量(每票自 max(date)+1 起,退市票跳过)==")
        bk.sync_is_st(client, today=today)

    qs, years = recent_quarters(args.quarters_back)
    print(f"== 2) 报告期基本面增量 (quarters {qs[0]}..{qs[-1]}, years {years}) ==")
    bk.backfill_statement(client, income, "income_statement", "income_statement_acc", qs, years, skip=False)
    bk.backfill_statement(client, balance, "balance_sheet", "balance_sheet", qs, years, skip=False)
    bk.backfill_statement(client, cash_flow, "cash_flow_statement", "cash_flow_statement_acc", qs, years, skip=False)
    bk.backfill_statement(client, indicator, "financial_indicator", "financial_indicator_acc", qs, years, skip=False)

    print("== 3) stock_valuation 增量 ==")
    last = _max_day(client, "stock_valuation", "day")
    vstart = (last + dt.timedelta(days=1)).isoformat() if last else "2005-01-01"
    vdates = [d.strftime("%Y-%m-%d") for d in get_trade_days(start_date=vstart, end_date=today)]
    bk.backfill_valuation(client, vdates, skip=True)

    if not args.skip_bars:
        print("== 4) bar_1d 增量 ==")
        lastb = _max_day(client, "bar_1d", "date")
        # 不用 max(day)+1:回拉 N 天重叠重写,让盘中快照/当日定值修正被新版本顶掉。
        bstart = (lastb - dt.timedelta(days=args.bars_overlap_days)).isoformat() if lastb else "2005-01-01"
        update_bars(client, bstart, today)

    if not args.skip_stk:
        print("== 5) STK 报告期原始表同步 (finance.run_query 底表;空表→全量,有数据→增量) ==")
        stk.sync_all(client, overlap_days=args.stk_overlap_days)

    if not args.skip_fund_finance:
        # 基金 finance.run_query 底表(FUND_*):与 STK 同一引擎(空表→全量 from 2005,有数据→增量)。
        # FUND_NET_VALUE 等逐日大表配额重,首次种子建议先单独跑 scripts/backfill_fund.py。
        print("== 5.5) 基金 finance.run_query 底表同步 (FUND_*;空表→全量 from 1998,有数据→增量) ==")
        stk.sync_all(client, tables=FUND_SYNC_TABLES, start_year=1998, overlap_days=args.stk_overlap_days)

    if not args.skip_industry:
        # 行业/概念:与 backfill_industry.py 同一条 sync() 路径(列表 + 逐股 walk 续传到 today + 折叠)。
        # 历史没补完会在此续传(配额不足优雅停止、重跑续传);补完后每天只补新交易日。
        # 想更快种子化历史,可先单独跑 scripts/backfill_industry.py(专用配额、不和 bar_1m 抢)。
        print("== 6) 行业/概念增量(sync:列表 + 逐股 walk 续传 + 折叠)==")
        bi.sync(client, today=today, min_spare=args.min_spare)

    if not args.skip_index:
        # 指数成分/权重/估值:轻量增量(sync_daily)。历史种子(二分成分回补 + 月度权重全扫 +
        # 历史指数 bar)走 scripts/backfill_index.py 手动跑;此处仅续传/日更,配额不足优雅停止。
        # 指数日线 bar_1d 已由上面 update_bars 自带(securities 含 type='index')。
        print("== 6.5) 指数成分/权重/估值增量(sync_daily;历史种子见 backfill_index.py)==")
        bx.sync_daily(client, today=today, min_spare=args.min_spare)

    if not args.skip_margin:
        # 融资融券:标的列表逐日 walk + 折叠,mtss 逐日(当日标的并集)续传;配额不足优雅停止。
        # 历史种子也走同一 sync() 路径,反复跑续传。
        print("== 6.8) 融资融券增量(mtss 逐日 + 标的列表 walk+折叠)==")
        bm.sync(client, today=today, min_spare=args.min_spare)

    if not args.skip_locked_shares:
        # 限售解禁:聚宽独立数据集(含未来预计行、num 经送转调整),不可由 STK_* 重算 ->
        # 全量重拉刷新(每票一条宽窗口请求;refresh 让未来预计行的 rate 随股本变化更新)。
        # 初次种子(空表只拉缺票、可断点续传)直接跑 scripts/backfill_locked_shares.py。
        print("== 6.9) 限售解禁数据集刷新(locked_shares 全量重拉)==")
        bls.backfill(client, refresh=True, min_spare=args.min_spare)

    if not args.skip_money_flow:
        print("== 6.91) 日频资金流向刷新(money_flow_pro 最近交易日回拉)==")
        mf_start = _recent_trade_start(today, args.money_flow_overlap_days)
        bmf.backfill(client, start_date=mf_start, end_date=today, refresh=True,
                     min_spare=args.min_spare)

    if not args.skip_billboard:
        print("== 6.92) 龙虎榜刷新(billboard 最近交易日按日替换)==")
        bb_start = _recent_trade_start(today, args.billboard_overlap_days)
        bbl.backfill(client, start_date=bb_start, end_date=today, refresh=True,
                     min_spare=args.min_spare)

    optimize = ["trade_days", "income_statement", "income_statement_acc", "balance_sheet",
                "cash_flow_statement", "cash_flow_statement_acc",
                "financial_indicator", "financial_indicator_acc", "stock_valuation", "bar_1d",
                "industries", "industry_history", "concepts", "concept_history",
                "index_member_history", "index_weights", "index_valuation", "index_sync_state",
                "mtss", "margin_target_history", "money_flow_pro", "billboard"]
    if not args.skip_fund_finance:   # 仅当本轮同步了基金表(否则首跑时表未建,OPTIMIZE 会报错)
        optimize += ["fund_main_info", "fund_net_value", "fund_fin_indicator", "fund_portfolio",
                     "fund_portfolio_bond", "fund_portfolio_stock", "fund_invest_target",
                     "fund_dividend", "fund_share_daily", "fund_mf_daily_profit"]
    for t in optimize:
        if not _table_exists(client, t):
            continue
        final = "" if t == "billboard" else " FINAL"
        client.command(f"OPTIMIZE TABLE {DATABASE}.{t}{final}")

    # 6.95) *_day 物化表刷新。必须在上面 OPTIMIZE FINAL(基表已折叠为单版本)之后、bar_1m 之前:
    # bar_1m 可能因配额优雅停止,放其前确保 *_day 当天必刷新。失败隔离:出错只告警不中止 main,
    # base 更新此时已提交、bar_1m 仍照常跑,次日按回溯窗口自愈。迁移期(尚未物化)自动空跑。
    if not args.skip_day_tables:
        print("== 6.95) _day 物化表刷新(staging + REPLACE PARTITION 原子替换)==")
        needed = max(args.quarters_back * 92, 731)
        if args.day_lookback_days < needed:
            print(f"  !! 警告:--day-lookback-days {args.day_lookback_days} < 报告重拉窗口约 {needed} 天,"
                  f"超窗口的旧期重述不会传播到 *_day 表;请调大 --day-lookback-days 或定期全量重建。")
        try:
            daymat.refresh_incremental(client, lookback_days=args.day_lookback_days)
        except Exception as e:  # noqa: BLE001  失败隔离:不中止日更,次日按回溯窗口自愈
            print(f"  !! _day 物化刷新失败(已跳过,不影响 base 更新与 bar_1m):{type(e).__name__}: {e}")

    # bar_1m 最重、最耗配额:放在最后,确保其余必要更新先全部完成。
    # 逐票自各自 max(datetime) 补到 today:缺失票补全历史、已满票只补新日,
    # 配额不足优雅停止,重跑同命令自动续传(全市场全历史需跨多天)。
    if not args.skip_bars_1m:
        print("== 7) bar_1m 逐票补齐(最后)==")
        update_bars_1m(client, today, min_spare=args.min_spare)

    print("query count:", get_query_count())
    print("UPDATE DONE")


if __name__ == "__main__":
    main()
