"""绘图层的纯静态校验: 规范化闸门、K 线财务正确性、envelope 契约、渲染器契约。

和 test_mcp_server.py 一样不碰 ClickHouse 也不碰聚宽 —— get_price / get_fq_anchor 一律
monkeypatch,无凭证环境也能跑。这里管的是「画出来的东西对不对」,不是「数据对不对」。

财务正确性的几条(candlestick 维度顺序、涨跌配色、复权锚来源、标题口径)必须在这层锁死:
它们错了不会抛异常,只会画出一张看着正常但是错的图,比崩溃难发现得多。
"""

from __future__ import annotations

import asyncio
import re
import types

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("mcp", reason="MCP server 需要 `uv sync --extra mcp`")

import mcp.types as mtypes  # noqa: E402

import kedu  # noqa: E402
from kedu.mcp_server import _app, _plot  # noqa: E402
from kedu.mcp_server import build_server  # noqa: E402


# ---------------------------------------------------------------------------
# 造数
# ---------------------------------------------------------------------------
def _price_df(n: int = 3, *, volume: bool = True, index_name: str = "date") -> pd.DataFrame:
    """模仿 get_price 单代码窄表: index 为时间, 列为 fields。

    OHLC 取互不相同的量级, 顺序错位时断言必然失败。
    """
    idx = pd.DatetimeIndex(
        [pd.Timestamp("2024-01-0%d" % (i + 2)) for i in range(n)], name=index_name
    )
    cols = {
        "open": [10.0 + i for i in range(n)],
        "close": [20.0 + i for i in range(n)],
        "low": [1.0 + i for i in range(n)],
        "high": [30.0 + i for i in range(n)],
    }
    if volume:
        cols["volume"] = [1000.0 * (i + 1) for i in range(n)]
    return pd.DataFrame(cols, index=idx)


def _source(**kw) -> _plot.KlineSource:
    base = {"security": "000001.XSHE", "start_date": "2024-01-01", "end_date": "2024-01-31"}
    return _plot.KlineSource(**{**base, **kw})


def _compile(df=None, source=None, display=None, anchor=None):
    frame = _plot.normalize_plot_data(_price_df() if df is None else df)
    return _plot.compile_recipe("kline", source or _source(), display, frame, anchor)


_ANCHOR = {"time": "2026-07-27", "factor": 1.234567}


# ---------------------------------------------------------------------------
# 财务正确性:错了不会抛,只会画错
# ---------------------------------------------------------------------------
def test_candlestick_dimension_order_is_open_close_low_high():
    """ECharts candlestick 的 defaultValueDimensions 是 [open, close, lowest, highest],

    不是 OHLC。顺序写反不会报错, 只会把实体和影线画错。
    """
    series = _compile()["option"]["series"][0]
    assert series["type"] == "candlestick"
    assert series["data"][0] == [10.0, 20.0, 1.0, 30.0]  # open, close, low, high
    assert series["data"][2] == [12.0, 22.0, 3.0, 32.0]


def test_up_down_colors_default_to_a_share_convention():
    """红涨绿跌是 A 股约定, 不能取决于 vendored 的 ECharts 版本默认值。"""
    style = _compile()["option"]["series"][0]["itemStyle"]
    assert style["color"] == "#eb5454" and style["borderColor"] == "#eb5454"
    assert style["color0"] == "#47b262" and style["borderColor0"] == "#47b262"


def test_price_axis_does_not_start_at_zero():
    """K 线纵轴从 0 起会把波动压平, 看上去像一条直线。"""
    assert _compile()["option"]["yAxis"][0]["scale"] is True


def test_subtitle_carries_fq_anchor_and_both_ranges():
    """副标题要同时带请求区间、实际数据区间与复权口径 —— 图会被截图存档。"""
    sub = _compile(anchor=_ANCHOR)["subtitle"]
    assert "请求 2024-01-01 ~ 2024-01-31" in sub
    assert "数据 2024-01-02 ~ 2024-01-04(3 根)" in sub
    assert "前复权" in sub and "锚 2026-07-27" in sub and "1.23457" in sub


def test_subtitle_marks_anchor_unknown_rather_than_guessing():
    """取不到锚就说取不到, 绝不拿窗口最后一天冒充。"""
    sub = _compile(anchor=None)["subtitle"]
    assert "锚未知" in sub
    assert "2024-01-04" not in sub.split("前复权")[-1]


