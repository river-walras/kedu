"""MCP Apps: ui:// 资源的装配、URI 计算、扩展声明与 host 能力探测.

vendored 资产的许可与来源见 static/VENDOR.md, 这里讲四件事。

**URI 带内容哈希** (ui://kedu/chart/echarts-<ver>.<hash>.html)。规范只说 host *可以*
缓存 UI 资源 —— 实测 MCPJam 1.5.17 每次工具调用都重新 resources/read, 根本不缓存。
所以哈希的作用不是省流量, 是保证 HTML 一改 URI 就变: 会缓存的 host 上不会出现旧渲染器
碰上新 envelope(表现是空白图, 而不是报错)。

**CSP 补丁**。MCP Apps 强制的 CSP 是 `script-src 'self' 'unsafe-inline' <resourceDomains>`,
没有 unsafe-eval 且服务端无法申请。两个 bundle 各有一处 `new Function`, 装配时改掉 ——
细节见 _PATCHES。不改掉的话 ext-apps 那处每次加载都会产生一条真实 CSP 违规报告
(被 try/catch 吞掉, 图照常显示, 但违规是真的)。

**扩展声明**。规范要求双方在 initialize 时都声明才算协商成功。官方 SDK 1.28 的
ServerCapabilities 还没有 extensions 字段, 靠 extra='allow' 注入。

**能力探测**。客户端声明落在 ClientCapabilities 的 model_extra 里。探不到默认按不支持
处理并退回文本 —— 但部分 host(如 MCPJam 1.5.17)不声明却照样渲染 App, 那种情况下
需要 KEDU_MCP_FORCE_UI 手动压过, 见 client_supports_ui。
"""

from __future__ import annotations

import functools
import hashlib
import os
import re
from pathlib import Path
from typing import Any

UI_EXTENSION_ID = "io.modelcontextprotocol/ui"
UI_MIME_TYPE = "text/html;profile=mcp-app"

_STATIC = Path(__file__).parent / "static"
_ECHARTS_VERSION = "6.1.0"

_ECHARTS_MARK = "/*__KEDU_ECHARTS__*/"
_EXTAPPS_MARK = "/*__KEDU_EXTAPPS__*/"

# 装配期改写 vendored bundle 里会触发 CSP 违规的 new Function。改写而不是直接编辑
# static/ 下的文件: 那两个文件保持与上游字节一致, 来源可校验(见 VENDOR.md)。
#
# 每条补丁都断言「恰好出现一次」—— 换版本后片段一变就 fail-fast, 而不是静默不打补丁
# 然后在浏览器里冒出一条谁也不会去看的 CSP 报告。
_PATCHES: tuple[tuple[str, str, str, str], ...] = (
    (
        "ext-apps.js",
        'try{return new Function(""),!0}catch(r){return!1}',
        "return!1",
        # Zod 的 JIT 可用性探针。它会真的执行, 在严格 CSP 下抛 EvalError 并留下一条
        # 违规报告, 然后走非 JIT 路径。直接返回 false 结果完全一样, 少一条报告。
        "Zod JIT 探针 —— CSP 下必然为假, 直接短路",
    ),
    (
        "echarts.min.js",
        'new Function("return ("+i+");")()',
        "JSON.parse(i)",
        # GeoJSON 解析在 `"undefined"!=typeof JSON&&JSON.parse` 为假时的远古降级分支,
        # 现代浏览器不可达。换成 JSON.parse 后语义不变, 且装配产物里 new Function 归零,
        # 「零 new Function」比「计数没变」是强得多、也可证得多的不变量。
        "GeoJSON 死分支 —— 等价替换为 JSON.parse",
    ),
)

# ext-apps 是 ESM bundle, 末尾一句 `export{...,eI as App}`。内联进 <script type="module">
# 之后这些导出没人能 import。把应用类与 host 主题 helper 改写成模块内 const 绑定 ——
# 前提是它们确实还在导出列表里, 否则换版本后要 fail-fast, 不能等浏览器报 undefined。
_EXPORT_TAIL = re.compile(r"export\s*\{([^{}]*)\}\s*;?\s*$")
_INLINE_EXPORTS = ("App", "applyDocumentTheme", "applyHostStyleVariables")


