"""kedu API 的调用层: 反射注册表 + 一次调用, 只回原始返回值, 不做任何渲染.

抽出来是因为出口不止一个: kedu_call 要把结果渲染成截断过的文本给模型看, 绘图要把结果
规范化成表格喂给图表。两条路必须共用同一份「什么 API 可调、参数怎么绑」的判定, 否则会
出现一边能调另一边不能调的错位。渲染与规范化各自在 _render / _plot 里做。

注意「可调用」不等于「可绘图」: 这里放行的是 kedu.__all__ 里的全部函数, 能不能画取决于
返回值能否规范化成表格、且带齐模板要的列 —— 那道闸在 _plot.normalize_plot_data。
DSL 三兄弟(参数是查询对象)两条路都进不来, 需要各自的专用 tool。
"""

from __future__ import annotations

import inspect
from typing import Any

import kedu

# 这些名字不进 dispatcher: 查询表面是 SQLAlchemy 对象而非可 JSON 化的函数;
# 认证类不该由模型调用; DSL 三兄弟有各自的专用 tool(参数是查询对象, 反射传不进去)。
DSL_SURFACE = {"query", "income", "balance", "cash_flow", "indicator", "valuation"}
NOT_DISPATCHABLE = DSL_SURFACE | {
    "auth",
    "auth_from_env",
    "get_client",
    "DATABASE",
    "finance",
}
DSL_TOOL_REDIRECT = {
    "get_fundamentals": "kedu_get_fundamentals",
    "get_fundamentals_continuously": "kedu_get_fundamentals_continuously",
    "get_history_fundamentals": "kedu_get_history_fundamentals",
}


def dispatchable() -> dict[str, Any]:
    """kedu.__all__ 中可由反射调用的函数集合."""
    out: dict[str, Any] = {}
    for name in kedu.__all__:
        if name in NOT_DISPATCHABLE or name in DSL_TOOL_REDIRECT:
            continue
        obj = getattr(kedu, name, None)
        if callable(obj):
            out[name] = obj
    return out


def summary(fn: Any) -> str:
    """取函数 docstring 的首行作为一句话摘要."""
    doc = inspect.getdoc(fn) or ""
    return doc.splitlines()[0] if doc else "(无 docstring)"


def invoke_kedu(api: str, params: dict[str, Any] | None = None) -> Any:
    """按名字调一个 kedu API, 返回其原始返回值(DataFrame / list / dict / 标量).

    只负责调到: 不渲染、不截断、不规范化。参数不匹配时把签名一起报出来, 让调用方
    一次就能改对, 而不是靠试。
    """
    if api in DSL_TOOL_REDIRECT:
        raise ValueError(
            f"{api} 的参数是聚宽查询对象, 无法用 JSON 传递; "
            f"请改用 tool {DSL_TOOL_REDIRECT[api]}。"
        )
    registry = dispatchable()
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
    return fn(*bound.args, **bound.kwargs)