@pytest.mark.parametrize(
    "fq,expected", [("post", "后复权"), ("none", "不复权")]
)
def test_subtitle_states_non_pre_adjustment(fq, expected):
    sub = _compile(source=_source(fq=fq))["subtitle"]
    assert expected in sub
    assert "锚" not in sub


def test_title_states_security_and_frequency():
    assert _compile()["title"] == "000001.XSHE  日线 K 线"
    minute = _compile(source=_source(frequency="1m"))
    assert minute["title"] == "000001.XSHE  1 分钟 K 线"


def test_count_request_is_described_as_count_not_a_fake_range():
    sub = _compile(
        source=_plot.KlineSource(security="000001.XSHE", count=120)
    )["subtitle"]
    assert "请求 最近 120 根" in sub


# ---------------------------------------------------------------------------
# 规范化闸门:可调用 != 可绘图
# ---------------------------------------------------------------------------
def test_normalize_promotes_named_index_to_column():
    """get_price 单代码返回窄表, 时间在 index 上 —— 不提成列就没有横轴。"""
    frame = _plot.normalize_plot_data(_price_df())
    assert frame.columns[0] == "date"
    assert frame.rows[0][0] == "2024-01-02"


def test_normalize_keeps_minute_resolution():
    df = _price_df(index_name="datetime")
    df.index = pd.DatetimeIndex(
        ["2024-01-02 09:31", "2024-01-02 09:32", "2024-01-02 09:33"], name="datetime"
    )
    frame = _plot.normalize_plot_data(df)
    assert frame.rows[0][0] == "2024-01-02 09:31"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_normalize_maps_nonfinite_to_null(bad):
    """JSON 没有 NaN/Infinity, 漏出去会让 host 解析失败, 整张图不出来。"""
    df = _price_df()
    df.iloc[1, df.columns.get_loc("close")] = bad
    frame = _plot.normalize_plot_data(df)
    assert frame.column("close")[1] is None


def test_normalize_maps_nat_to_null():
    df = pd.DataFrame({"t": [pd.NaT, pd.Timestamp("2024-01-02")], "v": [1, 2]})
    assert _plot.normalize_plot_data(df).column("t") == [None, "2024-01-02"]


def test_normalize_handles_numpy_scalars():
    df = pd.DataFrame({"a": np.array([1, 2], dtype="int64"),
                       "b": np.array([True, False]),
                       "c": np.array([1.5, 2.5], dtype="float32")})
    rows = _plot.normalize_plot_data(df).rows
    assert rows[0][0] == 1 and isinstance(rows[0][0], int)
    assert rows[0][1] is True and isinstance(rows[0][1], bool)
    assert isinstance(rows[0][2], float)


def test_normalize_rejects_empty_result():
    with pytest.raises(_plot.PlotError, match="结果为空"):
        _plot.normalize_plot_data(pd.DataFrame({"open": []}))


def test_normalize_rejects_non_tabular():
    with pytest.raises(_plot.PlotError, match="表格型返回值"):
        _plot.normalize_plot_data(["000001.XSHE"])
    with pytest.raises(_plot.PlotError, match="表格型返回值"):
        _plot.normalize_plot_data({"000001.XSHE": {"sw_l1": "801780"}})


def test_normalize_rejects_oversized_result():
    """超限报错而不截断 —— 截断后的图看不出缺了什么。"""
    n = _plot.MAX_POINTS + 1
    with pytest.raises(_plot.PlotError, match="超过单图上限"):
        _plot.normalize_plot_data(pd.DataFrame({"open": range(n)}))


def test_kline_rejects_missing_ohlc_columns():
    df = _price_df().drop(columns=["low", "high"])
    with pytest.raises(_plot.PlotError, match="缺少绘图需要的列: low, high"):
        _compile(df)


def test_kline_rejects_duplicate_times():
    df = _price_df()
    df.index = pd.DatetimeIndex(["2024-01-02"] * 3, name="date")
    with pytest.raises(_plot.PlotError, match="重复值"):
        _compile(df)


