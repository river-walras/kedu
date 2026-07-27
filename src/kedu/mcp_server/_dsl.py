"""把 `query(...)` 表达式字符串求值成 SqlQuery 对象(受限 eval).

聚宽的查询表面是 SQLAlchemy 对象 —— `query(valuation).filter(valuation.pe_ratio < 10)`
无法用 JSON 参数无损表达(or_ / in_ / 跨表 / 函数调用都会丢)。这里选择让模型直接写
表达式字符串, 换取满分表达力, 代价是一次 eval。

约束这次 eval 的三道闸:
1. `mode="eval"` 编译 —— 语法上就写不出 import / 赋值 / 语句。
2. `__builtins__` 置空 —— 拿不到 eval / open / getattr / __import__。
3. AST 预扫 —— 禁掉一切下划线开头的名字与属性, 堵死
   `().__class__.__mro__[1].__subclasses__()` 这条经典逃逸链。

这是本地单用户工具的合理取舍; 若将来要把 HTTP transport 暴露到可信边界之外,
应改用结构化 filter 描述而不是放开这里。
"""

from __future__ import annotations

import ast
import datetime
from typing import Any

from sqlalchemy import and_, asc, desc, extract, func, not_, or_, text

from .._jqsdk import SqlQuery, balance, cash_flow, income, indicator, query, valuation
from ..finance import finance
from ..finance_schema import RUN_QUERY_TABLES

# 不依赖数据库、构造代价为零的基础名字。finance 的 STK_*/FUND_* 模型不在此列:
# build_model 要 DESCRIBE 实表, 预建 30 张会在每次调用前打 30 个来回, 故按需注入。
_BASE_NAMESPACE: dict[str, Any] = {
    "query": query,
    "income": income,
    "balance": balance,
    "cash_flow": cash_flow,
    "indicator": indicator,
    "valuation": valuation,
    "finance": finance,
    "and_": and_,
    "or_": or_,
    "not_": not_,
    "func": func,
    "desc": desc,
    "asc": asc,
    "extract": extract,
    "text": text,
    "date": datetime.date,
    "datetime": datetime.datetime,
    "True": True,
    "False": False,
    "None": None,
}


def available_names() -> list[str]:
    """表达式里可用的全部名字, 供 kedu_describe 与报错信息展示."""
    return sorted(_BASE_NAMESPACE) + sorted(RUN_QUERY_TABLES)


def _comprehension_targets(tree: ast.AST) -> set[str]:
    """收集推导式绑定的临时变量名, 这些名字不该按未定义处理."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.comprehension):
            for name in ast.walk(node.target):
                if isinstance(name, ast.Name):
                    bound.add(name.id)
    return bound


def _validate(tree: ast.AST, expr: str) -> set[str]:
    """AST 预扫: 禁下划线名字/属性, 并返回表达式引用到的自由变量名."""
    bound = _comprehension_targets(tree)
    referenced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise ValueError(
                f"表达式禁止访问下划线属性 {node.attr!r}(沙箱逃逸防护): {expr}"
            )
        if isinstance(node, ast.Name):
            if node.id.startswith("_"):
                raise ValueError(
                    f"表达式禁止使用下划线名字 {node.id!r}(沙箱逃逸防护): {expr}"
                )
            if node.id not in bound:
                referenced.add(node.id)
    return referenced


def _build_namespace(referenced: set[str], expr: str) -> dict[str, Any]:
    """按引用到的名字装配命名空间, finance 表模型按需构建."""
    ns = dict(_BASE_NAMESPACE)
    for name in referenced:
        if name in ns:
            continue
        if name in RUN_QUERY_TABLES:
            ns[name] = getattr(finance, name)  # build_model 内部有缓存
            continue
        raise ValueError(
            f"表达式引用了未知名字 {name!r}: {expr}\n"
            f"表达式只能用下列名字(没有 builtins, 也没有 import): "
            f"{', '.join(available_names())}"
        )
    return ns


def eval_expr(expr: str) -> Any:
    """在受限命名空间里求值一个表达式字符串."""
    if not isinstance(expr, str) or not expr.strip():
        raise ValueError("表达式不能为空")
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as e:
        raise ValueError(f"表达式语法错误: {e}\n{expr}") from None
    ns = _build_namespace(_validate(tree, expr), expr)
    code = compile(tree, "<kedu-mcp-dsl>", "eval")
    return eval(code, {"__builtins__": {}}, ns)  # noqa: S307  三道闸见模块 docstring


def eval_query(expr: str) -> SqlQuery:
    """求值并断言结果是 SqlQuery, 给模型一个明确的形状错误提示."""
    result = eval_expr(expr)
    if not isinstance(result, SqlQuery):
        raise ValueError(
            f"表达式求值结果是 {type(result).__name__}, 不是查询对象; "
            f"应形如 query(valuation).filter(valuation.pe_ratio < 10).limit(50): {expr}"
        )
    return result


def eval_fields(exprs: list[str]) -> list[Any]:
    """求值 get_history_fundamentals 的 fields, 每项形如 'income.total_operating_revenue'."""
    if not exprs:
        raise ValueError(
            "fields 不能为空, 应形如 ['valuation.pe_ratio', 'income.np_parent_company_owners']"
        )
    return [eval_expr(e) for e in exprs]
