"""从聚宽抓取指数数据到 ClickHouse(读侧见 kedu.index / kedu.prices / kedu.securities):

  - 指数列表        ← get_all_securities(['index']) 落 securities(type='index'),由 update_jqdata.update_securities 维护
  - index_valuation ← get_index_valuation(9 指数) 逐码 max(day)+1 续传
  - index_weights   ← get_index_weights(idx, 月末锚点) 逐月扫描,sync_state 记 covered(空月也推进,不重拉)
  - index_member_*  ← get_index_stocks(idx, date) 粗网格 + 递归二分定位变更日 → staging seg → 折叠成区间
  - 指数日线 bar_1d ← get_price(index, fq=None) 逐日(factor≡1);历史种子在本脚本,日更由 update_jqdata.update_bars 自带

为什么成分股用二分而非逐日全扫:get_index_stocks 无批量,全指数逐交易日 = n_index × ~5100 次
往返不可行。成分集分段恒定(调仓日才变),粗网格采样 + 在「相邻锚点不同」的区间内递归二分,
精确定位变更日,调用量降 1~2 个数量级,且区间边界与逐日折叠等价。

进度与续传:index_sync_state(dataset, index_code, covered_until) 记每个指数已扫到的交易日;
逐指数边界检查剩余配额,低于 min_spare 优雅停止、重跑续传(种子与日更同一路径,covered 保证只取增量)。

用法(历史种子,可反复跑续传):
  uv run --env-file .env python scripts/backfill_index.py
  可选:--min-spare N、--member-step N、--skip-bars。
日常增量由 update_jqdata.py 调 sync_daily()(不含历史 bar 种子)。
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from collections import OrderedDict, defaultdict
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jqdatasdk  # noqa: E402
from jqdatasdk import get_query_count  # noqa: E402

from kedu.db import DATABASE, auth_from_env, get_client  # noqa: E402
from kedu.index import INDEX_VAL_FIELDS, INDEX_VAL_SECURITIES  # noqa: E402
from kedu.schema import INDEX_DDL, MARKET_DDL  # noqa: E402
import scripts.backfill_jq as bk  # noqa: E402  (复用 jq_auth)

INDEX_START = "2005-01-01"
MEMBER_STEP = 21          # 成分二分的粗网格步长(交易日);≈月度,覆盖调仓周期
WEIGHT_START = "2005-01-01"
_RETRIES = 3              # 瞬时错误重试次数
_RETRY_SLEEP = 1.0        # 重试基准退避(秒,线性递增)


def _jq_retry(fn, *args, **kwargs):
    """调用 jqdatasdk 并对瞬时错误重试;耗尽后**抛出**(由逐指数循环捕获跳过该指数)。

    关键:绝不把异常吞成空结果。网络抖动/限流被重试吸收;持续失败(不支持该指数等)
    向上抛出,在逐指数边界被捕获、跳过本只(covered 不前进,下轮重试),既不静默写入
    伪「成分清空」损坏区间,也不让整轮种子崩在某个不支持的指数上。
    """
    last = None
    for i in range(_RETRIES):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(_RETRY_SLEEP * (i + 1))
    raise last

# 成分二分的 staging(折叠成区间后由 index_member_history 承载读侧)。
SEG_DDL = f"""CREATE TABLE IF NOT EXISTS {DATABASE}.index_member_seg (
  index_code String, seg_start Date, stock String, position UInt32,
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at)
ORDER BY (index_code, seg_start, stock)"""


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def _resolve_today(today: str | None) -> dt.date:
    return dt.date.fromisoformat(today) if today else dt.date.today()


def _as_date(v):
    """把聚宽/ClickHouse 返回的日期类值规整为 datetime.date,缺失为 None。"""
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return pd.Timestamp(v).date()


def _index_universe(client):
    """指数宇宙:securities 里 type='index' 的 (code, start_date, end_date)。"""
    return client.query(
        f"SELECT instrument_id, start_date, end_date FROM {DATABASE}.securities "
        f"WHERE type='index' ORDER BY instrument_id").result_rows


def _trade_days(client, start_iso, end_iso) -> list[dt.date]:
    return [r[0] for r in client.query(
        f"SELECT day FROM {DATABASE}.trade_days WHERE day BETWEEN '{start_iso}' AND '{end_iso}' "
        f"ORDER BY day").result_rows]


def _get_covered(client, dataset: str) -> dict[str, dt.date]:
    rows = client.query(
        f"SELECT index_code, max(covered_until) FROM {DATABASE}.index_sync_state FINAL "
        f"WHERE dataset = '{dataset}' GROUP BY index_code").result_rows
    return {r[0]: _as_date(r[1]) for r in rows}


def _set_covered(client, dataset: str, index_code: str, day: dt.date) -> None:
    client.insert(f"{DATABASE}.index_sync_state", [[dataset, index_code, day]],
                  column_names=["dataset", "index_code", "covered_until"])


def _index_life_days(client, code, sd, ed, today_d: dt.date) -> list[dt.date]:
    """指数有效期内的交易日:[max(start,2005) .. min(end,today)]。"""
    lo = max(_as_date(sd) or dt.date.fromisoformat(INDEX_START), dt.date.fromisoformat(INDEX_START))
    hi = min(_as_date(ed) or today_d, today_d)
    if lo > hi:
        return []
    return _trade_days(client, lo.isoformat(), hi.isoformat())


# ---------------------------------------------------------------------------
# (b) index_valuation —— 9 指数,逐码 max(day)+1 续传
# ---------------------------------------------------------------------------
def backfill_index_valuation(client, today=None) -> None:
    """逐指数自各自 max(day)+1 拉 get_index_valuation 到 today(按年分块,守 10000 行/次上限)。"""
    client.command(INDEX_DDL["index_valuation"])
    today_d = _resolve_today(today)
    maxmap = dict(client.query(
        f"SELECT code, max(day) FROM {DATABASE}.index_valuation GROUP BY code").result_rows)
    cnt = dict(client.query(
        f"SELECT code, count() FROM {DATABASE}.index_valuation GROUP BY code").result_rows)
    out_cols = ["code", "day", *INDEX_VAL_FIELDS]
    total = 0
    for code in INDEX_VAL_SECURITIES:
        have = _as_date(maxmap.get(code)) if cnt.get(code) else None
        start = (have + dt.timedelta(days=1)) if have else dt.date.fromisoformat(INDEX_START)
        if start > today_d:
            continue
        try:
            for y in range(start.year, today_d.year + 1):
                ys = max(start, dt.date(y, 1, 1)).isoformat()
                ye = min(today_d, dt.date(y, 12, 31)).isoformat()
                df = _jq_retry(jqdatasdk.get_index_valuation, code, start_date=ys, end_date=ye,
                               fields=list(INDEX_VAL_FIELDS), count=None)
                if df is None or df.empty:
                    continue
                df = df.copy()
                df["day"] = pd.to_datetime(df["day"]).dt.date
                for c in INDEX_VAL_FIELDS:
                    if c not in df.columns:
                        df[c] = None
                df["code"] = df["code"].astype(str)
                client.insert_df(f"{DATABASE}.index_valuation", df[out_cols])
                total += len(df)
        except Exception as e:  # noqa: BLE001  持续失败:跳过本码(下轮自 max(day) 续)
            print(f"  index_valuation {code}: 多次出错,跳过(下轮续): {e!r}", flush=True)
            continue
        print(f"  index_valuation {code}: 累计 +{total}", flush=True)
    print(f"  index_valuation: +{total} 行(逐码补到 {today_d})", flush=True)


# ---------------------------------------------------------------------------
# (c) index_weights —— 逐月锚点扫描,sync_state 记 covered(空月也推进)
# ---------------------------------------------------------------------------
def _month_anchors(trade_days: list[dt.date]) -> list[dt.date]:
    """每个自然月的**最后一个交易日**作为权重查询锚点(只查真实交易日,不查日历月末/周末)。"""
    by_month: dict[tuple[int, int], dt.date] = {}
    for d in trade_days:  # 升序遍历,同月后者覆盖前者 → 留每月最后交易日
        by_month[(d.year, d.month)] = d
    return list(by_month.values())


_WEIGHT_COLS = ["index_code", "code", "position", "weight", "display_name", "weight_date"]


def backfill_index_weights(client, today=None, min_spare: int = 2_000_000) -> None:
    """逐指数按每月最后交易日锚点调 get_index_weights,按返回披露日去重落 index_weights。

    瞬时错误重试(_jq_retry);持续失败跳过本只剩余锚点、落已收集的、covered 不再前进(下轮续)。
    """
    client.command(INDEX_DDL["index_weights"])
    today_d = _resolve_today(today)
    covered = _get_covered(client, "weight")
    universe = _index_universe(client)
    have_dates = defaultdict(set)  # index_code -> {已落库 weight_date}
    for ic, wd in client.query(
            f"SELECT index_code, weight_date FROM {DATABASE}.index_weights FINAL").result_rows:
        have_dates[ic].add(_as_date(wd))
    total = 0
    for n, (code, sd, ed) in enumerate(universe, 1):
        if get_query_count()["spare"] < min_spare:
            print(f"  index_weights: 剩余配额不足 {min_spare:,},优雅停止于第 {n}/{len(universe)} 只 ({code})",
                  flush=True)
            return
        cov = covered.get(code)
        start = (cov.replace(day=1) + pd.offsets.MonthBegin(1)).date() if cov else \
            (_as_date(sd) or dt.date.fromisoformat(WEIGHT_START))
        end_lim = min(_as_date(ed) or today_d, today_d)   # 退市后不查
        anchors = _month_anchors(_trade_days(client, start.isoformat(), end_lim.isoformat()))
        if not anchors:
            continue
        buf: list[tuple] = []
        err = None
        try:
            for anchor in anchors:
                df = _jq_retry(jqdatasdk.get_index_weights, code, date=anchor.isoformat())
                if df is not None and not df.empty:
                    wd = _as_date(df["date"].iloc[0]) if "date" in df.columns else anchor
                    if wd not in have_dates[code]:
                        have_dates[code].add(wd)
                        for pos, (stock, row) in enumerate(df.iterrows()):
                            buf.append((code, str(stock), pos,
                                        None if pd.isna(row.get("weight")) else float(row["weight"]),
                                        None if row.get("display_name") is None else str(row["display_name"]),
                                        wd))
                _set_covered(client, "weight", code, anchor)
        except Exception as e:  # noqa: BLE001  持续失败:落已收集的,跳过剩余锚点(下轮续)
            err = e
        if buf:
            client.insert_df(f"{DATABASE}.index_weights", pd.DataFrame(buf, columns=_WEIGHT_COLS))
            total += len(buf)
        if err is not None:
            print(f"  index_weights {code}: 多次出错,跳过剩余锚点(下轮续): {err!r}", flush=True)
            continue
        if n % 50 == 0 or n == len(universe):
            print(f"  index_weights {code}: 累计 +{total} 行 | {n}/{len(universe)} | spare {get_query_count()['spare']:,}",
                  flush=True)
    print(f"  index_weights: +{total} 行(补到 {today_d})", flush=True)


# ---------------------------------------------------------------------------
# (d) index_member_history —— 二分变更点 → staging seg → 折叠区间
# ---------------------------------------------------------------------------
def _members(idx: str, day: dt.date, cache: dict) -> list[str]:
    """idx 在 day 的成分股有序 list(1 配额/调用,按 day 缓存)。

    瞬时错误重试,持续失败抛出(不吞成空 —— 见 _jq_retry)。空成分须是 API 真返回空。
    """
    if day in cache:
        return cache[day]
    lst = _jq_retry(jqdatasdk.get_index_stocks, idx, date=day.isoformat())
    lst = [str(s) for s in (lst or [])]
    cache[day] = lst
    return lst


def _find_change_idx(idx: str, days: list[dt.date], step: int, cache: dict) -> set[int]:
    """返回 days 内成员集相对前一采样发生变化的下标(不含 0)。

    先按 step 粗网格定位「相邻锚点集合不同」的区间,再在该区间递归二分到相邻,精确定位变更日。
    集合比较用 frozenset(顺序变化不算变更;聚宽成分按代码序,集合相同即顺序相同)。
    """
    changes: set[int] = set()
    n = len(days)
    if n < 2:
        return changes
    anchors = list(range(0, n, step))
    if anchors[-1] != n - 1:
        anchors.append(n - 1)

    def fset(i):
        return frozenset(_members(idx, days[i], cache))

    def rec(lo, hi):
        if fset(lo) == fset(hi):
            return
        if hi - lo == 1:
            changes.add(hi)
            return
        mid = (lo + hi) // 2
        rec(lo, mid)
        rec(mid, hi)

    for a, b in zip(anchors, anchors[1:]):
        rec(a, b)
    return changes


def walk_index_members(client, today=None, min_spare: int = 2_000_000,
                       step: int = MEMBER_STEP) -> None:
    """逐指数自 covered+1 二分扫到 today,新段落 staging index_member_seg;covered 推进。

    续传:已有 staging 的指数,用其最后一段成员作为「pending 之前的成员」以衔接边界变更。
    种子与日更同一路径:日更 pending 通常仅 1 天,二分退化为 1 次采样。
    """
    client.command(SEG_DDL)
    today_d = _resolve_today(today)
    covered = _get_covered(client, "member")
    universe = _index_universe(client)
    # 各指数最后一段(seg_start 最大)的成员集,用于衔接。
    last_seg = {}
    for ic, ss in client.query(
            f"SELECT index_code, max(seg_start) FROM {DATABASE}.index_member_seg FINAL "
            f"GROUP BY index_code").result_rows:
        last_seg[ic] = _as_date(ss)
    prev_members = defaultdict(frozenset)
    if last_seg:
        for ic, ss in last_seg.items():
            stocks = [r[0] for r in client.query(
                f"SELECT stock FROM {DATABASE}.index_member_seg FINAL "
                f"WHERE index_code='{ic}' AND seg_start='{ss.isoformat()}'").result_rows]
            prev_members[ic] = frozenset(stocks)

    total_segs = 0
    for n, (code, sd, ed) in enumerate(universe, 1):
        if get_query_count()["spare"] < min_spare:
            print(f"  index_member: 剩余配额不足 {min_spare:,},优雅停止于第 {n}/{len(universe)} 只 ({code})",
                  flush=True)
            return
        life = _index_life_days(client, code, sd, ed, today_d)
        if not life:
            continue
        cov = covered.get(code)
        pending = [d for d in life if cov is None or d > cov]
        if not pending:
            continue
        try:
            cache: dict[dt.date, list[str]] = {}
            change_idx = _find_change_idx(code, pending, step, cache)
            seg_idx = sorted({0} | change_idx)
            prev = prev_members.get(code, frozenset())
            rows: list[tuple] = []
            for si in seg_idx:
                members = _members(code, pending[si], cache)
                mset = frozenset(members)
                if si == 0 and prev and mset == prev:
                    continue  # 与上一段衔接,无新段
                for pos, stock in enumerate(members):
                    rows.append((code, pending[si], stock, pos))
                prev = mset
        except Exception as e:  # noqa: BLE001  持续失败:跳过本只,covered 不前进,下轮重试
            print(f"  index_member {code}: 多次出错,跳过本轮(下轮重试): {e!r}", flush=True)
            continue
        if rows:
            client.insert_df(f"{DATABASE}.index_member_seg", pd.DataFrame(
                rows, columns=["index_code", "seg_start", "stock", "position"]))
            total_segs += len(seg_idx)
        prev_members[code] = prev
        _set_covered(client, "member", code, pending[-1])
        if n % 50 == 0 or n == len(universe):
            print(f"  index_member {code}: 累计 +{total_segs} 段行 | {n}/{len(universe)} | spare {get_query_count()['spare']:,}",
                  flush=True)
    print(f"  index_member: 本轮 staging +{total_segs} 段行(扫到 {today_d})", flush=True)


def build_index_member_history(client, today=None) -> None:
    """折叠 index_member_seg → index_member_history(逐股合并相邻在档段),TRUNCATE+reload,读侧不 FINAL。"""
    client.command(INDEX_DDL["index_member_history"])
    today_d = _resolve_today(today)
    td = _trade_days(client, INDEX_START, today_d.isoformat())
    if not td:
        print("  build_index_member_history: 无 trade_days")
        return
    td_pos = {d: i for i, d in enumerate(td)}
    latest = td[-1]
    covered = _get_covered(client, "member")
    rows = client.query(
        f"SELECT index_code, seg_start, stock, position FROM {DATABASE}.index_member_seg FINAL "
        f"ORDER BY index_code, seg_start, position").result_rows
    if not rows:
        print("  build_index_member_history: staging 为空,先跑 walk_index_members")
        return
    by_index: dict[str, OrderedDict] = defaultdict(OrderedDict)
    for ic, ss, st, pos in rows:
        by_index[ic].setdefault(_as_date(ss), []).append((str(st), int(pos)))

    out: list[tuple] = []
    for ic, segmap in by_index.items():
        seg_starts = sorted(segmap.keys())
        cov = covered.get(ic, latest)
        seg_end = []
        for k, ss in enumerate(seg_starts):
            if k + 1 < len(seg_starts):
                j = td_pos.get(seg_starts[k + 1])
                seg_end.append(td[j - 1] if (j is not None and j > 0) else ss)
            else:
                seg_end.append(cov)
        stock_segs: dict[str, dict[int, int]] = defaultdict(dict)
        for k, ss in enumerate(seg_starts):
            for st, pos in segmap[ss]:
                stock_segs[st][k] = pos
        for st, segpos in stock_segs.items():
            ks = sorted(segpos.keys())
            runs = []
            a = p = ks[0]
            for k in ks[1:]:
                if k == p + 1:
                    p = k
                else:
                    runs.append((a, p))
                    a = p = k
            runs.append((a, p))
            for a, b in runs:
                is_last = (b == len(seg_starts) - 1)
                end_day = seg_end[b]
                end_date = None if (is_last and end_day == latest and cov >= latest) else end_day
                out.append((ic, st, segpos[a], seg_starts[a], end_date))

    client.command(f"TRUNCATE TABLE {DATABASE}.index_member_history")
    if out:
        df = pd.DataFrame(out, columns=["index_code", "stock", "position", "start_date", "end_date"])
        client.insert_df(f"{DATABASE}.index_member_history", df)
    n_idx = len({r[0] for r in out})
    print(f"  index_member_history: {len(out)} 区间 / {n_idx} 指数", flush=True)


# ---------------------------------------------------------------------------
# (e) 指数日线 bar_1d 历史种子(日更由 update_jqdata.update_bars 自带)
# ---------------------------------------------------------------------------
def backfill_index_bars(client, start: str | None = None, end: str | None = None) -> None:
    """逐交易日 get_price(指数, fq=None) 落 bar_1d:factor≡1、is_st=0,涨跌停/paused 存 API 原值。"""
    client.command(MARKET_DDL["bar_1d"])
    today_d = _resolve_today(end)
    start = start or INDEX_START
    days = [d.strftime("%Y-%m-%d") for d in jqdatasdk.get_trade_days(start_date=start, end_date=today_d.isoformat())]
    universe = _index_universe(client)
    pf = ["open", "close", "high", "low", "pre_close", "high_limit", "low_limit", "volume", "money", "avg", "paused"]
    out = ["instrument_id", "date", "open", "close", "high", "low", "pre_close",
           "high_limit", "low_limit", "volume", "money", "avg", "factor", "paused", "is_st"]
    total = 0
    for d in days:
        day = dt.date.fromisoformat(d)
        codes = [c for c, sd, ed in universe
                 if (_as_date(sd) is None or _as_date(sd) <= day) and (_as_date(ed) is None or day <= _as_date(ed))]
        if not codes:
            continue
        raw = jqdatasdk.get_price(codes, end_date=d, count=1, frequency="daily", fields=pf,
                                  fq=None, panel=False, skip_paused=False)
        if raw is None or raw.empty:
            continue
        raw = raw.rename(columns={"code": "instrument_id", "time": "date"})
        raw = raw[raw["close"].notna()].copy()
        if raw.empty:
            continue
        raw["date"] = pd.to_datetime(raw["date"]).dt.date
        raw["paused"] = raw["paused"].fillna(0).astype("uint8")
        raw["factor"] = 1.0
        raw["is_st"] = 0
        client.insert_df(f"{DATABASE}.bar_1d", raw[out])
        total += len(raw)
    print(f"  index bar_1d: +{total} 行 / {len(days)} 天(到 {today_d})", flush=True)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def sync_daily(client, today=None, min_spare: int = 2_000_000, step: int = MEMBER_STEP) -> None:
    """已种子化后的轻量增量(供 update_jqdata 调用;不含历史 bar 种子,指数日线由 update_bars 自带)。"""
    print("== 指数估值增量 ==")
    backfill_index_valuation(client, today=today)
    print("== 指数权重增量(逐月续) ==")
    backfill_index_weights(client, today=today, min_spare=min_spare)
    print("== 指数成分二分续传 ==")
    walk_index_members(client, today=today, min_spare=min_spare, step=step)
    print("== 折叠指数成分区间 ==")
    build_index_member_history(client, today=today)


def sync(client, today=None, min_spare: int = 2_000_000, step: int = MEMBER_STEP,
         skip_bars: bool = False) -> None:
    """历史种子 + 增量(反复跑续传)。含指数日线历史 bar 种子。"""
    sync_daily(client, today=today, min_spare=min_spare, step=step)
    if not skip_bars:
        print("== 指数日线历史种子 ==")
        backfill_index_bars(client, end=(today or dt.date.today().isoformat()))


def main() -> None:
    p = argparse.ArgumentParser(description="指数成分/权重/估值/日线 回补(反复跑续传)。")
    p.add_argument("--min-spare", type=int, default=2_000_000, help="逐指数剩余配额低于此值优雅停止")
    p.add_argument("--member-step", type=int, default=MEMBER_STEP, help="成分二分粗网格步长(交易日)")
    p.add_argument("--skip-bars", action="store_true", help="跳过指数日线历史 bar 种子")
    args = p.parse_args()

    bk.jq_auth()
    auth_from_env()
    client = get_client()
    sync(client, min_spare=args.min_spare, step=args.member_step, skip_bars=args.skip_bars)
    print("query count:", get_query_count())
    print("DONE")


if __name__ == "__main__":
    main()
