"""kedu MCP server —— 分层混合设计.

分层的理由: kedu 的价值全在语义层(复权动态锚、成员资格、report_type 多版本、
行业 walk 折叠), 不在数据本身。任何绕开 `kedu.*` 直接打 ClickHouse 的设计都会
静默丢掉这些语义 —— 返回的数字看着正常, 实际是错的。所以这个 server 的每条
出口最终都落在 kedu 的公开函数上, 不提供裸 SQL 通道。

三层:
1. 高频 API 直出 tool(12 个) —— 完整 JSON Schema, 模型靠 name/description 就能选中。
2. `kedu_call` 反射 dispatcher —— 兜住 __all__ 里剩下的长尾 API, 走的仍是 kedu.*,
   语义不丢; 新增 kedu API 时这里零改动。
3. `kedu_describe` —— 列 API 目录、查函数签名、查 finance 表结构、列 DSL 可用名字。

出站一律经 _render.render(), 行/列超限即截断并给出醒目提示, 见 _render 模块 docstring。
"""

from __future__ import annotations

import argparse
import contextlib
import inspect
import os
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

import kedu

from ..db import auth_from_env
from ..finance import finance
from ..finance_schema import RUN_QUERY_TABLES
from . import _dsl
from ._render import HARD_MAX_ROWS, render, resolve_limits

# 这些名字不进 dispatcher: 查询表面是 SQLAlchemy 对象而非可 JSON 化的函数;
# 认证类不该由模型调用; DSL 三兄弟有各自的专用 tool(参数是查询对象, 反射传不进去)。
_DSL_SURFACE = {"query", "income", "balance", "cash_flow", "indicator", "valuation"}
_NOT_DISPATCHABLE = _DSL_SURFACE | {
    "auth",
    "auth_from_env",
    "get_client",
    "DATABASE",
    "finance",
}
_DSL_TOOL_REDIRECT = {
    "get_fundamentals": "kedu_get_fundamentals",
    "get_fundamentals_continuously": "kedu_get_fundamentals_continuously",
    "get_history_fundamentals": "kedu_get_history_fundamentals",
}

INSTRUCTIONS = """\
本地 ClickHouse 上的聚宽 jqdatasdk 复刻(库名 jqdata, 72 张表约 75 亿行)。
所有 tool 的返回语义与 jqdatasdk 逐字段对齐, 可直接当聚宽数据用。

重要:
- 不要用通用 ClickHouse/SQL 工具去查 jqdata 库。裸 SQL 会绕开复权因子、前复权动态锚、
  成员资格语义、report_type 多版本去重、行业区间折叠等处理, 拿到的数字看着正常但是错的。
  一切查询走本 server 的 tool。
- get_price 默认 fq='pre'(前复权), 且前复权锚定全表最新交易日, 是动态值。
  要不复权传 fq=None, 后复权传 fq='post'。
- 基本面/finance 查询用表达式字符串写聚宽 DSL, 例如
  query(valuation).filter(valuation.pe_ratio < 10).limit(50)
  可用名字见 kedu_describe(dsl=True)。
- 结果有行数上限, 超出会截断并在开头标注; 需要更多行时调 max_rows, 或收窄查询条件。
- 这里只列了高频 API。完整 API 目录用 kedu_describe(), 长尾 API 用 kedu_call() 调用。
"""


def _dispatchable() -> dict[str, Any]:
    """kedu.__all__ 中可由 kedu_call 反射调用的函数集合."""
    out: dict[str, Any] = {}
    for name in kedu.__all__:
        if name in _NOT_DISPATCHABLE or name in _DSL_TOOL_REDIRECT:
            continue
        obj = getattr(kedu, name, None)
        if callable(obj):
            out[name] = obj
    return out


def _summary(fn: Any) -> str:
    """取函数 docstring 的首行作为一句话摘要."""
    doc = inspect.getdoc(fn) or ""
    return doc.splitlines()[0] if doc else "(无 docstring)"


def build_server(**settings: Any) -> FastMCP:
    """装配并返回 FastMCP 实例. settings 透传 host/port/streamable_http_path 等."""
    mcp = FastMCP("kedu", instructions=INSTRUCTIONS, **settings)

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
    # 第二/三层: discovery + 反射 dispatcher
    # ------------------------------------------------------------------

    @mcp.tool(name="kedu_describe")
    def describe(
        api: str | None = None, table: str | None = None, dsl: bool = False
    ) -> str:
        """查 kedu 的 API 目录、函数签名、finance 表结构、DSL 可用名字。

        不带参数: 列出全部可用 API 及一句话说明。
        api='get_call_auction': 返回该 API 的签名与完整 docstring。
        table='STK_INCOME_STATEMENT': 返回该 finance 表的字段与类型(不传表名则列全部表)。
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
            if key not in RUN_QUERY_TABLES:
                parts.append(
                    f"未知 finance 表 {table!r}。可用表:\n  "
                    + ", ".join(sorted(RUN_QUERY_TABLES))
                )
            else:
                parts.append(
                    f"finance.{key} 字段:\n" + render(finance.get_table_info(key), 500)
                )
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
        if api in _DSL_TOOL_REDIRECT:
            raise ValueError(
                f"{api} 的参数是聚宽查询对象, 无法用 JSON 传递; "
                f"请改用 tool {_DSL_TOOL_REDIRECT[api]}。"
            )
        registry = _dispatchable()
        fn = registry.get(api)
        if fn is None:
            raise ValueError(
                f"未知或不可调用的 API {api!r}。可用: {', '.join(sorted(registry))}"
            )
        try:
            bound = inspect.signature(fn).bind(**(params or {}))
        except TypeError as e:
            raise ValueError(
                f"{api} 参数不匹配: {e}\n签名: {api}{inspect.signature(fn)}"
            ) from None
        return render(fn(*bound.args, **bound.kwargs), max_rows, max_cols)

    return mcp


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