def test_kline_rejects_unsorted_times():
    df = _price_df()
    df.index = pd.DatetimeIndex(["2024-01-04", "2024-01-02", "2024-01-03"], name="date")
    with pytest.raises(_plot.PlotError, match="非升序"):
        _compile(df)


def test_kline_rejects_all_null_ohlc():
    df = _price_df(n=2)
    for c in ("open", "close", "low", "high"):
        df[c] = np.nan
    with pytest.raises(_plot.PlotError, match="OHLC 全为空"):
        _compile(df)


def test_kline_without_volume_column_still_renders():
    env = _compile(_price_df(volume=False))
    assert [s["type"] for s in env["option"]["series"]] == ["candlestick"]
    assert len(env["option"]["grid"]) == 1


# ---------------------------------------------------------------------------
# display 白名单
# ---------------------------------------------------------------------------
def test_display_forbids_unknown_keys():
    """未知键报错而不是静默忽略 —— 否则调用方以为设置生效了。"""
    with pytest.raises(Exception, match="extra_forbidden|Extra inputs"):
        _plot.PlotDisplay(seires_smoothing=True)


def test_display_rejects_non_hex_color():
    with pytest.raises(_plot.PlotError, match="hex 颜色"):
        _compile(display=_plot.PlotDisplay(up_color="red; drop table"))


def test_display_rejects_inverted_y_range():
    with pytest.raises(_plot.PlotError, match="必须小于"):
        _compile(display=_plot.PlotDisplay(y_min=30, y_max=10))


def test_display_toggles_are_wired():
    off = _compile(display=_plot.PlotDisplay(legend=False, zoom=False, animation=False))
    assert off["option"]["legend"]["show"] is False
    assert off["option"]["animation"] is False
    assert "dataZoom" not in off["option"]

    on = _compile(display=_plot.PlotDisplay(y_min=1, y_max=99))
    assert on["option"]["yAxis"][0]["min"] == 1
    assert [z["type"] for z in on["option"]["dataZoom"]] == ["inside", "slider"]


def test_source_forbids_unknown_keys():
    with pytest.raises(Exception, match="extra_forbidden|Extra inputs"):
        _plot.KlineSource(security="000001.XSHE", fq_type="pre")


# ---------------------------------------------------------------------------
# envelope 契约
# ---------------------------------------------------------------------------
def test_envelope_shape():
    env = _compile(anchor=_ANCHOR)
    assert env["schema_version"] == _plot.SCHEMA_VERSION
    assert env["chart"] == "kline" and env["renderer"] == "echarts"
    assert env["title"] and env["subtitle"]
    assert env["meta"]["rows"] == 3
    assert env["meta"]["fq_anchor"] == _ANCHOR
    assert env["meta"]["source"]["security"] == "000001.XSHE"


def test_title_stays_out_of_the_echarts_option():
    """canvas 里的文字不换行, 窄屏会把副标题裁掉 —— 标题必须由渲染器画进 DOM。"""
    env = _compile(anchor=_ANCHOR)
    assert "title" not in env["option"]


def test_layout_reserves_room_for_the_zoom_slider():
    """不预留就会和缩放条叠在一起; 关掉缩放时那块空间要还回来。"""
    with_zoom = _compile()["option"]
    without = _compile(display=_plot.PlotDisplay(zoom=False))["option"]
    assert with_zoom["grid"][1]["bottom"] > without["grid"][1]["bottom"]
    assert with_zoom["dataZoom"][1]["bottom"] == _plot._SLIDER_BOTTOM


def test_envelope_carries_no_functions_only_json():
    """option 里出现函数就没法走 JSON, 且 CSP 下也执行不了。"""
    import json

    json.dumps(_compile(anchor=_ANCHOR))  # 不可序列化会直接抛


def test_summary_is_one_line_without_data_rows():
    """给模型的摘要只讲口径, 不夹带行数据。"""
    env = _compile(anchor=_ANCHOR)
    s = _plot.envelope_summary(env)
    assert "\n" not in s
    assert "000001.XSHE" in s and "锚 2026-07-27" in s
    assert "30.0" not in s  # high 的值不该出现在摘要里


def test_unknown_chart_template_is_rejected():
    frame = _plot.normalize_plot_data(_price_df())
    with pytest.raises(_plot.PlotError, match="未知图表模板"):
        _plot.compile_recipe("sankey", _source(), None, frame, None)


