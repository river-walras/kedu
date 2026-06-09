"""校验本地 kedu.get_extras('is_st') vs live jqdatasdk.get_extras。

宽表(index=交易日、columns=代码、值=bool/NaN)按 (date, code) 展开为长表逐值比对,
布尔/缺失统一归一为 0/1/NaN 后比较。含文档示例、单票、count 模式、100 票抽样。
live 结果走快照缓存。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import kedu
import jqdatasdk

from tests._compare import df_compare


def _melt(df: pd.DataFrame) -> pd.DataFrame:
    """宽表 -> 长表 (date, code, is_st);is_st 归一为 0/1/NaN(float),消除 bool/float 表示差异。"""
    d = df.copy()
    d.index = pd.to_datetime(d.index)
    d.index.name = "date"
    long = d.reset_index().melt(id_vars="date", var_name="code", value_name="is_st")
    long["is_st"] = long["is_st"].map(lambda v: np.nan if pd.isna(v) else float(bool(v)))
    return long


# (name, codes, kwargs)
EXTRAS_CASES = [
    ("EXTRAS 文档示例 000001+000018 2021-12-01..03",
     ["000001.XSHE", "000018.XSHE"], dict(start_date="2021-12-01", end_date="2021-12-03")),
    ("EXTRAS 单票 600519 2019 全年",
     ["600519.XSHG"], dict(start_date="2019-01-01", end_date="2019-12-31")),
    ("EXTRAS count 模式 000018 end=2020-12-31 count=20",
     ["000018.XSHE"], dict(end_date="2020-12-31", count=20)),
]


def _compare(snap, name, codes, kw):
    loc = kedu.get_extras("is_st", codes, **kw)
    live = snap.get(
        f"extras-is_st-{name}", (name, tuple(codes), sorted(kw.items())),
        lambda: jqdatasdk.get_extras("is_st", codes, **kw),
    )
    return df_compare(_melt(loc), _melt(live), name, keys=["date", "code"])


@pytest.mark.parametrize("name,codes,kw", EXTRAS_CASES, ids=[c[0] for c in EXTRAS_CASES])
def test_get_extras_is_st(name, codes, kw, snap, clickhouse_auth):
    ok, msgs = _compare(snap, name, codes, kw)
    assert ok, " | ".join(msgs)


def test_get_extras_100codes(sample_codes, snap):
    ok, msgs = _compare(
        snap, "EXTRAS 100票 2022 全年", sample_codes,
        dict(start_date="2022-01-01", end_date="2022-12-31"),
    )
    assert ok, " | ".join(msgs)


def test_get_extras_rejects_unsupported_info():
    """不支持的 info(非 is_st、非基金净值)仍报 NotImplementedError。"""
    with pytest.raises(NotImplementedError):
        kedu.get_extras("futures_positions", ["000001.XSHE"], start_date="2020-01-01")


def test_get_extras_df_false_dict(snap, clickhouse_auth):
    codes = ["000001.XSHE", "000018.XSHE"]
    kw = dict(start_date="2021-12-01", end_date="2021-12-03")
    wide = kedu.get_extras("is_st", codes, **kw)
    d = kedu.get_extras("is_st", codes, df=False, **kw)
    assert set(d) == set(codes)
    for c in codes:
        assert isinstance(d[c], np.ndarray)
        assert len(d[c]) == len(wide.index)
