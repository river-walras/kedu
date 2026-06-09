"""校验本地 kedu.finance.run_query/run_offset_query vs live jqdatasdk.finance 的 10 张基金表。

逐表构造 query(代码形态随表:FUND_MAIN_INFO 用 main_code 裸码、FUND_INVEST_TARGET/
FUND_SHARE_DAILY 用带后缀 code、其余用裸 code),逐字段比对(列集合/行数/值 abs<=1e-4/
日期字符串精确)。live 结果走快照缓存。代码取自 reference/基金/*.md 文档示例,确定性。
"""
from __future__ import annotations

import pytest
from jqdatasdk import query

import jqdatasdk
from kedu.finance import finance as LF

from tests._compare import df_compare

JF = jqdatasdk.finance


def _run(snap, name, table, build, offset=True):
    """构造本地/线上 query 并执行,返回 (ok, messages)。live 走缓存。"""
    local_q = build(getattr(LF, table))
    local = LF.run_offset_query(local_q) if offset else LF.run_query(local_q)

    def producer():
        live_q = build(getattr(JF, table))
        return JF.run_offset_query(live_q) if offset else JF.run_query(live_q)

    live = snap.get(f"fundfin-{name}", (name, table, offset), producer)
    return df_compare(local, live, name, keys=["id"])


# (name, table, build(model)->query, offset)
FIXED_CASES = [
    # 主体信息:main_code 裸码
    ("MAIN_INFO 159937", "FUND_MAIN_INFO",
     lambda m: query(m).filter(m.main_code == "159937")),
    ("MAIN_INFO in[162411,166001]", "FUND_MAIN_INFO",
     lambda m: query(m).filter(m.main_code.in_(["162411", "166001"]))),
    # 净值:裸 code + day 窗口(逐日大表,限窗口控行数)
    ("NET_VALUE 150008@2020", "FUND_NET_VALUE",
     lambda m: query(m).filter(m.code == "150008", m.day >= "2020-01-01", m.day <= "2020-12-31")),
    ("NET_VALUE 000001@2021H1", "FUND_NET_VALUE",
     lambda m: query(m).filter(m.code == "000001", m.day >= "2021-01-01", m.day <= "2021-06-30")),
    # 财务指标:裸 code
    ("FIN_INDICATOR 150012", "FUND_FIN_INDICATOR",
     lambda m: query(m).filter(m.code == "150012")),
    ("FIN_INDICATOR 184688", "FUND_FIN_INDICATOR",
     lambda m: query(m).filter(m.code == "184688")),
    # 资产组合概况
    ("PORTFOLIO 150023", "FUND_PORTFOLIO",
     lambda m: query(m).filter(m.code == "150023")),
    ("PORTFOLIO 184688", "FUND_PORTFOLIO",
     lambda m: query(m).filter(m.code == "184688")),
    # 持债
    ("PORTFOLIO_BOND 398051", "FUND_PORTFOLIO_BOND",
     lambda m: query(m).filter(m.code == "398051")),
    # 持股
    ("PORTFOLIO_STOCK 150016", "FUND_PORTFOLIO_STOCK",
     lambda m: query(m).filter(m.code == "150016")),
    # ETF 跟踪指数:带后缀 code
    ("INVEST_TARGET 510190.XSHG", "FUND_INVEST_TARGET",
     lambda m: query(m).filter(m.code == "510190.XSHG")),
    # 分红拆分合并
    ("DIVIDEND 150018", "FUND_DIVIDEND",
     lambda m: query(m).filter(m.code == "150018")),
    ("DIVIDEND 184688", "FUND_DIVIDEND",
     lambda m: query(m).filter(m.code == "184688")),
    # 场内份额:date 键(返回当日全场内份额)
    ("SHARE_DAILY 2019-05-23", "FUND_SHARE_DAILY",
     lambda m: query(m).filter(m.date == "2019-05-23")),
    # 场内份额:带后缀 code 时序
    ("SHARE_DAILY 150008.XSHE", "FUND_SHARE_DAILY",
     lambda m: query(m).filter(m.code == "150008.XSHE")),
    # 货基收益日报:裸 code
    ("MF_DAILY_PROFIT 000330", "FUND_MF_DAILY_PROFIT",
     lambda m: query(m).filter(m.code == "000330")),
    # run_query(非 offset)路径
    ("MF_DAILY_PROFIT 000330 (run_query)", "FUND_MF_DAILY_PROFIT",
     lambda m: query(m).filter(m.code == "000330")),
]


@pytest.mark.parametrize("name,table,build", [(c[0], c[1], c[2]) for c in FIXED_CASES],
                         ids=[c[0] for c in FIXED_CASES])
def test_fund_finance_fixed(name, table, build, snap, clickhouse_auth):
    offset = not name.endswith("(run_query)")
    ok, msgs = _run(snap, name, table, build, offset=offset)
    assert ok, " | ".join(msgs)


def test_fund_net_value_multi(snap, clickhouse_auth):
    """多基金 × 窗口净值(offset 分页)。"""
    codes = ["150008", "000001", "510300", "159937"]
    ok, msgs = _run(
        snap, "NET_VALUE 4票@2020", "FUND_NET_VALUE",
        lambda m: query(m).filter(m.code.in_(codes), m.day >= "2020-01-01", m.day <= "2020-12-31"),
    )
    assert ok, " | ".join(msgs)
