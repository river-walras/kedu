"""校验本地 kedu.finance.run_query / run_offset_query vs live jqdatasdk.finance。

逐表构造 query(report_type 多版本、in_ 多 end_date、跨行业、别名表、业绩预告/审计/
预约披露、run_query 非 offset、100 票×多期分页),逐字段比对(列集合/行数/值 abs<=1e-4/
日期字符串精确)。live 结果走快照缓存。
"""
from __future__ import annotations

import pytest
from jqdatasdk import query

import jqdatasdk
from kedu.finance import finance as LF

from tests._compare import df_compare

JF = jqdatasdk.finance


def _run(snap, name, table, build, offset=True, live_name=None):
    """构造本地/线上 query 并执行,返回 (ok, messages)。live 走缓存。"""
    local_q = build(getattr(LF, table))
    if offset:
        local = LF.run_offset_query(local_q)
    else:
        local = LF.run_query(local_q)

    def producer():
        live_q = build(getattr(JF, live_name or table))
        return JF.run_offset_query(live_q) if offset else JF.run_query(live_q)

    live = snap.get(f"finance-{name}", (name, table, offset, live_name), producer)
    return df_compare(local, live, name, keys=["id"])


# 固定 case:(name, table, build(model)->query, offset, live_name)
FIXED_CASES = [
    ("INCOME 300080@2019Q1 多版本", "STK_INCOME_STATEMENT",
     lambda m: query(m).filter(m.code == "300080.XSHE", m.end_date == "2019-03-31"),
     True, None),
    ("INCOME 000001 in[2022,2021]", "STK_INCOME_STATEMENT",
     lambda m: query(m).filter(m.code == "000001.XSHE",
                               m.end_date.in_(["2022-12-31", "2021-12-31"])),
     True, None),
    ("BALANCE 000001@2023", "STK_BALANCE_SHEET",
     lambda m: query(m).filter(m.code == "000001.XSHE", m.end_date == "2023-12-31"),
     True, None),
    ("BALANCE 600519@2023", "STK_BALANCE_SHEET",
     lambda m: query(m).filter(m.code == "600519.XSHG", m.end_date == "2023-12-31"),
     True, None),
    ("BALANCE 601318@2023", "STK_BALANCE_SHEET",
     lambda m: query(m).filter(m.code == "601318.XSHG", m.end_date == "2023-12-31"),
     True, None),
    ("BALANCE 000651@2023", "STK_BALANCE_SHEET",
     lambda m: query(m).filter(m.code == "000651.XSHE", m.end_date == "2023-12-31"),
     True, None),
    ("CASHFLOW 000001 in[2023,2022]", "STK_CASHFLOW_STATEMENT",
     lambda m: query(m).filter(m.code == "000001.XSHE",
                               m.end_date.in_(["2023-12-31", "2022-12-31"])),
     True, None),
    # 本地别名 STK_CASH_FLOW_STATEMENT 应等价于现网真实名 STK_CASHFLOW_STATEMENT
    ("CASHFLOW(别名) 600519@2023", "STK_CASH_FLOW_STATEMENT",
     lambda m: query(m).filter(m.code == "600519.XSHG", m.end_date == "2023-12-31"),
     True, "STK_CASHFLOW_STATEMENT"),
    ("FIN_FORCAST 600519 pub>=2015", "STK_FIN_FORCAST",
     lambda m: query(m).filter(m.code == "600519.XSHG", m.pub_date >= "2015-01-01"),
     True, None),
    ("AUDIT_OPINION 600519 end>=2019", "STK_AUDIT_OPINION",
     lambda m: query(m).filter(m.code == "600519.XSHG", m.end_date >= "2019-01-01"),
     True, None),
    ("REPORT_DISCLOSURE 600519 end>=2019", "STK_REPORT_DISCLOSURE",
     lambda m: query(m).filter(m.code == "600519.XSHG", m.end_date >= "2019-01-01"),
     True, None),
    ("STATUS_CHANGE 600276", "STK_STATUS_CHANGE",
     lambda m: query(m).filter(m.code == "600276.XSHG"),
     True, None),
    ("STATUS_CHANGE 000001 pub>=2010", "STK_STATUS_CHANGE",
     lambda m: query(m).filter(m.code == "000001.XSHE", m.pub_date >= "2010-01-01"),
     True, None),
    # run_query(非 offset)默认 5000 上限路径
    ("INCOME 600000@2020Q3 (run_query)", "STK_INCOME_STATEMENT",
     lambda m: query(m).filter(m.code == "600000.XSHG", m.end_date == "2020-09-30"),
     False, None),
]


@pytest.mark.parametrize("name,table,build,offset,live_name", FIXED_CASES,
                         ids=[c[0] for c in FIXED_CASES])
def test_finance_fixed(name, table, build, offset, live_name, snap, clickhouse_auth):
    ok, msgs = _run(snap, name, table, build, offset, live_name)
    assert ok, " | ".join(msgs)


def test_finance_100codes_income(sample_codes, snap):
    """100 票 × 3 期,run_offset_query 分页 + 排序。"""
    codes = sample_codes
    ends = ["2023-12-31", "2022-12-31", "2021-12-31"]
    ok, msgs = _run(
        snap, "INCOME 100票×3期 (offset)", "STK_INCOME_STATEMENT",
        lambda m: query(m).filter(m.code.in_(codes), m.end_date.in_(ends)),
    )
    assert ok, " | ".join(msgs)


def test_finance_100codes_disclosure(sample_codes, snap):
    codes = sample_codes
    ok, msgs = _run(
        snap, "REPORT_DISCLOSURE 100票 end>=2021", "STK_REPORT_DISCLOSURE",
        lambda m: query(m).filter(m.code.in_(codes), m.end_date >= "2021-01-01"),
    )
    assert ok, " | ".join(msgs)
