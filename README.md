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

## MCP server

把 kedu 暴露给 Claude Code / Codex 等 MCP 客户端。分层混合设计：12 个高频 API 直出 tool，
长尾 API 走 `kedu_call` 反射 dispatcher，`kedu_describe` 做发现。

**不提供裸 SQL 通道**：每条出口最终都落在 `kedu.*` 上。直接打 ClickHouse 会绕开复权动态锚、
成员资格语义、`report_type` 多版本去重、行业区间折叠等处理，拿到的数字看着正常但是错的。

MCP server 走可选 extra，装 `kedu[mcp]` 即可；`kedu-mcp` 是包暴露的入口点。
凭证从环境变量读取（`auth_from_env`），写在 MCP 配置的 `env` 块里即可，不需要 `--env-file`。

需要 `kedu>=0.1.3`。

### 注册到客户端

Claude Code（`~/.claude.json`）：

```json
{
  "mcpServers": {
    "kedu": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "kedu[mcp]", "kedu-mcp"],
      "env": {
        "CLICKHOUSE_HOST": "127.0.0.1",
        "CLICKHOUSE_PORT": "8123",
        "CLICKHOUSE_USER": "admin",
        "CLICKHOUSE_PASSWORD": "<password>",
        "CLICKHOUSE_DATABASE": "jqdata"
      }
    }
  }
}
```

Codex（`~/.codex/config.toml`）：

```toml
[mcp_servers.kedu]
command = "uvx"
args = ["--from", "kedu[mcp]", "kedu-mcp"]
startup_timeout_sec = 20.0
tool_timeout_sec = 60.0

[mcp_servers.kedu.env]
CLICKHOUSE_HOST = "127.0.0.1"
CLICKHOUSE_PORT = "8123"
CLICKHOUSE_USER = "admin"
CLICKHOUSE_PASSWORD = "<password>"
CLICKHOUSE_DATABASE = "jqdata"
```

装进某个项目而不是临时拉起，用 `uv add 'kedu[mcp]'`，客户端 `command` 改成 `kedu-mcp`。

### 从源码跑（开发用）

在仓库根目录：

```bash
uv sync --extra mcp
uv run --env-file .env kedu-mcp                      # stdio
uv run --env-file .env kedu-mcp --transport http     # streamable-http
```

要让客户端指向工作副本而不是 PyPI 版本，把上面配置的 `command` 换成 `uv`、
`args` 换成 `["run", "--directory", "<仓库路径>", "--extra", "mcp", "kedu-mcp"]`，
`env` 块不变。或者 `uv tool install --editable '.[mcp]'` 装一个跟随源码的全局
`kedu-mcp`（卸载 `uv tool uninstall kedu`）。

**别用 `uvx --from '<本地路径>[mcp]'`。** uv 会把本地路径构建出的 wheel 连同整个 tool
环境按需求串缓存住，改了源码不重建，`--refresh` / `--reinstall` / `--refresh-package`
三个都救不回来。这个缓存语义对 PyPI 上的不可变版本是对的，对本地路径是陷阱。

### HTTP 常驻

多个客户端共享一个进程时用 streamable-http，pm2 编排见 `ecosystem.config.js` 的 `kedu-mcp`
（走仓库工作副本，只监听 `127.0.0.1`）：

```bash
pm2 start ecosystem.config.js --only kedu-mcp
pm2 save
```

客户端侧对应写 `"type": "http", "url": "http://127.0.0.1:8000/mcp"`。

### tool 一览