# ---------------------------------------------------------------------------
# 渲染器契约:HTML 与服务端必须对得上
# ---------------------------------------------------------------------------
def _template() -> str:
    """未装配的模板 —— 断言的是我们自己写的那段代码, 不是 vendored bundle 的内容。"""
    return (_app._STATIC / "chart.html").read_text(encoding="utf-8")


def test_renderer_schema_version_matches_server():
    """两边版本不一致 = 缓存住的旧渲染器碰上新 envelope, 表现是空白图。"""
    m = re.search(r"const SUPPORTED_SCHEMA = (\d+);", _template())
    assert m, "chart.html 里找不到 SUPPORTED_SCHEMA"
    assert int(m.group(1)) == _plot.SCHEMA_VERSION


def test_renderer_uses_dynamic_settheme_and_resize():
    html = _template()
    assert "chart.setTheme(theme)" in html
    assert "ResizeObserver" in html and "chart.resize()" in html
    assert "setupSizeChangedNotifications" in html


def test_renderer_preserves_theme_across_partial_host_context_updates():
    """Phone 切换只发 deviceCapabilities 时, 不能因 patch 里无 theme 就退回亮色。"""
    html = _template()
    assert "app.onhostcontextchanged = () => applyHostContext(app.getHostContext())" in html
    assert "ctx.theme !== 'dark' && ctx.theme !== 'light'" in html
    assert "applyDocumentTheme(ctx.theme)" in html


def test_renderer_applies_host_colors_to_dom_header():
    """标题不在 ECharts canvas 内, 必须单独消费 host 的明暗色变量。"""
    html = _template()
    assert "applyHostStyleVariables(ctx.styles.variables)" in html
    assert "--color-text-primary" in html
    assert "--color-text-secondary" in html
    assert 'html[data-theme="dark"]' in html


def test_renderer_reports_errors_via_sendlog_not_ui_message():
    """ui/message 会把内容灌进对话; 渲染失败属于诊断, 只能走 notifications/message。"""
    html = _template()
    assert "app.sendLog(" in html
    assert "sendMessage(" not in html
    assert "updateModelContext(" not in html


def test_renderer_shows_a_visible_error_panel():
    html = _template()
    assert 'id="err"' in html and "errEl.classList.add('on')" in html


def test_renderer_draws_title_into_dom_with_wrapping():
    """窄屏下副标题必须能换行 —— 它承载复权口径, 被裁掉等于丢掉最该看的信息。"""
    html = _template()
    assert 'id="title"' in html and 'id="subtitle"' in html
    assert "titleEl.textContent" in html and "subtitleEl.textContent" in html
    assert "overflow-wrap: anywhere" in html
    assert "@media (max-width: 480px)" in html


def test_assembled_html_contains_no_new_function():
    """MCP Apps 的 CSP 是 script-src 'self' 'unsafe-inline', 没有 unsafe-eval。

    「计数没变」证明不了热路径安全 —— ext-apps 的 Zod JIT 探针确实会执行, 在严格
    CSP 下产生一条真实违规报告(被 try/catch 吞掉, 图照常出, 但违规是真的)。
    装配期把两处都改掉后, 「产物里零个 new Function」才是能证的不变量。
    """
    html = _app.chart_html()
    assert "new Function" not in html
    assert not re.search(r"[^a-zA-Z.]eval\(", html)


def test_csp_patches_fail_loudly_when_upstream_changes():
    """补丁打不上必须炸, 不能静默跳过 —— 否则 CSP 违规会悄悄回来。"""
    for name, needle, _replacement, _reason in _app._PATCHES:
        js = (_app._STATIC / name).read_text(encoding="utf-8")
        assert js.count(needle) == 1, f"{name} 的补丁目标片段不再唯一"
    with pytest.raises(RuntimeError, match="换版本了"):
        _app._apply_patches("无关内容", _app._PATCHES[0][0])


def test_jit_probe_is_short_circuited_not_merely_wrapped():
    """探针改成直接返回 false, 语义与 CSP 下的实际行为一致, 少一条违规报告。"""
    module = _app._extapps_module()
    assert 'try{return new Function(""),!0}catch(r){return!1}' not in module
    assert "return!1});" in module


