"""校验本地 get_fundamentals_continuously / get_history_fundamentals vs live。

多参数组合:continuously(多交易日,end_date+count)、history(stat_date 1q/1y、
stat_by_year、watch_date、多字段跨表、多标的、100 票广覆盖)。逐字段比对。
live 结果走快照缓存。
"""
from __future__ import annotations

import pytest
from jqdatasdk import balance, cash_flow, income, indicator

import kedu
import jqdatasdk

from tests._compare import df_compare

SEC2 = ["000001.XSHE", "600000.XSHG"]


# ---------------------------------------------------------------------------
# get_fundamentals_continuously
# ---------------------------------------------------------------------------
# (name, query_builder, kwargs)
# qb 取命名空间 ns(kedu 或 jqdatasdk):continuously 会重建 query,vendoring 后两套模型
# 不同身份,必须各喂各的模型,否则同名表自连成笛卡尔积(见 test_fundamentals 同款修复)。
CONT_CASES = [
    ("CONT 文档示例 2票×5日",
     lambda ns: ns.query(ns.valuation.turnover_ratio, ns.valuation.market_cap, ns.indicator.eps)
     .filter(ns.valuation.code.in_(["000001.XSHE", "600000.XSHG"])),
     dict(end_date="2017-12-25", count=5)),
    ("CONT valuation+income 3票×3日",
     lambda ns: ns.query(ns.valuation.pe_ratio, ns.valuation.pb_ratio,
                         ns.income.total_operating_revenue)
     .filter(ns.valuation.code.in_(["600519.XSHG", "000651.XSHE", "601318.XSHG"])),
     dict(end_date="2021-06-30", count=3)),
    ("CONT 单字段 1票×10日",
     lambda ns: ns.query(ns.valuation.market_cap).filter(ns.valuation.code.in_(["000001.XSHE"])),
     dict(end_date="2020-03-20", count=10)),
]


@pytest.mark.parametrize("name,qb,kw", CONT_CASES, ids=[c[0] for c in CONT_CASES])
def test_continuously(name, qb, kw, snap, clickhouse_auth):
    loc = kedu.get_fundamentals_continuously(qb(kedu), panel=False, **kw)
    live = snap.get(
        f"continuously-{name}", (name, sorted(kw.items())),
        lambda: jqdatasdk.get_fundamentals_continuously(qb(jqdatasdk), panel=False, **kw),
    )
    ok, msgs = df_compare(loc, live, name, keys=["code", "day"])
    assert ok, " | ".join(msgs)


# ---------------------------------------------------------------------------
# get_history_fundamentals
# ---------------------------------------------------------------------------
# (name, sec, fields, kwargs)
HIST_CASES = [
    ("HIST stat 1q 跨表", SEC2,
     [balance.cash_equivalents, cash_flow.net_deposit_increase,
      income.total_operating_revenue],
     dict(stat_date="2019q1", count=5, interval="1q")),
    ("HIST stat 1y", ["000001.XSHE"],
     [income.total_operating_revenue, income.np_parent_company_owners],
     dict(stat_date="2019q1", count=4, interval="1y")),
    ("HIST stat_by_year", SEC2,
     [income.total_operating_revenue, balance.total_assets],
     dict(stat_date="2018", count=4, interval="1y", stat_by_year=True)),
    ("HIST watch_date 1q", SEC2,
     [income.total_operating_revenue, income.np_parent_company_owners],
     dict(watch_date="2019-05-01", count=3, interval="1q")),
    ("HIST watch_date indicator", SEC2,
     [indicator.eps, indicator.roe, indicator.inc_net_profit_year_on_year],
     dict(watch_date="2020-09-30", count=4, interval="1q")),
    ("HIST 多标的 stat 1q",
     ["000001.XSHE", "600519.XSHG", "000651.XSHE", "601318.XSHG", "002594.XSHE"],
     [income.operating_revenue, income.net_profit],
     dict(stat_date="2022q4", count=4, interval="1q")),
]


def _hist_compare(snap, name, sec, fields, kw):
    loc = kedu.get_history_fundamentals(sec, fields, **kw)
    live = snap.get(
        f"history-{name}",
        (name, tuple(sec), tuple(str(f) for f in fields), sorted(kw.items())),
        lambda: jqdatasdk.get_history_fundamentals(sec, fields, **kw),
    )
    return df_compare(loc, live, name, keys=["code", "statDate"])


@pytest.mark.parametrize("name,sec,fields,kw", HIST_CASES, ids=[c[0] for c in HIST_CASES])
def test_history(name, sec, fields, kw, snap, clickhouse_auth):
    ok, msgs = _hist_compare(snap, name, sec, fields, kw)
    assert ok, " | ".join(msgs)


def test_history_100codes_stat(sample_codes, snap):
    ok, msgs = _hist_compare(
        snap, "HIST 100票 stat 1q", sample_codes,
        [income.total_operating_revenue, balance.total_assets, indicator.eps],
        dict(stat_date="2023q2", count=3, interval="1q"),
    )
    assert ok, " | ".join(msgs)


def test_history_100codes_watch(sample_codes, snap):
    ok, msgs = _hist_compare(
        snap, "HIST 100票 watch_date", sample_codes,
        [income.net_profit, indicator.roe],
        dict(watch_date="2022-08-15", count=2, interval="1q"),
    )
    assert ok, " | ".join(msgs)
