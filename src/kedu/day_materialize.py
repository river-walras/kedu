"""date 模式基本面 as-of 物化表(*_day)的构建、刷新与一致性校验。

背景: get_fundamentals(date=X) 走 4 张 ``*_day`` 关系(income_statement_day /
cash_flow_statement_day / financial_indicator_day / balance_sheet_day), 历史上是视图,
每次查询都在服务端逐列 argMax as-of 现算(~245ms/天), 成为回测瓶颈。本模块把这 4 张
视图物化成**同名预计算表**(透明 drop-in, 查询层零改动), q1 由 ~245ms 降到 ~30ms。

逐字节一致的命门是表与视图共用 ``schema._day_select_body`` 同一段 SELECT;按 day 分片物化
不改变任一 (code, day) 单元取值(见该函数文档)。物化必须在基表 OPTIMIZE FINAL 之后跑,
此时 ReplacingMergeTree 基表每 (code, statDate) 恰一行, argMax/max(statDate) 取值确定。

流程:
- full_build  : 离线全量重建(DROP+CREATE+逐月 INSERT+OPTIMIZE FINAL)。重构期间不应有查询。
- refresh_*   : 日更增量, 走 staging + REPLACE PARTITION 原子替换, 回测进行中不读到半刷新分区。
- consistency_guard: 表 vs {name}_view 逐列精确比对(tol=0), 作为迁移完成的硬门槛。
"""

from __future__ import annotations

import argparse
import datetime as dt

import numpy as np
import pandas as pd

from .db import DATABASE, query_df
from .schema import (
    DAY_VIEWS,
    _day_select_body,
    data_columns,
    day_table_ddl,
    day_view_ddl,
)

# *_day 关系的非数据列(SELECT 主体产出顺序: code, day, id, pubDate, statDate, *data)。
_KEY_COLS = ["code", "day", "id", "pubDate", "statDate"]


# ---------------------------------------------------------------------------
# 月份 / 分区 / 关系类型 工具
# ---------------------------------------------------------------------------
def _month_first(d: dt.date) -> dt.date:
    return d.replace(day=1)


def _next_month(d: dt.date) -> dt.date:
    return (
        dt.date(d.year + 1, 1, 1) if d.month == 12 else dt.date(d.year, d.month + 1, 1)
    )


def _months_in_range(
    start: dt.date, end: dt.date
) -> list[tuple[int, dt.date, dt.date]]:
    """返回 [start, end] 覆盖的逐月 (yyyymm, 月首, 次月首)。yyyymm 即 toYYYYMM 分区值。"""
    out: list[tuple[int, dt.date, dt.date]] = []
    cur = _month_first(start)
    last = _month_first(end)
    while cur <= last:
        nxt = _next_month(cur)
        out.append((cur.year * 100 + cur.month, cur, nxt))
        cur = nxt
    return out


def _day_where(lo: dt.date, hi: dt.date) -> str:
    """sv.day ∈ [lo, hi) 的 WHERE 谓词(放在 GROUP BY 之前, 作用于 stock_valuation.day)。"""
    return f"sv.day >= '{lo.isoformat()}' AND sv.day < '{hi.isoformat()}'"


def _relation_kind(client, name: str) -> str:
    """返回 'table' | 'view' | 'missing'。用于日更迁移期兼容: 仍是视图/缺失则跳过刷新。"""
    rows = client.query(
        f"SELECT engine FROM system.tables WHERE database = '{DATABASE}' AND name = '{name}'"
    ).result_rows
    if not rows:
        return "missing"
    return "view" if rows[0][0] == "View" else "table"


def _partition_exists(client, name: str, pid: int) -> bool:
    n = client.query(
        f"SELECT count() FROM system.parts WHERE database = '{DATABASE}' "
        f"AND table = '{name}' AND partition_id = '{pid}' AND active"
    ).result_rows[0][0]
    return bool(n)


def _sv_day_bounds(client) -> tuple[dt.date | None, dt.date | None]:
    lo, hi = client.query(
        f"SELECT min(day), max(day) FROM {DATABASE}.stock_valuation"
    ).result_rows[0]
    return lo, hi


def _insert_select(name: str, where: str, target: str | None = None) -> str:
    """INSERT INTO {target} (显式列) <name 的 SELECT 主体>。

    显式列表让 _ingested_at 走 DEFAULT now()。target 默认即 name(全量物化),
    刷新时传 staging 表名。SELECT 口径恒由 name 决定, 与 target 无关。
    """
    _, model = DAY_VIEWS[name]
    cols = _KEY_COLS + data_columns(model)
    collist = ", ".join(f"`{c}`" for c in cols)
    return (
        f"INSERT INTO {DATABASE}.{target or name} ({collist})\n"
        f"{_day_select_body(name, where=where)}"
    )