def test_chart_uri_is_content_addressed():
    uri = _app.chart_uri()
    assert uri.startswith("ui://kedu/chart/echarts-6.1.0.")
    assert uri.endswith(".html")
    # 哈希由 HTML 内容决定 —— 内容不变则 URI 稳定
    assert _app.chart_uri() == uri


def test_extapps_export_rewrite_binds_inline_dependencies():
    """内联进 module 后没人能 import, App 与主题 helper 都必须改写成 const 绑定。"""
    html = _app.chart_html()
    for name in _app._INLINE_EXPORTS:
        assert re.search(rf"const {name} = [A-Za-z0-9_$]+;", html)
    assert not re.search(r"export\s*\{", html)


# ---------------------------------------------------------------------------
# host 能力探测
# ---------------------------------------------------------------------------
def _ctx(capabilities):
    return types.SimpleNamespace(
        session=types.SimpleNamespace(
            client_params=types.SimpleNamespace(capabilities=capabilities)
        )
    )


def test_client_supports_ui_reads_extensions_from_model_extra():
    caps = mtypes.ClientCapabilities.model_validate(
        {"extensions": {_app.UI_EXTENSION_ID: {"mimeTypes": [_app.UI_MIME_TYPE]}}}
    )
    assert _app.client_supports_ui(_ctx(caps)) is True


def test_client_supports_ui_is_false_when_not_declared():
    assert _app.client_supports_ui(_ctx(mtypes.ClientCapabilities())) is False
    assert _app.client_supports_ui(_ctx(None)) is False
    assert _app.client_supports_ui(types.SimpleNamespace()) is False


def test_force_ui_env_overrides_probe(monkeypatch):
    """给「不声明扩展却照样渲染 App」的 host 用的逃生门(实测 MCPJam 1.5.17)。"""
    ctx = _ctx(mtypes.ClientCapabilities())
    assert _app.client_supports_ui(ctx) is False
    monkeypatch.setenv("KEDU_MCP_FORCE_UI", "1")
    assert _app.client_supports_ui(ctx) is True
    monkeypatch.setenv("KEDU_MCP_FORCE_UI", "0")
    assert _app.client_supports_ui(ctx) is False


def test_client_supports_ui_is_false_outside_request_context():
    """Context.session 在没有 request context 时抛 ValueError, 不能让它冒出去。"""

    class NoCtx:
        @property
        def session(self):
            raise ValueError("no request context")

    assert _app.client_supports_ui(NoCtx()) is False


# ---------------------------------------------------------------------------
# 端到端(仍不碰 ClickHouse)
# ---------------------------------------------------------------------------
@pytest.fixture
def stub_kedu(monkeypatch):
    calls = {}

    def fake_get_price(security, **kw):
        calls["get_price"] = {"security": security, **kw}
        return _price_df()

    def fake_get_fq_anchor(security, frequency="daily"):
        calls["get_fq_anchor"] = {"security": security, "frequency": frequency}
        return pd.DataFrame(
            {"code": [security], "anchor_time": [pd.Timestamp("2026-07-27")],
             "anchor_factor": [1.234567]}
        )

    monkeypatch.setattr(kedu, "get_price", fake_get_price)
    monkeypatch.setattr(kedu, "get_fq_anchor", fake_get_fq_anchor)
    return calls


def _call_plot(**kw):
    args = {"chart": "kline", "source": {"security": "000001.XSHE",
                                         "start_date": "2024-01-01",
                                         "end_date": "2024-01-31"}}
    args.update(kw)
    return asyncio.run(build_server().call_tool("kedu_plot", args))


def test_plot_returns_envelope_when_host_supports_ui(stub_kedu, monkeypatch):
    monkeypatch.setattr(_app, "client_supports_ui", lambda ctx: True)
    result = _call_plot()
    assert isinstance(result, mtypes.CallToolResult)
    assert result.structuredContent["chart"] == "kline"
    assert result.structuredContent["schema_version"] == _plot.SCHEMA_VERSION
    # 给模型的只有一行摘要, 行数据全在 structuredContent 里
    assert len(result.content) == 1
    text = result.content[0].text
    assert "已渲染 kline 图" in text and "\n" not in text


