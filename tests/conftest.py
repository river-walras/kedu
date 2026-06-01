"""pytest 配置:CLI 选项 + 共享 fixture。

设计要点:
- ClickHouse 凭证始终需要(fail-fast,不静默回退);缺失则整体 skip。
- JQ 凭证只在「快照 miss、真要拉 live」时惰性需要(见 _snapshot.ensure_jq_auth),
  所以纯缓存命中的跑无需 JQ 凭证。
- 会话末打印实际打到 live 的次数与 JQ 配额消耗,给出地面真值(校准流量预估)。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._snapshot import Snapshotter, live_calls  # noqa: E402


# ---------------------------------------------------------------------------
# CLI 选项
# ---------------------------------------------------------------------------
def pytest_addoption(parser):
    g = parser.getgroup("kedu 校验")
    g.addoption(
        "--refresh-snapshots",
        "--refresh",
        action="store_true",
        dest="refresh_snapshots",
        default=False,
        help="忽略 tests/_snapshots 缓存,强制重新拉 live 并覆盖(消耗 JQ 配额)。别名 --refresh。",
    )
    g.addoption(
        "--price-scale",
        choices=["light", "medium", "heavy"],
        default="heavy",
        help="get_price 日线抽样档位:light=20票/medium=30票/heavy=50票(默认 heavy)。",
    )
    g.addoption("--fund-sample", type=int, default=100, help="基本面/finance/history 抽样票数。")
    g.addoption(
        "--run-bars-1m",
        action="store_true",
        default=False,
        help="启用 bar_1m 校验(默认 skip:bar_1m 尚未入库,需先回补窗口)。",
    )
    # bar_1m 窗口参数(仅 --run-bars-1m 时生效)
    g.addoption("--bars-1m-codes", default="000001.XSHE,600519.XSHG")
    g.addoption("--bars-1m-start", default="2026-05-26")
    g.addoption("--bars-1m-end", default="2026-05-29")
    g.addoption("--bars-1m-fq", default="none,post")


# ---------------------------------------------------------------------------
# 凭证 / 数据库
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def clickhouse_auth():
    """显式从环境拉起 ClickHouse 凭证;缺失则 skip(fail-fast,不回退默认值)。"""
    required = ("CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD")
    missing = [n for n in required if not os.getenv(n)]
    if missing:
        pytest.skip(
            "kedu 校验需要 ClickHouse 凭证 " + ", ".join(missing)
            + ";请用 `uv run --env-file .env pytest tests`"
        )
    from kedu.db import auth_from_env

    auth_from_env()


@pytest.fixture(scope="session")
def snap(request):
    """绑定 --refresh-snapshots 的快照取数器;命中读盘,miss 时惰性 JQ auth。"""
    return Snapshotter(refresh=request.config.getoption("--refresh-snapshots"))


# ---------------------------------------------------------------------------
# 抽样
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def sample_codes(clickhouse_auth, request):
    from tests._sampling import sample_codes as _sc

    return _sc(request.config.getoption("--fund-sample"))


@pytest.fixture(scope="session")
def price_codes(clickhouse_auth, request):
    from tests._sampling import price_codes as _pc

    scale = request.config.getoption("--price-scale")
    n = {"light": 20, "medium": 30, "heavy": 50}[scale]
    return _pc(n)


# ---------------------------------------------------------------------------
# 配额报告(会话级)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session", autouse=True)
def _quota_report():
    """会话末打印实际打到 live 的次数与 JQ 配额前后差(仅当发生过 live 调用)。"""
    yield
    n = live_calls()
    if n == 0:
        print("\n[quota] 全部命中快照缓存,未消耗 JQ 配额。")
        return
    try:
        from jqdatasdk import get_query_count

        spare = get_query_count()
    except Exception as e:  # noqa: BLE001
        print(f"\n[quota] live 调用 {n} 次;get_query_count 读取失败:{e}")
        return
    print(f"\n[quota] live 调用 {n} 次;当前剩余 get_query_count={spare}")