| tool | 说明 |
| --- | --- |
| `kedu_get_price` | 行情，默认 `fq='pre'` |
| `kedu_get_fundamentals` | 交易日截面基本面（DSL） |
| `kedu_get_fundamentals_continuously` | 连续多日基本面（DSL） |
| `kedu_get_history_fundamentals` | 多报告期基本面历史（字段表达式列表） |
| `kedu_finance_run_query` | `STK_*` / `FUND_*` 原始表（DSL，`offset=True` 走分页） |
| `kedu_get_valuation` | 估值 |
| `kedu_get_all_securities` | 证券列表 |
| `kedu_get_trade_days` | 交易日历 |
| `kedu_get_industry` / `kedu_get_industry_stocks` | 行业归属 / 成分 |
| `kedu_get_index_stocks` | 指数成分 |
| `kedu_get_extras` | `is_st` 与基金净值 |
| `kedu_describe` | API 目录、函数签名、finance 表字段、DSL 可用名字 |
| `kedu_call` | 反射调用其余长尾 API |
| `kedu_plot` | 交互式图表（MCP Apps），目前只有 `chart='kline'` |

### 图表（MCP Apps）

`kedu_plot` 走 [MCP Apps 扩展](https://modelcontextprotocol.io/docs/extensions/apps)
（`io.modelcontextprotocol/ui`）：工具的 `_meta.ui.resourceUri` 指向一个 `ui://` 资源，
host 取回 HTML 在沙箱 iframe 里渲染，数据经 `structuredContent` 推给 iframe。

- **数据不进模型。** `content` 只有一行摘要(标的、区间、复权口径),全量 option 与数据
  在 `structuredContent` 里。
- **双方都声明扩展。** 服务端在 `initialize` 的 `capabilities.extensions` 里声明
  `io.modelcontextprotocol/ui` —— 规范要求双向声明,只挂 tool 的 `_meta.ui` 不够,
  严格的 host 会因此根本不去取 `ui://` 资源。
- **客户端不支持就退回文本。** 没在 `initialize` 里声明该扩展时,`kedu_plot` 返回的是同
  一份数据的 CSV,不会把 envelope 倒进上下文。Claude Code 目前不在支持矩阵里。
  少数 host 不声明却照样渲染 App(实测 MCPJam 1.5.17),这种情况下设
  `KEDU_MCP_FORCE_UI=1` 强制走图表分支 —— 它放宽的是安全默认值,只在确知对端会渲染时开。
- **渲染器整包内联**(约 1.39 MiB),不出网也不需要额外开端口 —— 见
  `src/kedu/mcp_server/static/VENDOR.md`。资源 URI 带内容哈希,改了 HTML 缓存自然失效;
  但规范只允许 host 缓存、不保证缓存(MCPJam 1.5.17 每次工具调用都重新读一遍)。
- **前复权锚会画在副标题上。** `fq='pre'` 是动态值，锚由 `get_fq_anchor` 从复权语义层单独
  取（不能拿查询窗口最后一天冒充），否则截图存档几天后数值就对不上。

### 查询 DSL

`get_fundamentals` 与 `finance.run_query` 的参数是 SQLAlchemy 查询对象，无法用 JSON 无损表达
（`or_` / `in_` / 跨表都会丢），所以这两类 tool 收表达式字符串：

```text
query(valuation.code, valuation.pe_ratio).filter(valuation.pe_ratio < 10).limit(50)
query(STK_INCOME_STATEMENT).filter(STK_INCOME_STATEMENT.code == '000001.XSHE').limit(20)
```

求值走受限 eval，三道闸：`mode="eval"` 编译（写不出 import/赋值）、`__builtins__` 置空、
AST 预扫禁掉一切下划线名字与属性（堵死 `().__class__.__mro__` 逃逸链）。
可用名字用 `kedu_describe(dsl=True)` 查。

这是**本地单用户**工具的取舍。若要把 HTTP transport 暴露到可信边界之外，应改成结构化 filter 描述。

### 出站上限

结果超限即截断，并在正文开头给出醒目提示与收窄建议——不静默截断，也不直接报错。

- 默认 200 行 / 40 列，硬上限 5000 行 / 400 列
- 环境变量 `KEDU_MCP_MAX_ROWS` / `KEDU_MCP_MAX_COLS`，或启动参数 `--max-rows` / `--max-cols`
- 单次调用可用 tool 的 `max_rows` / `max_cols` 参数临时抬高

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
