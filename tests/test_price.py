"""日线行情严格值校验:本地 get_price(frequency='daily') vs live jqdatasdk。

抽样档位由 --price-scale 决定(light=20/medium=30/heavy=50 票,默认 heavy)。
窗口由本地 bar_1d 的 factor 变化推导(除权事件 ±窗口 + 近一年;不耗 JQ 配额),
再定向去 live 校验 —— 最大化每条配额覆盖的复权场景。live 结果走快照缓存。

复权口径(见 src/kedu/prices.py):
- fq=None:原始 OHLCV,与聚宽逐字段精确一致 -> 严格比对全部字段;
- fq='post':bar_1d.factor 直接取自聚宽 get_price(fq='post').factor,基准与聚宽一致,
  后复权价 = raw×factor 应与聚宽逐字段一致 -> 严格比对(首跑若出现 ~0.01 偏差,
  即 prices.py 对 post 价 round(2) 的舍入噪声);
- fq='pre':最新交易日=原始价,与聚宽一致,但需数据覆盖到最新日 -> 仅
  --refresh-snapshots 时对少数代表股在近窗口严格比对。
"""
from __future__ import annotations

import pytest

import jqdatasdk
from kedu import get_price as local_price

from tests._compare import col_equal, df_compare
from tests._sampling import PINNED, ex_div_windows

FIELDS = ["open", "close", "high", "low", "volume", "money", "avg",
          "high_limit", "low_limit", "pre_close", "factor", "paused"]


def _live_daily(snap, code, start, end, fq):
    fq_tag = "none" if fq is None else fq
    return snap.get(
        f"price-{code}-{start}-{end}-{fq_tag}",
        (code, start, end, fq_tag, tuple(FIELDS)),
        lambda: jqdatasdk.get_price(
            code, start_date=start, end_date=end, frequency="daily",
            fields=FIELDS, fq=fq, skip_paused=False,
        ),
    )


def _check_strict(loc, live, label) -> list[str]:
    """逐字段精确比对(fq=None / post / pre 通用)。"""
    idx = loc.index.intersection(live.index)
    problems = []
    if len(idx) != len(live.index) or len(idx) != len(loc.index):
        problems.append(f"{label}: 行对齐 local={len(loc)} live={len(live)} common={len(idx)}")
    for c in FIELDS:
        if c not in loc or c not in live:
            problems.append(f"{label}.{c}: 缺列")
            continue
        ok, mx, nbad = col_equal(loc.loc[idx, c], live.loc[idx, c], tol=1e-4)
        if not ok:
            problems.append(f"{label}.{c}: {nbad} 处不一致 (max_abs={mx:.3g})")
    return problems


def _check_code(code, snap) -> tuple[int, list[str]]:
    """对单 code 的关键窗口跑 fq=None 与 fq='post',均逐字段严格比对。返回 (窗口数, 失败明细)。"""
    windows = ex_div_windows(code)
    if not windows:
        return 0, [f"{code}: 本地 bar_1d 无数据"]
    problems: list[str] = []
    for start, end in windows:
        loc_none = local_price(code, start_date=start, end_date=end,
                               frequency="daily", fields=FIELDS, fq=None)
        if loc_none.empty:
            problems.append(f"{code} {start}..{end}: 本地 0 行")
            continue
        live_none = _live_daily(snap, code, start, end, None)
        if live_none is None or live_none.empty:
            continue  # live 无数据(停牌整窗等),跳过该窗口
        problems += _check_strict(loc_none, live_none, f"{code} {start}..{end} none")

        loc_post = local_price(code, start_date=start, end_date=end,
                               frequency="daily", fields=FIELDS, fq="post")
        live_post = _live_daily(snap, code, start, end, "post")
        if live_post is not None and not live_post.empty:
            problems += _check_strict(loc_post, live_post, f"{code} {start}..{end} post")
    return len(windows), problems


@pytest.mark.parametrize("code", PINNED, ids=PINNED)
def test_price_daily_pinned(code, snap, clickhouse_auth):
    """代表股逐只校验(granular)。"""
    n, problems = _check_code(code, snap)
    assert not problems, f"({n} 窗口) " + " | ".join(problems[:20])


def test_price_daily_extended(price_codes, snap, request):
    """抽样档位中 PINNED 以外的扩展票,聚合校验。"""
    scale = request.config.getoption("--price-scale")
    extra = [c for c in price_codes if c not in set(PINNED)]
    if not extra:
        pytest.skip(f"--price-scale={scale}: 无 PINNED 之外的扩展票")
    all_problems: list[str] = []
    for code in extra:
        _, problems = _check_code(code, snap)
        all_problems += problems
    assert not all_problems, (
        f"{len(extra)} 扩展票, {len(all_problems)} 处问题: " + " | ".join(all_problems[:30])
    )


