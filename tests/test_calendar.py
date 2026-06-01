"""交易日历校验:本地 get_trade_days / get_all_trade_days vs live jqdatasdk。

重点验 start/end 区间语义逐项一致(闭区间、跨春节/跨年、单日、仅 start 到 today、
count 与 start/end 互斥规则)以及非法参数双方都抛错。

日历查询在 JQ 不计配额,故直接打 live(不走快照缓存),但仍需 JQ 凭证。
"""
from __future__ import annotations

import pytest

import kedu
import jqdatasdk

from tests._compare import eq_days
from tests._snapshot import ensure_jq_auth


@pytest.fixture(scope="module", autouse=True)
def _auth(clickhouse_auth):
    ensure_jq_auth()


# get_trade_days 合法参数:本地与 live 应逐元素一致
LEGAL_CASES = [
    dict(start_date="2018-02-10", end_date="2018-03-01"),   # 文档例1:区间
    dict(start_date="2018-03-10", count=10),                 # 文档例2:前向 count
    dict(end_date="2018-03-01", count=9),                    # 后向 count
    dict(count=5),                                            # 默认 today 后向
    dict(start_date="2024-01-01", end_date="2024-12-31"),    # 全年
    dict(start_date="2024-02-05", end_date="2024-02-22"),    # 跨春节
    dict(start_date="2023-12-25", end_date="2024-01-10"),    # 跨年
    dict(start_date="2026-05-29", end_date="2026-05-29"),    # 单日(交易日)
    dict(start_date="2026-05-30", end_date="2026-05-30"),    # 单日(周六非交易日)
    dict(start_date="2020-05-01"),                           # 仅 start -> 至 today
    dict(start_date="2025-01-01", count=3),                  # 前向小样本
    dict(start_date="2015-06-12", end_date="2015-09-30"),    # 区间(含 2015 股灾停牌密集期)
]

# 非法参数:双方都应抛错
ILLEGAL_CASES = [
    dict(start_date="2024-01-01", end_date="2024-02-01", count=5),  # count 与区间并存
    dict(count=0),
    dict(count=-3),
    dict(),                                                          # 既无 start 又无 count
    dict(end_date="2024-06-01"),                                    # 只有 end
]


def _label(kw: dict) -> str:
    return "get_trade_days(" + ", ".join(f"{k}={v!r}" for k, v in kw.items()) + ")"


def test_get_all_trade_days():
    import datetime as _dt

    import numpy as np

    la = list(kedu.get_all_trade_days())
    lv = list(jqdatasdk.get_all_trade_days())
    # 公共区间必须逐日一致(历史/已确定的交易日)
    n = min(len(la), len(lv))
    diff = eq_days(np.array(la[:n], dtype=object), np.array(lv[:n], dtype=object))
    assert diff is None, f"get_all_trade_days 公共区间不一致: {diff}"
    # 尾部长度差异只允许出现在「未来已发布日历」(今天之后)—— 两侧同步时点不同会差几日,非数据错误
    today = _dt.date.today()
    tail = la[n:] or lv[n:]
    assert all(d > today for d in tail), (
        f"get_all_trade_days 尾部存在非未来日差异(疑似历史缺失): {tail[:5]} "
        f"(local n={len(la)}, live n={len(lv)})"
    )


@pytest.mark.parametrize("kw", LEGAL_CASES, ids=_label)
def test_get_trade_days_legal(kw):
    lv = jqdatasdk.get_trade_days(**kw)
    la = kedu.get_trade_days(**kw)
    diff = eq_days(la, lv)
    assert diff is None, f"{_label(kw)} 不一致: {diff}"


@pytest.mark.parametrize("kw", ILLEGAL_CASES, ids=_label)
def test_get_trade_days_illegal(kw):
    live_raised = local_raised = None
    try:
        jqdatasdk.get_trade_days(**kw)
    except Exception as e:  # noqa: BLE001
        live_raised = type(e).__name__
    try:
        kedu.get_trade_days(**kw)
    except Exception as e:  # noqa: BLE001
        local_raised = type(e).__name__
    assert live_raised and local_raised, (
        f"{_label(kw)} 抛错不一致 local={local_raised} live={live_raised}"
    )
