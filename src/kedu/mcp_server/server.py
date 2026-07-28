"""kedu MCP server —— 聚宽兼容 API + 受限只读 ClickHouse SQL.

分层的理由: kedu 的价值全在语义层(复权动态锚、成员资格、report_type 多版本、
行业 walk 折叠), 不在数据本身。高层 tool 继续提供与 jqdatasdk 对齐的语义；
`kedu_query` 则为跨表、窗口、聚合等复杂分析提供完整 ClickHouse SELECT 能力。
SQL 通道只使用独立只读账户，限定在 jqdata 数据域，不执行任何管理或写入操作。

四层:
1. 高频 API 直出 tool(12 个) —— 完整 JSON Schema, 模型靠 name/description 就能选中。
2. `kedu_call` 反射 dispatcher —— 兜住 __all__ 里剩下的长尾 API, 走的仍是 kedu.*,
   语义不丢; 新增 kedu API 时这里零改动。
3. `kedu_query` —— 原生 ClickHouse SELECT，SQL 不转译、不改写业务逻辑。
4. `kedu_describe` —— 列 API 目录、查函数签名、查表结构与语义、列 DSL 可用名字。

出站统一限制行列数，超限即截断并给出显式提示。
"""

from __future__ import annotations

import argparse
import contextlib
import inspect
import os
import sys
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations

import kedu

from ..db import auth_from_env
from ..finance import finance
from ..finance_schema import RUN_QUERY_TABLES
from . import _app, _dsl, _plot, _query
from ._invoke import DSL_TOOL_REDIRECT as _DSL_TOOL_REDIRECT
from ._invoke import dispatchable as _dispatchable
from ._invoke import invoke_kedu
from ._invoke import summary as _summary
from ._render import HARD_MAX_ROWS, render, resolve_limits

