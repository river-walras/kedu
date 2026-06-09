# kedu

本项目在本地 ClickHouse 上复刻聚宽 `jqdatasdk` 的常用 API。查询全部走本地库 `jqdata`，不依赖在线接口；数据由回补脚本从聚宽拉取后落库。

当前包名是 `kedu`，不是 `jqdata`：

```python
from kedu import auth, query, valuation, get_price, get_fundamentals

auth("default", "<password>")
df = get_fundamentals(query(valuation).limit(5), date="2024-05-10")
```

## 已经复刻可用的 API

### 认证与查询表面

```python
from kedu import (
    auth,
    auth_from_env,
    get_client,
    query,
    income,
    balance,
    cash_flow,
    indicator,
    valuation,
)
```

### 基本面

```python
from kedu import (
    get_fundamentals,
    get_fundamentals_continuously,
    get_history_fundamentals,
)
```

### 行情

```python
from kedu import get_price
```

已支持：

- `frequency='daily'`
- `frequency='1m'`，前提是本地已回补 `bar_1m`
- `fq=None`、`fq='post'`、`fq='pre'`
- 股票、指数、场内基金日线；分钟线按已入库标的可查

### 交易日历与证券列表

```python
from kedu import get_trade_days, get_all_trade_days, get_all_securities
```

`get_all_securities` 已覆盖：

- `stock`
- `index`
- `fund`
- `etf`
- `lof`
- `fja`

其中 `fund` 会展开到场内基金细分类型。

### finance 模块

```python
from kedu import finance, get_table_info
```

已支持：

- `finance.run_query`
- `finance.run_offset_query`
- `finance.get_table_info`
- 顶层别名 `get_table_info`

已复刻的 `finance` 表：

- 股票报告期/公告类：
  `STK_INCOME_STATEMENT`,
  `STK_BALANCE_SHEET`,
  `STK_CASHFLOW_STATEMENT`,
  `STK_CASH_FLOW_STATEMENT`,
  `STK_FIN_FORCAST`,
  `STK_AUDIT_OPINION`,
  `STK_REPORT_DISCLOSURE`,
  `STK_STATUS_CHANGE`,
  `STK_EXCHANGE_TRADE_INFO`,
  `STK_MT_TOTAL`
- 上市公司基本信息类：
  `STK_COMPANY_INFO`,
  `STK_LIST`,
  `STK_NAME_HISTORY`,
  `STK_EMPLOYEE_INFO`,
  `STK_HOLDER_NUM`,
  `STK_LIMITED_SHARES_LIST`,
  `STK_LIMITED_SHARES_UNLIMIT`,
  `STK_SHAREHOLDERS_SHARE_CHANGE`,
  `STK_CAPITAL_CHANGE`,
  `STK_SHARES_FROZEN`,
  `STK_SHAREHOLDER_TOP10`,
  `STK_SHAREHOLDER_FLOATING_TOP10`
- 基金类：
  `FUND_MAIN_INFO`,
  `FUND_NET_VALUE`,
  `FUND_FIN_INDICATOR`,
  `FUND_PORTFOLIO`,
  `FUND_PORTFOLIO_BOND`,
  `FUND_PORTFOLIO_STOCK`,
  `FUND_INVEST_TARGET`,
  `FUND_DIVIDEND`,
  `FUND_SHARE_DAILY`,
  `FUND_MF_DAILY_PROFIT`

### 行业、概念、指数

```python
from kedu import (
    get_industries,
    get_industry_stocks,
    get_history_industry,
    get_industry,
    get_concepts,
    get_concept_stocks,
    get_concept,
    get_index_stocks,
    get_index_weights,
    get_index_valuation,
)
```

### 融资融券、解禁、额外字段

```python
from kedu import (
    get_mtss,
    get_margincash_stocks,
    get_marginsec_stocks,
    get_locked_shares,
    get_extras,
)
```

`get_extras` 当前只支持这些 `info`：

- `is_st`
- `unit_net_value`
- `acc_net_value`
- `adj_net_value`

## 安装

```bash
uv sync
```

## 认证

交互式使用：

```python
from kedu import auth

auth("default", "<clickhouse_password>")
```

脚本使用环境变量：

```bash
export CLICKHOUSE_USER=default
export CLICKHOUSE_PASSWORD=your_password
export CLICKHOUSE_HOST=localhost
export CLICKHOUSE_PORT=8123
export CLICKHOUSE_DATABASE=jqdata
```

然后在脚本里：

```python
from kedu import auth_from_env

auth_from_env()
```

`get_client()` 在未认证时会直接报错，不会静默读取环境变量。

## 常用命令

```bash
UV_CACHE_DIR=/tmp/uv uv run pytest
UV_CACHE_DIR=/tmp/uv uv run ruff check .
UV_CACHE_DIR=/tmp/uv uv run ruff format .
```

单测示例：

```bash
UV_CACHE_DIR=/tmp/uv uv run pytest tests/test_fundamentals.py -xvs
UV_CACHE_DIR=/tmp/uv uv run pytest tests/test_index.py -xvs
```

## 数据回补脚本

仓库内常用脚本：

- `scripts/rebuild_from_jq.py`：全量回补基本面、日线、可选分钟线
- `scripts/backfill_stk.py`：回补 `finance` 的股票类表
- `scripts/backfill_fund.py`：回补基金类表、基金日线
- `scripts/backfill_index.py`：回补指数成分、权重、估值
- `scripts/backfill_industry.py`：回补行业、概念及其历史成分
- `scripts/backfill_margin.py`：回补融资融券数据
- `scripts/backfill_locked_shares.py`：回补限售解禁数据
- `scripts/backfill_jq.py`：回补交易日历、估值、`is_st` 等
- `scripts/update_jqdata.py`：盘后增量更新入口
- `scripts/status.py`：检查各表新鲜度

## 校验

测试会把本地结果和线上 `jqdatasdk` 逐字段比对。已覆盖的模块包括：

- 基本面三组接口
- `finance.run_query` / `run_offset_query`
- `get_price` 日线、基金日线、可选分钟线
- 交易日历、证券列表
- 行业、概念、指数
- 融资融券
- `get_locked_shares`
- `get_extras`

运行全部测试：

```bash
UV_CACHE_DIR=/tmp/uv uv run pytest
```
