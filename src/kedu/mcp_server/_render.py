"""MCP 出站渲染: 把 kedu 返回值转成紧凑文本, 带行数/列数上限与显式截断提示.

出站策略是「行数上限 + 截断」而非落盘: 超限时返回前 N 行, 并在正文最前面给出醒目的
截断提示与收窄建议。不静默截断(模型会把残缺结果当全量), 也不直接报错(那会让一个
201 行的查询彻底不可用)。上限由 KEDU_MCP_MAX_ROWS / KEDU_MCP_MAX_COLS 覆盖,
单次调用可用 max_rows 参数临时抬高, 但不得越过 HARD_MAX_ROWS。

用 CSV 而非 markdown 表格: 同样信息量下 token 少一半, 且列对齐无歧义。
"""

from __future__ import annotations

import json
import os
from typing import Any

import numpy as np
import pandas as pd

DEFAULT_MAX_ROWS = 200
DEFAULT_MAX_COLS = 40
HARD_MAX_ROWS = 5000
HARD_MAX_COLS = 400


def _env_int(name: str, default: int) -> int:
    """读正整数环境变量, 非法或非正时回落到 default."""
    raw = os.getenv(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def resolve_limits(
    max_rows: int | None = None, max_cols: int | None = None
) -> tuple[int, int]:
    """定出本次渲染的行/列上限: 显式入参 > 环境变量 > 默认值, 再夹到硬上限."""
    rows = (
        max_rows
        if max_rows and max_rows > 0
        else _env_int("KEDU_MCP_MAX_ROWS", DEFAULT_MAX_ROWS)
    )
    cols = (
        max_cols
        if max_cols and max_cols > 0
        else _env_int("KEDU_MCP_MAX_COLS", DEFAULT_MAX_COLS)
    )
    return min(rows, HARD_MAX_ROWS), min(cols, HARD_MAX_COLS)


def _narrow_hint() -> str:
    """给模型的收窄建议, 附在截断提示后面."""
    return (
        f"需要完整结果请收窄查询(缩短 start_date/end_date、减少 security 数量、"
        f"用 fields 限定列、或在 DSL 里加 .limit()), 或调高 max_rows"
        f"(硬上限 {HARD_MAX_ROWS} 行 / {HARD_MAX_COLS} 列)."
    )


def _fmt_json(value: Any) -> str:
    """JSON 序列化, 日期/Decimal/numpy 标量统一退化为字符串."""
    return json.dumps(value, ensure_ascii=False, default=str)


def _render_dataframe(df: pd.DataFrame, max_rows: int, max_cols: int) -> str:
    """DataFrame -> 元信息头 + CSV 正文."""
    n_rows, n_cols = df.shape
    row_cut = n_rows > max_rows
    col_cut = n_cols > max_cols
    view = df.iloc[:max_rows, :max_cols] if (row_cut or col_cut) else df

    # RangeIndex 是 0..n 的噪声, 不输出; 其余(时间索引、代码索引)是数据的一部分, 必须留。
    keep_index = not isinstance(view.index, pd.RangeIndex)
    if keep_index and view.index.name is None:
        # 无名索引在 CSV 里会退化成一个裸逗号开头, 读的人分不清首列是什么;
        # 给个占位名, 真实含义由下面的 index=<类型> 元信息行说明。
        view = view.rename_axis("index")

    head = [
        f"kind=DataFrame rows={n_rows} cols={n_cols} "
        f"shown_rows={len(view)} shown_cols={view.shape[1]}"
    ]
    if keep_index:
        head.append(
            f"index={type(df.index).__name__} name={df.index.name!r} "
            f"(CSV 首列; 原索引无名时占位显示为 'index')"
            if df.index.name is None
            else f"index={type(df.index).__name__} name={df.index.name!r} (CSV 首列)"
        )
    head.append(
        "dtypes: " + ", ".join(f"{c}:{t}" for c, t in view.dtypes.astype(str).items())
    )
    if row_cut:
        head.append(
            f"⚠ 行已截断: 共 {n_rows} 行, 仅返回前 {max_rows} 行。{_narrow_hint()}"
        )
    if col_cut:
        dropped = list(df.columns[max_cols:])
        preview = ", ".join(map(str, dropped[:10])) + (
            " ..." if len(dropped) > 10 else ""
        )
        head.append(
            f"⚠ 列已截断: 共 {n_cols} 列, 仅返回前 {max_cols} 列; "
            f"未返回 {len(dropped)} 列({preview})。"
        )
    body = view.to_csv(index=keep_index).rstrip("\n")
    return "\n".join(head) + "\n" + body


def _render_sequence(values: list, kind: str, max_rows: int) -> str:
    """list / ndarray -> 元信息头 + JSON 数组."""
    total = len(values)
    cut = total > max_rows
    view = values[:max_rows] if cut else values
    head = f"kind={kind} len={total} shown={len(view)}"
    if cut:
        head += f"\n⚠ 已截断: 共 {total} 项, 仅返回前 {max_rows} 项。{_narrow_hint()}"
    return head + "\n" + _fmt_json(view)


def _render_mapping(value: dict, max_rows: int) -> str:
    """dict -> 元信息头 + JSON 对象, 按顶层键数截断."""
    total = len(value)
    cut = total > max_rows
    view = dict(list(value.items())[:max_rows]) if cut else value
    head = f"kind=dict keys={total} shown={len(view)}"
    if cut:
        head += f"\n⚠ 已截断: 共 {total} 个键, 仅返回前 {max_rows} 个。{_narrow_hint()}"
    return head + "\n" + _fmt_json(view)


def render(obj: Any, max_rows: int | None = None, max_cols: int | None = None) -> str:
    """把 kedu 的任意返回值渲染成给模型看的紧凑文本.

    覆盖 kedu 实际会返回的全部形态: DataFrame(绝大多数 API)、Series、
    numpy.ndarray(get_trade_days)、list(get_index_stocks / get_industry_stocks)、
    dict(get_industry / get_extras(df=False))、以及标量。
    """
    rows, cols = resolve_limits(max_rows, max_cols)
    if obj is None:
        return "kind=None\nnull"
    if isinstance(obj, pd.DataFrame):
        return _render_dataframe(obj, rows, cols)
    if isinstance(obj, pd.Series):
        return _render_dataframe(obj.to_frame(name=obj.name or "value"), rows, cols)
    if isinstance(obj, np.ndarray):
        return _render_sequence(obj.tolist(), "ndarray", rows)
    if isinstance(obj, (list, tuple)):
        return _render_sequence(list(obj), type(obj).__name__, rows)
    if isinstance(obj, dict):
        return _render_mapping(obj, rows)
    if isinstance(obj, (str, bool, int, float, np.generic)):
        return f"kind={type(obj).__name__}\n{obj}"
    return f"kind={type(obj).__name__}\n{obj!r}"
