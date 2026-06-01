# jqdata — 聚宽 jqdatasdk 的本地 ClickHouse 复刻

在本地 ClickHouse(库名 `jqdata`)上复刻聚宽 `jqdatasdk` 的常用接口,返回结果与线上 `jqdatasdk` **逐字段一致**,且无网络/配额依赖。数据由脚本从聚宽拉取后落库,查询全部走本地 ClickHouse。

## 已复刻的接口(`jqdata`)

```python
from jqdata import (
    auth,                              # 设定 ClickHouse 凭证(仿 jqdatasdk.auth)
    get_fundamentals,                  # 单日/单报告期基本面
    get_fundamentals_continuously,     # 多交易日基本面
    get_history_fundamentals,          # 多季/多年报告期
    finance,                           # finance.run_query / run_offset_query / get_table_info(STK_* 原始报告期表)
    get_price,                         # 行情(单标的或 list;frequency='daily' 读 bar_1d / '1m' 读 bar_1m;前/后/不复权)
    get_trade_days, get_all_trade_days,# 交易日历
    get_all_securities,                # 证券列表(股票)
    query, income, balance, cash_flow, indicator, valuation,
)

auth("default", "<password>")          # 或先 export 环境变量后 jqdata.auth_from_env()
df = get_fundamentals(query(valuation, income), date="2022-01-11")
```

> `get_client()` 在未 `auth()` 时直接报错——不静默回退环境变量。脚本/pm2 用 `auth_from_env()` 显式从 `CLICKHOUSE_*` 环境变量拉起。

## 目录结构

```
src/jqdata/        # 包:接口实现(db/auth、fundamentals、finance、prices、calendar、securities、schema 等)
scripts/            # 建库 + 更新脚本
  rebuild_from_jq.py        # 全量重建(基本面 + 日线行情)
  backfill_stk.py           # STK_* 原始报告期表(finance.run_query 底表)幂等同步
  backfill_jq.py            # 基本面/估值回补 + 交易日历同步(--trade-days)
  update_jqdata.py          # 盘后增量更新(pm2 入口;trade_days→securities→基本面→估值→行情→STK_*)
  status.py                 # 各表数据新鲜度核查
  import_factors_to_clickhouse.py / check_missing_factor_stocks.py   # 因子表(独立数据,非复刻接口)
tests/              # pytest 用例和线上校验实现
reference/          # 字段/规则 schema 文档
ecosystem.config.js # pm2 调度(盘后增量)
```

## 安装与配置

```bash
uv sync                        # 创建 venv、装依赖、editable 安装本包(src 布局)
cp .env.example .env           # 填入 ClickHouse 与 JoinQuant 凭证(.env 已被 gitignore)
```

需要凭证的脚本统一用 env-file 注入运行:

```bash
uv run --env-file .env python scripts/status.py
```

## 建库(首次)

```bash
uv run --env-file .env python scripts/rebuild_from_jq.py --fundamentals --bars   # 基本面 + 日线行情
uv run --env-file .env python scripts/backfill_stk.py                            # STK_* 报告期表
uv run --env-file .env python scripts/backfill_jq.py --trade-days                # 交易日历
```

分钟线 bar_1m 量巨大(全市场每交易日 ~132 万行,全历史数十亿行),单独回补、可断点续跑(按 标的×年 跳过已存在):

```bash
# 全历史(极重、配额巨大,建议后台 + 分批;可用 --bars-1m-year 单年、--limit-codes 测试)
uv run --env-file .env python scripts/rebuild_from_jq.py --bars-1m
uv run --env-file .env python scripts/rebuild_from_jq.py --bars-1m --bars-1m-year 2026   # 单年批
```

## 增量更新(每日盘后)

由 pm2 调度 `scripts/update_jqdata.py`(北京时间 18:30 盘后;服务器为 UTC,故 cron 用 `30 10 * * 1-5`):步骤 = trade_days → securities → 报告期基本面 → 估值 → bar_1d → STK_* → OPTIMIZE → **bar_1m(最后,最重)**。

```bash
pm2 start ecosystem.config.js && pm2 save     # 首次注册并持久化
pm2 logs jqdata-update                         # 看日志
```

> `bar_1m` 增量随每日更新追加(`--skip-bars-1m` 可关);空表时只从当日起,不在每日更新里回补历史(历史走上面的 `rebuild_from_jq.py --bars-1m`)。

## 校验(与线上逐字段一致)

测试按值校验本地结果与线上 `jqdatasdk` 逐字段一致。抽查策略:基本面/finance/history 用
固定代表股+确定性均匀抽样 100 票;日线按 `--price-scale`(默认 heavy=50 票,窗口由本地
factor 变化推导的除权事件窗口构成);日历/证券列表在 JQ 不计配额,放开覆盖。

**快照缓存**:首次跑把 live 响应落盘 `tests/_snapshots/`(已 gitignore),重跑命中缓存=0
配额;`--refresh-snapshots` 强制重拉。纯命中跑只需 ClickHouse 凭证。会话末打印实际 live
调用次数与剩余 `get_query_count()`。

```bash
# 首次:落快照(消耗 JQ 配额,约数万条;末尾打印实际配额)
uv run --env-file .env pytest tests -q --refresh-snapshots
# 之后重跑:命中缓存,0 配额
uv run --env-file .env pytest tests -q

uv run --env-file .env pytest tests/test_calendar.py tests/test_securities.py -q   # 交易日历 + 证券列表(免费)
uv run --env-file .env pytest tests/test_fundamentals.py -q                        # get_fundamentals 严格值校验
uv run --env-file .env pytest tests/test_finance.py -q                             # finance.run_query / STK_*
uv run --env-file .env pytest tests/test_history.py -q                             # continuously / history
uv run --env-file .env pytest tests/test_price.py -q --price-scale medium          # 日线(可调档位)
# bar_1m 未入库,默认 skip;先回补窗口后显式启用:
uv run --env-file .env pytest tests/test_bars_1m.py --run-bars-1m --bars-1m-start 2026-05-26 --bars-1m-end 2026-05-29
```