INSTRUCTIONS = """\
# 目标

使用本地 jqdata 数据完成用户的数据查询。返回实际查询结果；只有用户明确要求 SQL 时才只
返回 SQL。不要猜测表名、字段、数据粒度或聚宽业务语义。

# 工具选择

- 如果需求可由 kedu_get_* 直接完成，必须优先调用对应 tool，以保留 jqdatasdk 的复权、
  可见报告、成员资格和返回格式语义。
- 如果需求对应未直接暴露的 kedu API，先用 kedu_describe(api=...) 核对签名，再用
  kedu_call。不要用 kedu_call 调用已有专用 tool。
- 只有在需要跨表 JOIN、窗口函数、自定义聚合、复杂子查询或原始关系字段时，才使用
  kedu_query。
- 如果不知道可用 API，调用不带参数的 kedu_describe。如果不知道 SQL 表，调用
  kedu_describe(table='*')；如果不知道字段、引擎或表语义，使用精确表名再次调用。
- 只有用户需要图表时才调用 kedu_plot。不要把查询出的逐行数据传给 kedu_plot；由它按
  source 自行取数。

# SQL 工作流

1. 从请求中确定结果粒度、标的范围、时间范围、指标和排序。缺少会实质改变结果的条件时，
   先向用户确认；能从上下文或表结构确定时不要提问。
2. 查询当前上下文中尚未核对过的表结构。不要根据相似表或 jqdatasdk 字段名猜物理字段。
3. 写一条只读 SELECT 或 EXPLAIN SELECT。显式选择字段、JOIN 键、日期边界和排序；对可能
   产生多对多扩张的 JOIN，先按目标粒度聚合或验证键唯一性。
4. 所有用户值都通过 ClickHouse typed placeholder 和 parameters 传入。不要把代码、日期、
   名称或列表拼接进 SQL。
5. 大表探索先使用窄日期范围、聚合或 LIMIT。只有结果本身需要更多明细时才提高 max_rows。
6. 读取返回头中的 types、truncated、WARNING 和统计信息。如果 truncated=true，结果不完整；
   改用聚合、分页条件或更窄范围后重新查询，不要基于截断样本声称全量结论。
7. 检查结果粒度和关键计数是否符合请求。复杂聚合无法从结果直接验证时，执行一个聚焦的
   count()、去重计数或分组检查。

# 必须遵守的查询语义

- kedu_query 不会改写 SQL，也不会自动添加复权、FINAL、report_type 或成员有效期条件。
- bar_1d 和 bar_1m 存储原始价格。需要与 get_price 一致的价格时优先用 kedu_get_price；
  必须用 SQL 时，显式处理 factor、停牌和动态前复权锚。
- 查询 finance 原始表时，显式选择 report_type 和报告版本。查询财务可见性时，确保
  pubDate <= 查询日，并按所需口径选择 statDate；或改用 kedu_get_fundamentals。
- 查询成员历史时使用右端包含区间：start_date <= day AND
  (end_date IS NULL OR end_date >= day)。查询权重时显式选择所需快照日。
- kedu_describe 标为 FINAL 的关系在要求确定快照时使用 FINAL。不要对所有表机械添加 FINAL。
- 日期按中国市场交易日语义解释。不要把自然日数量当作交易日数量。

# 安全边界与失败处理

- kedu_query 只接受一条 SELECT 或 EXPLAIN SELECT，数据库固定为 jqdata。服务端会拒绝
  DDL、DML、system 库、跨库引用、SETTINGS、导出及未允许的表函数。不要尝试绕过拒绝。
- SQL 因未知表或字段失败时，调用 kedu_describe 核对后修正。因查询规模超时或结果过大
  失败时，收窄范围、提前聚合或拆成有明确边界的查询；不要盲目重复同一查询。
- 凭据缺失、readonly 未生效或服务不可用时，报告具体错误。不要改用 CLICKHOUSE_USER，
  不要声称已经取得数据。

# 结果解释

- kedu_query 返回元数据头和 CSV：\\N 表示 NULL，types 给出 ClickHouse 原生类型，WARNING
  表示需要处理或向用户披露的语义风险。
- 最终回答必须说明实际采用的时间范围、结果粒度和关键过滤条件。只在它们影响结论时说明
  WARNING、截断或无法验证的部分。
- get_price 默认 fq='pre'，前复权锚定全表最新交易日，是动态值；fq=None 为不复权，
  fq='post' 为后复权。
- 基本面和 finance 专用 tool 的 query_expr 使用聚宽 DSL。先用 kedu_describe(dsl=True)
  获取可用名字，不要把 ClickHouse SQL 传给 query_expr。
"""


