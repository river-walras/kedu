"""统一比对工具:本地 kedu 结果 vs live jqdatasdk 结果,逐字段值校验。

集中原先散落在 verify_finance_run_query / verify_history_continuously /
verify_calendar_securities 的比对逻辑,供所有 test_*.py 复用。

约定:
- 数值列:两边都 NaN 视为相等,否则 abs<=tol(或可选 rel<=rtol)视为相等;
- 日期/时间列:按 ISO 字符串(前 10 位)精确比较;
- 字符串列:精确比较。
"""
from __future__ import annotations

import datetime as _dt

import numpy as np
import pandas as pd

TOL = 1e-4


# ---------------------------------------------------------------------------
# 列级标量转换
# ---------------------------------------------------------------------------
def as_num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def as_str(s: pd.Series) -> pd.Series:
    """统一成字符串:日期/时间取 ISO 前 10 位,缺失为空串。"""

    def conv(v):
        try:
            if v is None or v is pd.NaT or pd.isna(v):
                return ""
        except (TypeError, ValueError):
            pass
        if isinstance(v, pd.Timestamp):
            return v.date().isoformat()
        if isinstance(v, (_dt.date, _dt.datetime)):
            return v.isoformat()[:10]
        return str(v)

    return s.map(conv)


def is_datelike(s: pd.Series) -> bool:
    if pd.api.types.is_datetime64_any_dtype(s.dtype):
        return True
    nn = s.dropna()
    return len(nn) > 0 and isinstance(nn.iloc[0], (_dt.date, _dt.datetime, pd.Timestamp))


# ---------------------------------------------------------------------------
# 列比较
# ---------------------------------------------------------------------------
def col_equal(a: pd.Series, b: pd.Series, tol: float = TOL, rtol: float | None = None):
    """返回 (ok: bool, max_abs: float, n_bad: int)。

    - 日期列按 ISO 字符串精确比;
    - 数值列 abs<=tol(给 rtol 时再或 rel<=rtol);两边 NaN 视为相等;
    - 其余按字符串精确比。
    """
    if is_datelike(a) or is_datelike(b):
        sa, sb = as_str(a), as_str(b)
        bad = sa.to_numpy() != sb.to_numpy()
        return (not bad.any()), 0.0, int(bad.sum())

    has_str = a.map(lambda v: isinstance(v, str)).any() or b.map(
        lambda v: isinstance(v, str)
    ).any()
    na, nb = as_num(a), as_num(b)
    numeric = (na.notna().any() or nb.notna().any()) and not has_str
    if numeric:
        x = na.to_numpy(dtype="float64")
        y = nb.to_numpy(dtype="float64")
        both_nan = np.isnan(x) & np.isnan(y)
        absd = np.abs(x - y)
        ok = both_nan | (absd <= tol)
        if rtol is not None:
            reld = absd / np.maximum(np.abs(y), 1e-12)
            ok = ok | (reld <= rtol)
        bad = ~ok
        finite = ~both_nan & np.isfinite(absd)
        mx = float(absd[finite].max()) if finite.any() else 0.0
        return (not bad.any()), mx, int(bad.sum())

    sa, sb = as_str(a), as_str(b)
    bad = sa.to_numpy() != sb.to_numpy()
    return (not bad.any()), 0.0, int(bad.sum())


# ---------------------------------------------------------------------------
# DataFrame 比较(行集合按 keys 对齐)
# ---------------------------------------------------------------------------
def df_compare(
    local: pd.DataFrame,
    live: pd.DataFrame,
    name: str,
    keys: list[str],
    tol: float = TOL,
) -> tuple[bool, list[str]]:
    """列集合一致 + 行数一致 + 逐列 col_equal。返回 (ok, messages)。"""
    msgs: list[str] = []
    lc, vc = set(local.columns), set(live.columns)
    if lc != vc:
        msgs.append(f"{name}: 列集合不同 local-only={lc - vc} live-only={vc - lc}")
        return False, msgs
    cols = list(live.columns)
    keys = [k for k in keys if k in cols]
    L = local[cols].sort_values(keys, kind="stable").reset_index(drop=True)
    V = live[cols].sort_values(keys, kind="stable").reset_index(drop=True)
    if len(L) != len(V):
        msgs.append(f"{name}: 行数 local={len(L)} live={len(V)}")
        return False, msgs
    if len(V) == 0:
        msgs.append(f"{name}: 0 行(两侧均空)")
        return True, msgs
    ok_all, worst, wcol = True, 0.0, None
    for c in cols:
        ok, mx, nbad = col_equal(L[c], V[c], tol=tol)
        if not ok:
            ok_all = False
            msgs.append(f"{name}.{c}: {nbad} 处不一致 (max_abs={mx:.3g})")
        if mx > worst:
            worst, wcol = mx, c
    if ok_all:
        msgs.append(f"{name}: {len(V)} 行 × {len(cols)} 列 一致 (max_abs={worst:.2g} @{wcol})")
    return ok_all, msgs


# ---------------------------------------------------------------------------
# 交易日历比较(numpy.ndarray of datetime.date)
# ---------------------------------------------------------------------------
def eq_days(a, b) -> str | None:
    """逐元素比较两个交易日序列;None=一致,否则差异描述。"""
    la, lb = list(a), list(b)
    if len(la) != len(lb):
        head = f"len local={len(la)} live={len(lb)}"
        if la and lb:
            head += f" | local[0,-1]=({la[0]},{la[-1]}) live[0,-1]=({lb[0]},{lb[-1]})"
        return head
    for i, (x, y) in enumerate(zip(la, lb)):
        if x != y:
            return f"idx {i}: local={x!r} live={y!r}"
    if str(getattr(a, "dtype", None)) != str(getattr(b, "dtype", None)):
        return f"dtype local={getattr(a, 'dtype', None)} live={getattr(b, 'dtype', None)}"
    return None


# ---------------------------------------------------------------------------
# 证券列表 DataFrame 比较(index=code)
# ---------------------------------------------------------------------------
def secdf_compare(local: pd.DataFrame, live: pd.DataFrame, label: str) -> tuple[bool, list[str]]:
    """证券列表逐字段比较:列、index.name、行集合、各字段、日期列。返回 (ok, issues)。"""
    li, lv = local.sort_index(), live.sort_index()
    issues: list[str] = []
    if list(li.columns) != list(lv.columns):
        issues.append(f"columns local={list(li.columns)} live={list(lv.columns)}")
    if li.index.name != lv.index.name:
        issues.append(f"index.name local={li.index.name!r} live={lv.index.name!r}")
    sl, sv = set(li.index), set(lv.index)
    if sl != sv:
        issues.append(
            f"index 行集差异 n local={len(sl)} live={len(sv)} "
            f"only_local={sorted(sl - sv)[:5]} only_live={sorted(sv - sl)[:5]}"
        )
    common = li.index.intersection(lv.index)
    for col in ("display_name", "name", "type"):
        if col in li and col in lv:
            diff = li.loc[common, col].astype(str) != lv.loc[common, col].astype(str)
            if diff.any():
                ex = common[diff][:3].tolist()
                issues.append(f"{col}: {int(diff.sum())} 处不同, 例 {ex}")
    for col in ("start_date", "end_date"):
        if col in li and col in lv:
            a = pd.to_datetime(li.loc[common, col]).values
            b = pd.to_datetime(lv.loc[common, col]).values
            diff = a != b
            if diff.any():
                ex = common[diff][:3].tolist()
                issues.append(f"{col}: {int(diff.sum())} 处不同, 例 {ex}")
    return (not issues), issues