# ---------------------------------------------------------------------------
# 全量离线重建
# ---------------------------------------------------------------------------
def ensure_views(client, names: list[str] | None = None, verbose: bool = True) -> None:
    """创建/刷新 *_day_view 校验视图(A/B 用)。"""
    for name in names or list(DAY_VIEWS):
        if verbose:
            print(f"  view {DATABASE}.{name}_view")
        client.command(day_view_ddl(name))


def full_build(client, name: str, with_views: bool = True, verbose: bool = True) -> int:
    """离线全量物化单张 *_day 表。**调用前请确保基表已 OPTIMIZE FINAL、且无并发查询。**

    DROP(表或旧同名视图)→ 建表 → 按 stock_valuation 的月份逐月 INSERT(每月一分区,
    规避 max_partitions_per_insert_block)→ OPTIMIZE FINAL。返回总行数。
    """
    lo, hi = _sv_day_bounds(client)
    if lo is None:
        raise RuntimeError("stock_valuation 为空, 无法物化")
    if verbose:
        print(f"full_build {name}: stock_valuation {lo}..{hi}")
    # 同名旧关系可能是视图(历史)或表(重跑), 一律先删再以表重建。
    client.command(f"DROP VIEW IF EXISTS {DATABASE}.{name}")
    client.command(f"DROP TABLE IF EXISTS {DATABASE}.{name}")
    client.command(day_table_ddl(name))
    for pid, mlo, mhi in _months_in_range(lo, hi):
        client.command(_insert_select(name, _day_where(mlo, mhi)))
        if verbose:
            print(f"  {name} {pid} inserted", flush=True)
    client.command(f"OPTIMIZE TABLE {DATABASE}.{name} FINAL")
    if with_views:
        ensure_views(client, [name], verbose=verbose)
    n = client.query(f"SELECT count() FROM {DATABASE}.{name}").result_rows[0][0]
    if verbose:
        print(f"full_build {name}: {n:,} 行")
    return n


def full_build_all(client, with_views: bool = True, verbose: bool = True) -> None:
    for name in DAY_VIEWS:
        full_build(client, name, with_views=with_views, verbose=verbose)


# ---------------------------------------------------------------------------
# 增量原子刷新(staging + REPLACE PARTITION)
# ---------------------------------------------------------------------------
def refresh_months(
    client, name: str, months: list[tuple[int, dt.date, dt.date]], verbose: bool = True
) -> None:
    """对给定月份原子刷新单张 *_day 表。

    每月: 建唯一 staging(结构克隆目标表)→ 灌入该月 → OPTIMIZE staging FINAL →
    REPLACE PARTITION FROM staging(原子, 查询在切换瞬间过渡)→ 删 staging。
    边界: 若该月 staging 为 0 行而目标旧分区有数据, REPLACE 无法表达"替换为空",
    改为 DROP PARTITION(分区存在才删)。
    """
    for pid, mlo, mhi in months:
        stg = f"{name}__stg_{pid}"
        client.command(f"DROP TABLE IF EXISTS {DATABASE}.{stg}")
        client.command(f"CREATE TABLE {DATABASE}.{stg} AS {DATABASE}.{name}")
        try:
            client.command(_insert_select(name, _day_where(mlo, mhi), target=stg))
            client.command(f"OPTIMIZE TABLE {DATABASE}.{stg} FINAL")
            n = client.query(f"SELECT count() FROM {DATABASE}.{stg}").result_rows[0][0]
            if n:
                client.command(
                    f"ALTER TABLE {DATABASE}.{name} REPLACE PARTITION {pid} "
                    f"FROM {DATABASE}.{stg}"
                )
            elif _partition_exists(client, name, pid):
                client.command(f"ALTER TABLE {DATABASE}.{name} DROP PARTITION {pid}")
            if verbose:
                print(f"  refresh {name} {pid}: {n:,} 行", flush=True)
        finally:
            client.command(f"DROP TABLE IF EXISTS {DATABASE}.{stg}")


def refresh_incremental(
    client,
    lookback_days: int = 760,
    names: list[str] | None = None,
    verbose: bool = True,
) -> None:
    """日更增量刷新: 自 stock_valuation.max(day) 回溯 lookback_days, 刷受影响月份。

    lookback_days 必须 >= 日更报告重拉窗口(update_jqdata --quarters-back 对应天数),
    以覆盖"迟到披露/重述"对历史 (code, day) as-of 取值的传播。

    迁移期兼容: 对每张表先探测是否已是 TABLE; 仍是视图或缺失则告警跳过(绝不 REPLACE 到视图),
    使本步在离线 full_build 之前自动空跑, 不破坏日更。
    """
    _, hi = _sv_day_bounds(client)
    if hi is None:
        print("  refresh_incremental: stock_valuation 为空, 跳过")
        return
    start = hi - dt.timedelta(days=lookback_days)
    months = _months_in_range(start, hi)
    if verbose:
        print(f"  refresh_incremental: {start}..{hi} ({len(months)} 个月)")
    for name in names or list(DAY_VIEWS):
        kind = _relation_kind(client, name)
        if kind != "table":
            print(
                f"  [skip] {name} 当前为 {kind}(未物化), 跳过刷新;"
                f"请先离线运行 `python -m kedu.day_materialize full-build --all`"
            )
            continue
        refresh_months(client, name, months, verbose=verbose)


