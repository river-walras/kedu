"""指数回补进度速览:每类已处理指数数 / 行数 / 扫描前沿。

进度来源:
- 权重 / 成分:index_sync_state(dataset, index_code, covered_until)逐指数游标。
- 估值:本实现按 index_valuation.max(day) 逐码续传,不写 sync_state,故看 index_valuation 表。

用法(随时另开一个终端跑,只读、不耗配额):
    uv run --env-file .env python scripts/index_progress.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kedu.db import DATABASE, auth_from_env, get_client  # noqa: E402


def main() -> None:
    auth_from_env()
    c = get_client()

    def one(sql, default=(0, None)):
        try:
            return c.query(sql).result_rows[0]
        except Exception:  # noqa: BLE001  表还没建/为空
            return default

    total = one(f"SELECT count() FROM {DATABASE}.securities WHERE type='index'", (0,))[0]
    print(f"指数宇宙: {total} 只 (securities type='index')")
    print("-" * 60)

    vc, vmax = one(f"SELECT uniqExact(code), max(day) FROM {DATABASE}.index_valuation")
    print(f"估值 valuation : {vc} 只指数已落, 最新 day {vmax}  (按 index_valuation 续传)")

    for ds, label in (("weight", "权重 weights"), ("member", "成分 members")):
        done, frontier = one(
            f"SELECT count(), max(covered_until) FROM {DATABASE}.index_sync_state FINAL "
            f"WHERE dataset='{ds}'")
        pct = (100 * done // total) if total else 0
        print(f"{label} : {done}/{total} 指数 ({pct}%), 扫描前沿 {frontier}")

    wr, wi, wmax = one(
        f"SELECT count(), uniqExact(index_code), max(weight_date) FROM {DATABASE}.index_weights",
        (0, 0, None))
    print(f"  └ index_weights : {wr} 行 / {wi} 只有权重 / 最新披露 {wmax}")

    seg, segi = one(
        f"SELECT count(), uniqExact(index_code) FROM {DATABASE}.index_member_seg", (0, 0))
    hist, histi = one(
        f"SELECT count(), uniqExact(index_code) FROM {DATABASE}.index_member_history", (0, 0))
    print(f"  └ member_seg    : {seg} 段行 / {segi} 指数(staging)")
    print(f"  └ member_history: {hist} 区间 / {histi} 指数(读侧)")


if __name__ == "__main__":
    main()