def build_server(**settings: Any) -> FastMCP:
    """装配并返回 FastMCP 实例. settings 透传 host/port/streamable_http_path 等."""
    mcp = FastMCP("kedu", instructions=INSTRUCTIONS, **settings)
    # MCP Apps 要双方在 initialize 里都声明扩展才算协商成功; 只在 tool 上挂 _meta.ui
    # 不够, 严格的 host 会因此根本不去取 ui:// 资源。
    _app.declare_ui_extension(mcp)

    # ------------------------------------------------------------------
    # 第一层: 高频 API 直出 tool
    # ------------------------------------------------------------------

    @mcp.tool(name="kedu_get_price")
    def get_price(
        security: str | list[str],
        start_date: str | None = None,
        end_date: str | None = None,
        frequency: str = "daily",
        fields: list[str] | None = None,
        fq: str | None = "pre",
        count: int | None = None,
        skip_paused: bool = False,
        fill_paused: bool = True,
        max_rows: int | None = None,
    ) -> str:
        """获取股票/指数/场内基金的行情, 复刻 jqdatasdk.get_price。

        security 单代码(如 '000001.XSHE')或代码列表。frequency 支持 'daily' 与 '1m'。
        fq: 'pre' 前复权(默认, 动态锚定最新交易日) / 'post' 后复权 / None 不复权。
        fields 默认 open/close/high/low/volume/money 等; count 与 start_date 二选一。
        未给 start_date 且未给 count 时窗口默认 2015 全年(对齐聚宽)。
        单代码返回窄表(index 为时间), 多代码返回长表(列含 time/code)。
        """
        df = kedu.get_price(
            security,
            start_date=start_date,
            end_date=end_date,
            frequency=frequency,
            fields=fields,
            fq=fq,
            count=count,
            skip_paused=skip_paused,
            fill_paused=fill_paused,
        )
        return render(df, max_rows)

    @mcp.tool(name="kedu_get_fundamentals")
    def get_fundamentals(
        query_expr: str,
        date: str | None = None,
        statDate: str | None = None,
        max_rows: int | None = None,
        max_cols: int | None = None,
    ) -> str:
        """按聚宽 DSL 查某个交易日的基本面截面, 复刻 jqdatasdk.get_fundamentals。

        query_expr 是表达式字符串, 例如:
          query(valuation).filter(valuation.pe_ratio < 10).limit(50)
          query(valuation.code, valuation.market_cap, income.total_operating_revenue)
            .filter(valuation.code.in_(['000001.XSHE','600000.XSHG']))
        可查表: valuation / income / balance / cash_flow / indicator。
        date 与 statDate 二选一(date 为交易日截面, statDate 为报告期如 '2023q4')。
        注意: date 模式不返回当日尚无可见报告的次新股, 与聚宽一致。
        """
        df = kedu.get_fundamentals(
            _dsl.eval_query(query_expr), date=date, statDate=statDate
        )
        return render(df, max_rows, max_cols)

    @mcp.tool(name="kedu_get_fundamentals_continuously")
    def get_fundamentals_continuously(
        query_expr: str,
        end_date: str | None = None,
        count: int = 1,
        max_rows: int | None = None,
        max_cols: int | None = None,
    ) -> str:
        """查连续多个交易日的基本面, 复刻 jqdatasdk.get_fundamentals_continuously。

        query_expr 同 kedu_get_fundamentals。从 end_date 往前取 count 个交易日。
        """
        df = kedu.get_fundamentals_continuously(
            _dsl.eval_query(query_expr), end_date=end_date, count=count
        )
        return render(df, max_rows, max_cols)

    @mcp.tool(name="kedu_get_history_fundamentals")
    def get_history_fundamentals(
        security: str | list[str],
        fields: list[str],
        watch_date: str | None = None,
        stat_date: str | None = None,
        count: int = 1,
        interval: str = "1q",
        stat_by_year: bool = False,
        max_rows: int | None = None,
        max_cols: int | None = None,
    ) -> str:
        """查多标的多报告期的基本面历史, 复刻 jqdatasdk.get_history_fundamentals。

        fields 是字段表达式字符串列表, 只能取自财务报表四表
        income / balance / cash_flow / indicator, 例如
          ['income.total_operating_revenue', 'indicator.roe']
        valuation 是行情日频表, 不能用在这里(与聚宽一致), 估值请用 kedu_get_valuation。
        watch_date 与 stat_date 二选一; interval 如 '1q'/'4q'/'1y'。
        注意: watch_date 模式每票各自锚定, 会返回近期退市股, 但有全局新鲜度门槛。
        """
        df = kedu.get_history_fundamentals(
            security,
            _dsl.eval_fields(fields),
            watch_date=watch_date,
            stat_date=stat_date,
            count=count,
            interval=interval,
            stat_by_year=stat_by_year,
        )
        return render(df, max_rows, max_cols)

    @mcp.tool(name="kedu_finance_run_query")
    def finance_run_query(
        query_expr: str,
        offset: bool = False,
        max_rows: int | None = None,
        max_cols: int | None = None,
    ) -> str:
        """查 finance 的 STK_* / FUND_* 原始表, 复刻 finance.run_query。

        query_expr 是表达式字符串, finance 表可用裸表名或 finance.<表名>, 例如:
          query(STK_INCOME_STATEMENT).filter(
              STK_INCOME_STATEMENT.code == '000001.XSHE',
              STK_INCOME_STATEMENT.report_type == 0).limit(20)
        offset=True 走 run_offset_query(按 id 排序返回全部匹配)。
        表名清单与字段用 kedu_describe(table=...) 查。本地不套聚宽的 5000/20 万行上限。
        """
        q = _dsl.eval_query(query_expr)
        df = finance.run_offset_query(q) if offset else finance.run_query(q)
        return render(df, max_rows, max_cols)

    @mcp.tool(name="kedu_get_valuation")
    def get_valuation(
        security_list: str | list[str],
        start_date: str | None = None,
        end_date: str | None = None,
        fields: list[str] | None = None,
        count: int | None = None,
        max_rows: int | None = None,
    ) -> str:
        """获取多标的在交易日区间内的估值(市值表), 复刻 jqdatasdk.get_valuation。

        返回列序为 code、day、fields。fields 为空返回全字段。
        start_date 与 count 互斥; count 表示每票各取 end_date 前 count 个交易日。
        """
        df = kedu.get_valuation(
            security_list,
            start_date=start_date,
            end_date=end_date,
            fields=fields,
            count=count,
        )
        return render(df, max_rows)

    @mcp.tool(name="kedu_get_all_securities")
    def get_all_securities(
        types: str | list[str] | None = None,
        date: str | None = None,
        max_rows: int | None = None,
    ) -> str:
        """获取证券列表, 复刻 jqdatasdk.get_all_securities。

        types 为空只返回股票。可选 'stock'/'index'/'fund'/'etf'/'lof'/'fja' 等,
        'fund' 会展开到场内基金细分类型。date 给定时只返回该日仍在上市的证券。
        """
        df = kedu.get_all_securities(types or [], date=date)
        return render(df, max_rows)

    @mcp.tool(name="kedu_get_trade_days")
    def get_trade_days(
        start_date: str | None = None,
        end_date: str | None = None,
        count: int | None = None,
        max_rows: int | None = None,
    ) -> str:
        """获取交易日列表, 复刻 jqdatasdk.get_trade_days。

        含首尾。给 count 时 start_date 与 end_date 只能二选一:
        给 start_date 则往后取 count 个, 只给 end_date 则往前取 count 个。
        不给 count 时必须给 start_date。
        """
        days = kedu.get_trade_days(
            start_date=start_date, end_date=end_date, count=count
        )
        return render(days, max_rows)

    @mcp.tool(name="kedu_get_industry")
    def get_industry(
        security: str | list[str],
        date: str | None = None,
        df: bool = True,
        max_rows: int | None = None,
    ) -> str:
        """查股票在给定日期所属的各体系行业, 复刻 jqdatasdk.get_industry。

        df=True(默认)返回长表 code/type/industry_code/industry_name;
        df=False 返回 {code: {体系: {industry_code, industry_name}}} 嵌套 dict。
        date 默认北京今天。
        """
        return render(kedu.get_industry(security, date=date, df=df), max_rows)

    @mcp.tool(name="kedu_get_industry_stocks")
    def get_industry_stocks(
        industry_code: str, date: str | None = None, max_rows: int | None = None
    ) -> str:
        """获取某行业在给定日期的成分股列表, 复刻 jqdatasdk.get_industry_stocks。

        行业代码用 kedu_call('get_industries', {'name': 'sw_l1'}) 查(体系名如
        zjw / sw_l1 / sw_l2 / sw_l3 / jq_l1 / jq_l2)。date 默认北京今天。
        """
        return render(kedu.get_industry_stocks(industry_code, date=date), max_rows)

    @mcp.tool(name="kedu_get_index_stocks")
    def get_index_stocks(
        index_symbol: str, date: str | None = None, max_rows: int | None = None
    ) -> str:
        """获取某指数在给定日期的成分股, 复刻 jqdatasdk.get_index_stocks。

        如 '000300.XSHG'(沪深300)、'000905.XSHG'(中证500)。date 默认北京今天。
        权重用 kedu_call('get_index_weights', ...)。
        """
        return render(kedu.get_index_stocks(index_symbol, date=date), max_rows)

    @mcp.tool(name="kedu_get_extras")
    def get_extras(
        info: str,
        security_list: str | list[str],
        start_date: str | None = None,
        end_date: str | None = None,
        count: int | None = None,
        max_rows: int | None = None,
    ) -> str:
        """获取额外数据, 复刻 jqdatasdk.get_extras。

        info 目前支持 'is_st'(是否 ST)与基金净值 'unit_net_value'/'acc_net_value'/
        'adj_net_value'。返回 DataFrame: index 为交易日, 列为代码。
        count 与 start_date 二选一。
        """
        df = kedu.get_extras(
            info,
            security_list,
            start_date=start_date,
            end_date=end_date,
            count=count,
            df=True,
        )
        return render(df, max_rows)

    # ------------------------------------------------------------------
    # 第二层: 完整只读 ClickHouse SELECT
    # ------------------------------------------------------------------

    @mcp.tool(
        name="kedu_query",
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    def query(
        sql: str,
        parameters: dict[str, Any] | None = None,
        max_rows: int | None = None,
    ) -> str:
        """执行一条 jqdata 只读 ClickHouse 查询；用于 JOIN、窗口和自定义聚合。

        何时使用:
        - 跨表 JOIN、窗口函数、复杂子查询、自定义聚合或查询原始关系字段时使用。
        - 单表行情、基本面、估值、成分等标准请求应改用对应 kedu_get_*，以保留聚宽语义。

        调用步骤:
        1. 先用 kedu_describe(table='*') 找表，再用精确表名核对字段、引擎和 semantics。
        2. 明确结果粒度、JOIN 键、日期边界和排序后编写 SQL；大表探索使用聚合或 LIMIT。
        3. 用户值使用 typed placeholder。例如 SQL 写
           ``day >= {start:Date} AND code IN {codes:Array(String)}``，parameters 传
           ``{'start': '2024-01-01', 'codes': ['000001.XSHE']}``。
        4. 检查返回头中的 types、truncated 和 WARNING。truncated=true 时结果不完整，
           应收窄查询或聚合后重试。

        支持范围:
        支持 CTE、子查询、UNION/INTERSECT/EXCEPT、全部 JOIN、ARRAY JOIN、窗口函数、
        QUALIFY、ROLLUP/CUBE/GROUPING SETS、PREWHERE、FINAL 与 EXPLAIN。ClickHouse
        处理标量和聚合函数；表函数只允许 numbers/values。

        不自动处理的语义:
        SQL 不会自动添加复权、FINAL、report_type、财报可见日期或成员有效区间条件。
        返回的 WARNING 只提示风险，不会修改查询。

        返回与失败:
        返回元数据头和 CSV；\\N 表示 NULL，types 是 ClickHouse 原生类型，query_id 可用于
        定位执行。服务端会拒绝非只读语句、跨库引用、system 表、SETTINGS、导出和危险
        表函数；遇到拒绝时应改写为合规 SELECT，不要尝试绕过。
        """
        return _query.run(sql, parameters=parameters, max_rows=max_rows)

    # ------------------------------------------------------------------
    # 第三/四层: discovery + 反射 dispatcher
    # ------------------------------------------------------------------

    @mcp.tool(name="kedu_describe")
    def describe(
        api: str | None = None, table: str | None = None, dsl: bool = False
    ) -> str:
        """查 kedu 的 API 目录、函数签名、jqdata/finance 表结构、DSL 可用名字。

        不带参数: 列出全部可用 API 及一句话说明。
        api='get_call_auction': 返回该 API 的签名与完整 docstring。
        table='*': 列 jqdata SQL 关系；table='bar_1d': 返回物理字段与查询语义；
        table='STK_INCOME_STATEMENT': 返回 finance 逻辑表的字段与类型。
        dsl=True: 列出查询表达式里可用的全部名字。
        """
        parts: list[str] = []
        if dsl:
            parts.append(
                "DSL 可用名字(用于 kedu_get_fundamentals / kedu_finance_run_query "
                "的 query_expr):\n  " + ", ".join(_dsl.available_names())
            )
        if table is not None:
            key = table.upper().replace("FINANCE.", "")
            if key in RUN_QUERY_TABLES:
                parts.append(
                    f"finance.{key} 字段:\n" + render(finance.get_table_info(key), 500)
                )
            else:
                parts.append(_query.describe_table(table))
        if api is not None:
            fn = _dispatchable().get(api) or getattr(kedu, api, None)
            if fn is None or not callable(fn):
                parts.append(f"未知 API {api!r}(用不带参数的 kedu_describe() 看目录)")
            else:
                parts.append(
                    f"{api}{inspect.signature(fn)}\n\n{inspect.getdoc(fn) or '(无 docstring)'}"
                )
                if api in _DSL_TOOL_REDIRECT:
                    parts.append(
                        f"→ 该 API 参数是查询对象, 请用专用 tool {_DSL_TOOL_REDIRECT[api]}。"
                    )
        if not parts:
            lines = [
                "kedu API 目录(高层 tool 已直接暴露的不再重复; 其余用 kedu_call 调用):"
            ]
            for name, fn in sorted(_dispatchable().items()):
                lines.append(f"  {name}{inspect.signature(fn)}\n      {_summary(fn)}")
            lines.append("\n查询对象类 API(用专用 tool, 不能走 kedu_call):")
            for name, tool in sorted(_DSL_TOOL_REDIRECT.items()):
                lines.append(f"  {name} → {tool}")
            lines.append(
                f"\nfinance 表({len(RUN_QUERY_TABLES)} 张, 用 kedu_describe(table=...) 看字段):\n  "
                + ", ".join(sorted(RUN_QUERY_TABLES))
            )
            lines.append(
                "\nClickHouse SQL: 用 kedu_query 执行复杂只读查询；"
                "用 kedu_describe(table='*') 查看 jqdata 表目录。"
            )
            rows, cols = resolve_limits()
            lines.append(
                f"\n出站上限: 默认 {rows} 行 / {cols} 列, 单次调用可用 max_rows 抬高"
                f"(硬上限 {HARD_MAX_ROWS} 行)。"
            )
            parts.append("\n".join(lines))
        return "\n\n".join(parts)

    @mcp.tool(name="kedu_call")
    def call(
        api: str,
        params: dict[str, Any] | None = None,
        max_rows: int | None = None,
        max_cols: int | None = None,
    ) -> str:
        """调用任意 kedu 长尾 API(高频的已有专用 tool, 优先用那些)。

        api 为函数名, params 为关键字参数字典, 例如:
          api='get_call_auction', params={'security': '000001.XSHE',
                                          'start_date': '2024-01-02',
                                          'end_date': '2024-01-05'}
        可用 API 与签名用 kedu_describe() 查。走的仍是 kedu.* 本身,
        复权/成员资格等语义与专用 tool 完全一致。
        """
        return render(invoke_kedu(api, params), max_rows, max_cols)

    # ------------------------------------------------------------------
    # 可视化: MCP Apps —— 交互式图表
    #
    # 渲染器 HTML 与 kedu_plot 共用一个 ui:// 资源, URI 带内容哈希(见 _app)。
    # 数据分两路: content 给模型看一行摘要, structuredContent 给 iframe 全量 option。
    # 客户端没声明 MCP Apps 扩展时不返回 envelope —— 那只会把几百行 JSON 倒进模型
    # 上下文, 还不如现有的 CSV。
    # ------------------------------------------------------------------

    @mcp.resource(
        _app.chart_uri(),
        name="kedu-chart-renderer",
        description="kedu 图表渲染器(内联 ECharts, 供 kedu_plot 使用)",
        mime_type=_app.UI_MIME_TYPE,
        meta={"ui": {"prefersBorder": False}},
    )
    def chart_renderer() -> str:
        return _app.chart_html()

    @mcp.tool(
        name="kedu_plot",
        meta={"ui": {"resourceUri": _app.chart_uri(), "visibility": ["model"]}},
    )
    def plot(
        chart: Literal["kline"],
        source: _plot.KlineSource,
        display: _plot.PlotDisplay | None = None,
    ) -> CallToolResult:
        """把 kedu 的数据画成可交互图表(需客户端支持 MCP Apps, 否则退回 CSV 文本)。

        chart 目前只有 'kline'(K 线 + 成交量, 带缩放)。source 的字段语义与
        kedu.get_price 完全一致 —— fq='pre' 是动态前复权, 锚定该票最后一根 bar,
        图的副标题会把锚定日标出来。

        数据由服务端自己取, 不要也不必把行数据传进来。
        """
        ctx = mcp.get_context()
        df = kedu.get_price(
            source.security,
            start_date=source.start_date,
            end_date=source.end_date,
            frequency=source.frequency,
            fq=source.fq,
            count=source.count,
        )
        if not _app.client_supports_ui(ctx):
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=(
                            "当前客户端未声明 MCP Apps 扩展, 无法渲染交互图表, "
                            "以下为同一份数据的文本形式:\n\n" + render(df)
                        ),
                    )
                ]
            )
        anchor = _fq_anchor(source) if source.fq == "pre" else None
        envelope = _plot.compile_recipe(
            chart, source, display, _plot.normalize_plot_data(df), anchor
        )
        return CallToolResult(
            content=[TextContent(type="text", text=_plot.envelope_summary(envelope))],
            structuredContent=envelope,
        )

    return mcp


