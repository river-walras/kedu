"""校验本地 kedu.get_extras 基金净值(unit/acc/adj_net_value) vs live jqdatasdk.get_extras。

宽表(index=交易日、columns=代码、值=float 净值/NaN)melt 为长表逐值比对(abs<=1e-4)。
含场内 ETF + 场外基金、df=False dict、count 模式、adj_net_value 场外限制、错误代码报错。
live 结果走快照缓存(实测语义见 Phase 0 P0c)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import kedu
import jqdatasdk

from tests._compare import df_compare


def _melt(df: pd.DataFrame) -> pd.DataFrame:
    """宽表 -> 长表 (date, code, val);保留 NaN(净值缺失不填充)。"""
    d = df.copy()
    d.index = pd.to_datetime(d.index)
    d.index.name = "date"
    long = d.reset_index().melt(id_vars="date", var_name="code", value_name="val")
    long["val"] = pd.to_numeric(long["val"], errors="coerce")
    return long


# (name, info, codes, kwargs)
CASES = [
    ("UNIT 510300+000001 2021-01",
     "unit_net_value", ["510300.XSHG", "000001.OF"],
     dict(start_date="2021-01-04", end_date="2021-01-15")),
    ("ACC 510300+000001 2021-01",
     "acc_net_value", ["510300.XSHG", "000001.OF"],
     dict(start_date="2021-01-04", end_date="2021-01-15")),
    # adj_net_value 仅场外:用场外基金
    ("ADJ 000001.OF 2021-01",
     "adj_net_value", ["000001.OF"],
     dict(start_date="2021-01-04", end_date="2021-01-15")),
    # count 模式
    ("UNIT 510300 count=20",
     "unit_net_value", ["510300.XSHG"],
     dict(end_date="2021-01-15", count=20)),
]


def _compare(snap, name, info, codes, kw):
    loc = kedu.get_extras(info, codes, **kw)
    live = snap.get(
        f"extras-fund-{name}", (name, info, tuple(codes), sorted(kw.items())),
        lambda: jqdatasdk.get_extras(info, codes, **kw),
    )
    return df_compare(_melt(loc), _melt(live), name, keys=["date", "code"])


@pytest.mark.parametrize("name,info,codes,kw", CASES, ids=[c[0] for c in CASES])
def test_get_extras_fund_netvalue(name, info, codes, kw, snap, clickhouse_auth):
    ok, msgs = _compare(snap, name, info, codes, kw)
    assert ok, " | ".join(msgs)


def test_get_extras_adj_onexchange_rejected(clickhouse_auth):
    """adj_net_value 对场内基金报错(复刻聚宽:仅支持场外基金)。"""
    with pytest.raises(Exception, match="场外"):
        kedu.get_extras("adj_net_value", ["510300.XSHG"], start_date="2021-01-04",
                        end_date="2021-01-08")


def test_get_extras_fund_bare_code_rejected(clickhouse_auth):
    """无后缀裸码报「找不到标的」(复刻聚宽代码解析)。"""
    with pytest.raises(Exception, match="找不到标的"):
        kedu.get_extras("unit_net_value", ["510300"], start_date="2021-01-04",
                        end_date="2021-01-08")


def test_get_extras_fund_df_false(snap, clickhouse_auth):
    codes = ["510300.XSHG", "000001.OF"]
    kw = dict(start_date="2021-01-04", end_date="2021-01-15")
    wide = kedu.get_extras("unit_net_value", codes, **kw)
    d = kedu.get_extras("unit_net_value", codes, df=False, **kw)
    assert set(d) == set(codes)
    for c in codes:
        assert isinstance(d[c], np.ndarray)
        assert len(d[c]) == len(wide.index)