def _read(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


def _apply_patches(js: str, name: str) -> str:
    for target, needle, replacement, reason in _PATCHES:
        if target != name:
            continue
        found = js.count(needle)
        if found != 1:
            raise RuntimeError(
                f"static/{name} 里 CSP 补丁「{reason}」的目标片段出现 {found} 次(期望 1)。"
                f"vendored bundle 换版本了, 请重新定位片段并更新 _app._PATCHES。"
            )
        js = js.replace(needle, replacement)
    return js


def _extapps_module() -> str:
    """把需要的 ext-apps ESM 导出改写成模块内 const 绑定."""
    src = _read("ext-apps.js").rstrip()
    tail = _EXPORT_TAIL.search(src)
    if tail is None:
        raise RuntimeError(
            "static/ext-apps.js 尾部没有找到 `export{...}` —— vendored bundle 的形状变了, "
            "请检查版本并更新 _app._EXPORT_TAIL。"
        )
    bindings = []
    exports = tail.group(1)
    for name in _INLINE_EXPORTS:
        alias = re.search(rf"([A-Za-z0-9_$]+)\s+as\s+{re.escape(name)}\b", exports)
        if alias is None:
            raise RuntimeError(
                f"static/ext-apps.js 的导出列表里没有 `X as {name}` —— vendored bundle "
                "的形状变了, 请检查版本并更新 _app._INLINE_EXPORTS。"
            )
        bindings.append(f"const {name} = {alias.group(1)};")
    src = src[: tail.start()] + "\n" + "\n".join(bindings) + "\n"
    return _apply_patches(src, "ext-apps.js")


@functools.lru_cache(maxsize=1)
def chart_html() -> str:
    """装配完整的渲染器 HTML(内联 ECharts + ext-apps + 应用代码)."""
    html = _read("chart.html")
    for mark in (_ECHARTS_MARK, _EXTAPPS_MARK):
        if mark not in html:
            raise RuntimeError(f"static/chart.html 缺少注入标记 {mark}")
    echarts_js = _apply_patches(_read("echarts.min.js"), "echarts.min.js")
    extapps_js = _extapps_module()
    # 两个 bundle 都不含 `</script`(见 VENDOR.md), 可以直接内联; 这里再兜一道,
    # 换版本后若引入了该串会立刻炸, 而不是产出一个被提前截断的 HTML。
    for name, js in (("echarts.min.js", echarts_js), ("ext-apps.js", extapps_js)):
        if "</script" in js.lower():
            raise RuntimeError(f"static/{name} 含 `</script`, 内联会截断文档, 需改用其他注入方式。")
    return html.replace(_ECHARTS_MARK, echarts_js).replace(_EXTAPPS_MARK, extapps_js)


@functools.lru_cache(maxsize=1)
def chart_uri() -> str:
    """ui:// 资源 URI, 带 ECharts 版本与 HTML 内容哈希."""
    digest = hashlib.sha256(chart_html().encode("utf-8")).hexdigest()[:12]
    return f"ui://kedu/chart/echarts-{_ECHARTS_VERSION}.{digest}.html"


def ui_extension_capability() -> dict[str, Any]:
    """服务端要声明的 capabilities.extensions 片段."""
    return {UI_EXTENSION_ID: {"mimeTypes": [UI_MIME_TYPE]}}


def declare_ui_extension(mcp: Any) -> None:
    """把 MCP Apps 扩展写进 initialize 响应的 capabilities.extensions.

    规范要求双方显式声明才算协商成功, 只在 tool 上挂 _meta.ui 是不够的 —— 严格的 host
    会因为服务端没声明而根本不去取 ui:// 资源。

    官方 SDK 1.28 的 ServerCapabilities 没有 extensions 字段, 但 model_config 是
    extra='allow', 注入后能正确序列化。等 SDK 补上类型化字段, 这段就该删掉。
    """
    low = mcp._mcp_server
    original = low.create_initialization_options

    @functools.wraps(original)
    def with_ui_extension(*args: Any, **kwargs: Any) -> Any:
        opts = original(*args, **kwargs)
        caps = opts.capabilities.model_copy(
            update={"extensions": ui_extension_capability()}
        )
        return opts.model_copy(update={"capabilities": caps})

    low.create_initialization_options = with_ui_extension


def client_supports_ui(ctx: Any) -> bool:
    """客户端是否在 initialize 里声明了 MCP Apps 扩展.

    探不到一律当不支持 —— 宁可多退化几次到文本, 也不要把 envelope 塞给一个不会渲染它的
    host(那等于把几百行数据直接倒进模型上下文)。

    KEDU_MCP_FORCE_UI=1 可以压过探测。这是给「不声明扩展却照样渲染 App」的 host 用的
    逃生门(实测 MCPJam 1.5.17 就是这样), 默认关闭 —— 它放宽的是安全默认值, 只应该在
    明确知道对端会渲染时打开。
    """
    if os.getenv("KEDU_MCP_FORCE_UI", "").strip().lower() in ("1", "true", "yes"):
        return True
    try:
        # Context.session 在没有 request context 时会抛 ValueError, 不是返回 None,
        # 所以 getattr 的默认值救不了; 这里显式吞掉 —— 探不到就是不支持。
        caps = ctx.session.client_params.capabilities
        extensions = (caps.model_extra or {}).get("extensions")
    except (AttributeError, ValueError):
        return False
    return isinstance(extensions, dict) and UI_EXTENSION_ID in extensions