# ---------------------------------------------------------------------------
# 一致性校验(表 vs {name}_view, tol=0)—— 运行时自带最小比较逻辑, 测试复用本函数
# ---------------------------------------------------------------------------
def _exact_col_equal(a: pd.Series, b: pd.Series) -> int:
    """返回不一致元素个数。浮点两侧 NaN 视为相等、其余按位精确; 日期/字符串精确比。"""
    if pd.api.types.is_float_dtype(a.dtype) or pd.api.types.is_float_dtype(b.dtype):
        x = pd.to_numeric(a, errors="coerce").to_numpy(dtype="float64")
        y = pd.to_numeric(b, errors="coerce").to_numpy(dtype="float64")
        both_nan = np.isnan(x) & np.isnan(y)
        return int((~(both_nan | (x == y))).sum())
    sa = a.astype("string").fillna("")
    sb = b.astype("string").fillna("")
    return int((sa.to_numpy() != sb.to_numpy()).sum())


def _select_cols(name: str) -> str:
    _, model = DAY_VIEWS[name]
    return ", ".join(f"`{c}`" for c in _KEY_COLS + data_columns(model))


def compare_day(client, name: str, day: str) -> tuple[bool, str]:
    """比对某交易日 *_day 表 vs *_day_view 视图(行集合 + 逐列精确)。返回 (ok, message)。"""
    cols = _select_cols(name)
    order = "ORDER BY code"
    tbl = query_df(
        client, f"SELECT {cols} FROM {DATABASE}.{name} WHERE day = '{day}' {order}"
    )
    vw = query_df(
        client, f"SELECT {cols} FROM {DATABASE}.{name}_view WHERE day = '{day}' {order}"
    )
    if len(tbl) != len(vw):
        return False, f"{name}@{day}: 行数 table={len(tbl)} view={len(vw)}"
    if tbl.empty:
        return True, f"{name}@{day}: 0 行(两侧均空)"
    tbl = tbl.sort_values("code", kind="stable").reset_index(drop=True)
    vw = vw.sort_values("code", kind="stable").reset_index(drop=True)
    bad_cols = []
    for c in tbl.columns:
        nbad = _exact_col_equal(tbl[c], vw[c])
        if nbad:
            bad_cols.append(f"{c}({nbad})")
    if bad_cols:
        return False, f"{name}@{day}: 列不一致 {', '.join(bad_cols)}"
    return True, f"{name}@{day}: {len(tbl)} 行 × {len(tbl.columns)} 列 一致"


def consistency_guard(
    client, sample_days: list[str], names: list[str] | None = None, verbose: bool = True
) -> bool:
    """对样本交易日逐日比对所有 *_day 表 vs 视图。全部一致返回 True, 否则 False。

    需 *_day_view 视图存在(ensure_views / full_build(with_views=True) 已建)。
    """
    ok_all = True
    for name in names or list(DAY_VIEWS):
        for day in sample_days:
            ok, msg = compare_day(client, name, day)
            ok_all = ok_all and ok
            if verbose or not ok:
                print(("  OK  " if ok else "  BAD ") + msg)
    return ok_all


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    from .db import auth_from_env, get_client

    p = argparse.ArgumentParser(description="*_day 物化表 构建/刷新/校验")
    sub = p.add_subparsers(dest="cmd", required=True)

    pb = sub.add_parser(
        "full-build", help="离线全量物化(DROP+CREATE+INSERT+OPTIMIZE FINAL)"
    )
    g = pb.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true", help="物化全部 4 张 *_day 表")
    g.add_argument("--name", choices=list(DAY_VIEWS), help="只物化指定一张")
    pb.add_argument(
        "--no-views", action="store_true", help="不一并创建 *_day_view 校验视图"
    )

    pr = sub.add_parser("refresh", help="增量原子刷新(staging + REPLACE PARTITION)")
    pr.add_argument("--lookback-days", type=int, default=760)

    pg = sub.add_parser("guard", help="表 vs 视图逐列精确校验(tol=0)")
    pg.add_argument(
        "--days", nargs="+", required=True, help="样本交易日 YYYY-MM-DD ..."
    )

    args = p.parse_args()
    auth_from_env()
    client = get_client()

    if args.cmd == "full-build":
        if args.all:
            full_build_all(client, with_views=not args.no_views)
        else:
            full_build(client, args.name, with_views=not args.no_views)
    elif args.cmd == "refresh":
        refresh_incremental(client, lookback_days=args.lookback_days)
    elif args.cmd == "guard":
        ok = consistency_guard(client, args.days)
        if not ok:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
