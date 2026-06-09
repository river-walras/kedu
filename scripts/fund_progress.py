"""基金回补进度速览:各 fund_* 表行数 + fund_net_value 的 max(day)/逐年行数。

另开一个终端跑(只读,不影响正在回补的进程):
  uv run --env-file .env python scripts/fund_progress.py
持续盯:
  watch -n 10 uv run --env-file .env python scripts/fund_progress.py

进度信号:fund_net_value 的 max(day) 随按周增量上升(~2021 -> 2026 即接近完成);
行数为 ReplacingMergeTree 各 part 之和(含 180 天重叠重拉的未合并版本),仅作量级参考。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kedu.db import DATABASE as D  # noqa: E402
from kedu.db import auth_from_env, get_client  # noqa: E402

TABLES = ["fund_main_info", "fund_net_value", "fund_fin_indicator", "fund_portfolio",
          "fund_portfolio_bond", "fund_portfolio_stock", "fund_invest_target",
          "fund_dividend", "fund_share_daily", "fund_mf_daily_profit"]


def main() -> None:
    auth_from_env()
    c = get_client()
    exist = {r[0] for r in c.query(
        f"SELECT name FROM system.tables WHERE database='{D}'").result_rows}
    for t in TABLES:
        if t not in exist:
            print(f"  {t:24} (未建)")
            continue
        n = c.query(f"SELECT count() FROM {D}.{t}").result_rows[0][0]
        extra = ""
        if t in ("fund_net_value", "fund_share_daily"):
            col = "day" if t == "fund_net_value" else "date"
            rng = c.query(f"SELECT min({col}), max({col}) FROM {D}.{t}").result_rows[0]
            extra = f"  {col}=[{rng[0]} .. {rng[1]}]"
        print(f"  {t:24} {n:>13,} 行{extra}")


if __name__ == "__main__":
    main()