def _fq_anchor(source: _plot.KlineSource) -> dict[str, Any] | None:
    """取前复权锚。锚必须来自复权语义层, 不能拿查询窗口的最后一行冒充."""
    df = kedu.get_fq_anchor(source.security, frequency=source.frequency)
    if df.empty:
        return None
    row = df.iloc[0]
    return {
        "time": _plot._json_safe(row["anchor_time"]),
        "factor": float(row["anchor_factor"]),
    }


def main(argv: list[str] | None = None) -> None:
    """命令行入口: kedu-mcp, 支持 stdio 与 streamable-http 两种 transport."""
    p = argparse.ArgumentParser(
        prog="kedu-mcp",
        description="kedu MCP server(本地聚宽复刻数据)。"
        "ClickHouse 凭证从环境变量读取, 由 MCP 配置的 env 块注入。",
    )
    p.add_argument(
        "--transport",
        choices=("stdio", "http", "sse"),
        default="stdio",
        help="stdio(默认, 单机自用) / http(streamable-http, 多客户端共享) / sse(兼容旧客户端)",
    )
    p.add_argument("--host", default="127.0.0.1", help="http/sse 监听地址")
    p.add_argument("--port", type=int, default=8000, help="http/sse 监听端口")
    p.add_argument("--path", default="/mcp", help="streamable-http 挂载路径")
    p.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="默认出站行上限(同 KEDU_MCP_MAX_ROWS)",
    )
    p.add_argument(
        "--max-cols",
        type=int,
        default=None,
        help="默认出站列上限(同 KEDU_MCP_MAX_COLS)",
    )
    args = p.parse_args(argv)

    if args.max_rows:
        os.environ["KEDU_MCP_MAX_ROWS"] = str(args.max_rows)
    if args.max_cols:
        os.environ["KEDU_MCP_MAX_COLS"] = str(args.max_cols)

    # kedu.auth() 会 print 一行到 stdout —— 在 stdio transport 下 stdout 是 JSON-RPC
    # 通道, 那一行会直接把握手报文打脏。重定向到 stderr。
    try:
        with contextlib.redirect_stdout(sys.stderr):
            auth_from_env()
    except RuntimeError as e:
        # 缺凭证时 fail-fast(不静默回退默认值), 但要把话说到 MCP 用户的语境里:
        # 这里通常是 MCP 配置的 env 块没填, 而不是忘了 source .env。
        raise SystemExit(
            f"{e}\n"
            "作为 MCP server 启动时, 凭证应写在 MCP 配置的 env 块里, 例如:\n"
            '  "env": {"CLICKHOUSE_USER": "...", "CLICKHOUSE_PASSWORD": "...",\n'
            '          "CLICKHOUSE_HOST": "127.0.0.1", "CLICKHOUSE_PORT": "8123",\n'
            '          "CLICKHOUSE_DATABASE": "jqdata"}'
        ) from None

    mcp = build_server(host=args.host, port=args.port, streamable_http_path=args.path)
    mcp.run(transport="streamable-http" if args.transport == "http" else args.transport)


if __name__ == "__main__":
    main()