def test_plot_anchor_comes_from_semantics_layer(stub_kedu, monkeypatch):
    """锚必须来自 get_fq_anchor, 且 frequency 要跟着传 —— bar_1d 与 bar_1m 的锚不同。"""
    monkeypatch.setattr(_app, "client_supports_ui", lambda ctx: True)
    _call_plot(source={"security": "000001.XSHE", "count": 60, "frequency": "1m"})
    assert stub_kedu["get_fq_anchor"] == {"security": "000001.XSHE", "frequency": "1m"}


def test_plot_skips_anchor_lookup_when_not_pre_adjusted(stub_kedu, monkeypatch):
    monkeypatch.setattr(_app, "client_supports_ui", lambda ctx: True)
    _call_plot(source={"security": "000001.XSHE", "count": 60, "fq": "none"})
    assert "get_fq_anchor" not in stub_kedu


def test_plot_degrades_to_csv_without_ui_capability(stub_kedu, monkeypatch):
    """不支持扩展的 host 拿 envelope 只会看到一坨 JSON, 不如给它 CSV。"""
    monkeypatch.setattr(_app, "client_supports_ui", lambda ctx: False)
    result = _call_plot()
    assert result.structuredContent is None
    text = result.content[0].text
    assert "未声明 MCP Apps 扩展" in text
    assert "kind=DataFrame" in text and "date,open,close,low,high,volume" in text


def test_plot_passes_source_fields_through_to_get_price(stub_kedu, monkeypatch):
    monkeypatch.setattr(_app, "client_supports_ui", lambda ctx: True)
    _call_plot(source={"security": "600000.XSHG", "count": 30,
                       "frequency": "1m", "fq": "post"})
    assert stub_kedu["get_price"] == {
        "security": "600000.XSHG", "start_date": None, "end_date": None,
        "frequency": "1m", "fq": "post", "count": 30,
    }


# ---------------------------------------------------------------------------
# 注册面
# ---------------------------------------------------------------------------
def test_server_declares_the_ui_extension_in_initialize():
    """规范要求双方都声明才算协商成功; 只挂 tool 的 _meta.ui 时严格 host 不会取资源。"""
    opts = build_server()._mcp_server.create_initialization_options()
    caps = opts.capabilities.model_dump(by_alias=True, exclude_none=True)
    assert caps["extensions"] == {
        _app.UI_EXTENSION_ID: {"mimeTypes": [_app.UI_MIME_TYPE]}
    }
    # 原有能力不能被挤掉
    assert "tools" in caps and "resources" in caps


def test_ui_resource_is_registered_with_app_mimetype():
    resources = asyncio.run(build_server().list_resources())
    ui = [r for r in resources if str(r.uri).startswith("ui://")]
    assert len(ui) == 1
    assert ui[0].mimeType == _app.UI_MIME_TYPE
    assert str(ui[0].uri) == _app.chart_uri()


def test_plot_tool_points_at_the_ui_resource():
    tools = asyncio.run(build_server().list_tools())
    plot = next(t for t in tools if t.name == "kedu_plot")
    assert plot.meta == {"ui": {"resourceUri": _app.chart_uri(), "visibility": ["model"]}}


def test_plot_tool_has_no_output_schema():
    """返回 CallToolResult 时不该生成 outputSchema, 否则 structuredContent 会被校验。"""
    tools = asyncio.run(build_server().list_tools())
    plot = next(t for t in tools if t.name == "kedu_plot")
    assert plot.outputSchema is None


def test_get_fq_anchor_query_has_no_window_filter(monkeypatch):
    """锚是全表最后一根 bar —— 子查询一旦带上日期过滤, 锚就会被窗口截短。"""
    seen = {}

    def fake_query_df(cli, sql):
        seen["sql"] = sql
        return pd.DataFrame({"code": [], "anchor_time": [], "anchor_factor": []})

    monkeypatch.setattr("kedu.prices.get_client", lambda: object())
    monkeypatch.setattr("kedu.prices.query_df", fake_query_df)
    kedu.prices.get_fq_anchor("000001.XSHE", frequency="daily")

    sql = seen["sql"]
    assert "argMax(factor, date)" in sql and "bar_1d" in sql
    assert ">=" not in sql and "<=" not in sql
    kedu.prices.get_fq_anchor("000001.XSHE", frequency="1m")
    assert "argMax(factor, datetime)" in seen["sql"] and "bar_1m" in seen["sql"]
