"""严格值校验:本地 get_fundamentals vs live get_fundamentals。

100 票确定性抽样 × 多报告期(季/年) + 多截面 date × {income/balance/cash_flow/
indicator/valuation},逐字段精确比对(abs<=1e-4 或 rel<=1e-8),并校验行集合一致。

live 结果走快照缓存(首次拉取后落盘,重跑 0 配额)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import kedu
import jqdatasdk

from kedu import get_fundamentals as local_gf
from kedu.fundamentals import _postprocess_fundamentals
from kedu.schema import data_columns

from tests._compare import col_equal

# vendoring 后 kedu 的查询模型已与 jqdatasdk 不是同一对象;必须各自用各自的模型构造
# query —— 给 kedu.get_fundamentals 喂 kedu 模型、给 live jqdatasdk 喂 jqdatasdk 模型,
# 否则两套同名(__tablename__ 相同)但不同身份的模型混入同一 query 会被当作两张表(自连),
# 产生笛卡尔积。列名两侧逐字一致,故抽列、快照 key 用哪一侧都一样。
L_TABLES = {
    "income": kedu.income,
    "balance": kedu.balance,
    "cash_flow": kedu.cash_flow,
    "indicator": kedu.indicator,
    "valuation": kedu.valuation,
}
V_TABLES = {
    "income": jqdatasdk.income,
    "balance": jqdatasdk.balance,
    "cash_flow": jqdatasdk.cash_flow,
    "indicator": jqdatasdk.indicator,
    "valuation": jqdatasdk.valuation,
}

STAT_QUARTERS = ["2024q1", "2024q3", "2024q4", "2020q2", "2015q1", "2010q1"]
STAT_YEARS = ["2023", "2019", "2014"]
DATES = ["2024-05-10", "2010-06-01"]


def _plans():
    """生成 (table, mode_kw, tag) 列表。"""
    out = []
    for sd in STAT_QUARTERS + STAT_YEARS:
        for t in ("income", "balance", "cash_flow", "indicator"):
            out.append((t, {"statDate": sd}, f"statDate={sd}"))
    for d in DATES:
        for t in ("income", "balance", "cash_flow", "indicator", "valuation"):
            out.append((t, {"date": d}, f"date={d}"))
    return out


PLANS = _plans()
PLAN_IDS = [f"{t}-{tag}" for t, _, tag in PLANS]


def test_postprocess_handles_duplicate_date_columns():
    df = pd.DataFrame(
        [
            [1, "000001.XSHE", "2024-03-31", "2024-04-20", "2024-03-31", "10.5"],
        ],
        columns=["id", "code", "statDate", "pubDate", "statDate", "net_profit"],
    )

    out = _postprocess_fundamentals(df)

    assert list(out.columns) == [
        "id",
        "code",
        "statDate",
        "pubDate",
        "statDate.1",
        "net_profit",
    ]
    assert pd.api.types.is_datetime64_any_dtype(out["statDate"])
    assert pd.api.types.is_datetime64_any_dtype(out["statDate.1"])
    assert pd.api.types.is_float_dtype(out["net_profit"])


def _build_query(qfn, model, cols, codes):
    return qfn(model.code, *[getattr(model, c) for c in cols]).filter(
        model.code.in_(codes)
    )


@pytest.mark.parametrize("table,mode,tag", PLANS, ids=PLAN_IDS)
def test_get_fundamentals(table, mode, tag, sample_codes, snap):
    l_model, v_model = L_TABLES[table], V_TABLES[table]
    cols = data_columns(v_model)
    codes = sample_codes

    loc = local_gf(_build_query(kedu.query, l_model, cols, codes), **mode)
    live = snap.get(
        f"fundamentals-{table}-{tag}",
        (table, tag, tuple(codes), tuple(cols)),
        lambda: jqdatasdk.get_fundamentals(
            _build_query(jqdatasdk.query, v_model, cols, codes), **mode),
    )

    if loc.empty and live.empty:
        pytest.skip(f"{table} @ {tag}: 两侧均空")

    loc = loc.drop_duplicates("code").set_index("code")
    live = live.drop_duplicates("code").set_index("code")

    only_local = sorted(loc.index.difference(live.index))
    only_live = sorted(live.index.difference(loc.index))
    common = loc.index.intersection(live.index)
    loc, live = loc.loc[common], live.loc[common]

    problems: list[str] = []
    if only_local:
        problems.append(f"only_local={only_local[:5]}(n={len(only_local)})")
    if only_live:
        problems.append(f"only_live={only_live[:5]}(n={len(only_live)})")

    for c in cols:
        if c not in loc or c not in live:
            problems.append(f"{c}: 缺列")
            continue
        a = pd.to_numeric(loc[c], errors="coerce")
        b = pd.to_numeric(live[c], errors="coerce")
        # NaN 错配(一边有值一边 NaN)
        nan_mismatch = int((a.isna().to_numpy() ^ b.isna().to_numpy()).sum())
        ok, mx, nbad = col_equal(a, b, tol=1e-4, rtol=1e-8)
        if not ok or nan_mismatch:
            i = int(np.argmax((~np.isclose(a.to_numpy(float), b.to_numpy(float),
                                           rtol=1e-8, atol=1e-4, equal_nan=True))))
            ex = f" e.g.{common[i]} L={a.iloc[i]} J={b.iloc[i]}"
            problems.append(
                f"{c}: mismatch={nbad} nan_mismatch={nan_mismatch} max_abs={mx:.4g}{ex}"
            )

    assert not problems, f"{table} @ {tag} (n={len(common)}): " + " | ".join(problems)