def test_price_daily_pre(snap, request, clickhouse_auth):
    """fq='pre' 严格比对(动态前复权,锚真实最新交易日)。

    取每只代表股最早的除权窗口(factor << 最新 → adj 远小于 1,真正考验前复权),
    pre 由已验证的 raw×factor/factor_latest 推出,几个节点足以确认锚点新鲜度+算术。
    需数据到最新交易日,仅 --refresh-snapshots 时跑(动态锚不可缓存)。
    """
    if not request.config.getoption("--refresh-snapshots"):
        pytest.skip("fq='pre' 锚最新交易日,需新鲜 live;加 --refresh-snapshots 启用")
    problems: list[str] = []
    checked = 0
    for code in PINNED[:3]:
        windows = ex_div_windows(code)
        if not windows:
            continue
        start, end = windows[0]  # 最早窗口:factor 远离最新,adj 明显≠1
        loc = local_price(code, start_date=start, end_date=end,
                          frequency="daily", fields=FIELDS, fq="pre")
        live = _live_daily(snap, code, start, end, "pre")
        if loc.empty or live is None or live.empty:
            continue
        # 该窗口应确实被复权(adj<1),否则测试退化为等于原始价
        assert (loc["factor"] < 0.999).any(), f"{code} {start}..{end}: pre adj≈1,未真正复权"
        problems += _check_strict(loc, live, f"{code} {start}..{end} pre")
        checked += 1
    assert checked > 0, "无可校验的 pre 窗口(数据为空？)"
    assert not problems, " | ".join(problems[:20])


def test_price_pre_count_anchor_local(clickhouse_auth):
    """pre 锚点回归(纯本地,0 配额,永远跑):count 路径与 start/end 路径必须一致。

    动态前复权锚恒为最新交易日,与 count/end_date 无关。守住 count 路径曾误锚
    end_date(静态)的回归。
    """
    code = "600519.XSHG"
    a = local_price(code, start_date="2020-06-10", end_date="2020-07-10",
                    frequency="daily", fields=["close"], fq="pre")
    b = local_price(code, end_date="2020-07-10", count=len(a),
                    frequency="daily", fields=["close"], fq="pre")
    assert len(a) and (a["close"].round(2).to_numpy() == b["close"].round(2).to_numpy()).all(), (
        f"pre count 锚点不一致: start/end 末值={a['close'].iloc[-1]} count 末值={b['close'].iloc[-1]}"
    )


# ---------------------------------------------------------------------------
# 多标的(panel=False 长表)
# ---------------------------------------------------------------------------
MULTI_CODES = ["000001.XSHE", "600519.XSHG", "601318.XSHG", "000651.XSHE", "600036.XSHG"]
MULTI_START, MULTI_END = "2024-05-06", "2024-06-28"


@pytest.mark.parametrize("fq_tag", ["none", "post"])
def test_price_daily_multi(fq_tag, snap, clickhouse_auth):
    """多标的 get_price vs 聚宽 panel=False 长表,逐字段严格比对。"""
    fq = None if fq_tag == "none" else fq_tag
    loc = local_price(MULTI_CODES, start_date=MULTI_START, end_date=MULTI_END,
                      frequency="daily", fields=FIELDS, fq=fq)
    live = snap.get(
        f"price-multi-{fq_tag}",
        (tuple(MULTI_CODES), MULTI_START, MULTI_END, fq_tag, tuple(FIELDS)),
        lambda: jqdatasdk.get_price(MULTI_CODES, start_date=MULTI_START, end_date=MULTI_END,
                                    frequency="daily", fields=FIELDS, fq=fq,
                                    panel=False, skip_paused=False),
    )
    ok, msgs = df_compare(loc, live, f"multi {fq_tag}", keys=["code", "time"])
    assert ok, " | ".join(msgs)


def test_price_single_vs_list_local(clickhouse_auth):
    """纯本地(0 配额):单 str 窄表与单元素 list 长表数值一致(仅形状不同)。"""
    code = "600519.XSHG"
    kw = dict(start_date=MULTI_START, end_date=MULTI_END, frequency="daily",
              fields=["open", "close", "volume"], fq="post")
    narrow = local_price(code, **kw)
    long = local_price([code], **kw)
    sub = long[long["code"] == code][["open", "close", "volume"]].reset_index(drop=True)
    assert len(narrow) == len(sub), f"行数 narrow={len(narrow)} list={len(sub)}"
    assert (narrow.to_numpy() == sub.to_numpy()).all(), "单 str 与单元素 list 数值不一致"
