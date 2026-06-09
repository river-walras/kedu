"""本地复刻聚宽 get_extras, 支持 info='is_st' 与基金净值(unit/acc/adj_net_value).

is_st: 数据源 ClickHouse `jqdata.is_st`(每交易日每标的 ST 状态布尔), 由
scripts/backfill_jq.sync_is_st 自聚宽 get_extras('is_st') 同步。涵盖股改 s、ST、*ST、
退市整理期(任一为 True), 是 get_price 字段白名单之外的独立接口数据。

基金净值: 数据源 `jqdata.fund_net_value`(FUND_NET_VALUE 表), 由 backfill_fund 同步。
  - unit_net_value -> net_value(单位净值)
  - acc_net_value  -> sum_value(累计净值)
  - adj_net_value  -> refactor_net_value(累计复权净值, **仅场外基金 .OF**, 复刻聚宽)
对齐聚宽(实测 P0c):index 为请求窗口内 A 股交易日(datetime);代码须带正确后缀
(裸码或错市场后缀 -> 找不到标的);缺失格留 NaN(净值为时点值, **不前向填充**)。

返回与 jqdatasdk 一致:df=True 返回 DataFrame, df=False 返回 dict{code: numpy.ndarray}。
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import pandas as pd

from .calendar import get_trade_days
from .db import DATABASE, get_client

# 基金净值 info -> fund_net_value 物理列
_NET_COL = {"unit_net_value": "net_value", "acc_net_value": "sum_value",
            "adj_net_value": "refactor_net_value"}


def get_extras(info: str, security_list: str | Sequence[str],
               start_date: str | dt.date | None = None,
               end_date: str | dt.date | None = None,
               df: bool = True, count: int | None = None) -> pd.DataFrame | dict:
    """获取标的的额外数据, 支持 info='is_st' 与基金净值(unit/acc/adj_net_value).

    - security_list 单代码 str 或代码 list, 列序保持输入顺序.
    - 交易日窗口语义同 get_trade_days: count 与 start_date 二选一, count 表示自
      end_date 往前 count 个交易日, end_date 缺省为今日.
    - df=True 返回 DataFrame(index=交易日, columns=代码);df=False 返回 dict{code: numpy.ndarray}.
    """
    if info in _NET_COL:
        return _fund_net_value(info, security_list, start_date, end_date, df, count)
    if info != "is_st":
        raise NotImplementedError(
            f"get_extras 暂支持 info='is_st' 与基金净值(unit/acc/adj_net_value), 不支持 {info!r}"
            "(futures_* 等本地无对应数据源)")
    codes = [security_list] if isinstance(security_list, str) else list(security_list)
    assert codes, "security_list is required"

    days = [d for d in get_trade_days(start_date=start_date, end_date=end_date, count=count)]
    idx = pd.DatetimeIndex(pd.to_datetime(days))
    if not days:
        wide = pd.DataFrame(index=idx, columns=codes, dtype=object)
        return wide if df else {c: wide[c].to_numpy() for c in codes}

    cli = get_client()
    d0, dn = days[0].isoformat(), days[-1].isoformat()
    inlist = ", ".join("'" + str(c).replace("'", "") + "'" for c in codes)
    rows = cli.query(
        f"SELECT instrument_id, date, is_st FROM {DATABASE}.is_st "
        f"WHERE instrument_id IN ({inlist}) "
        f"AND date >= toDate('{d0}') AND date <= toDate('{dn}')"
    ).result_rows
    # 每票在窗口起点之前的最近一条 is_st(用于给退市票/区间整体晚于停更的票播种前向填充)
    seed_rows = cli.query(
        f"SELECT instrument_id, argMax(is_st, date) FROM {DATABASE}.is_st "
        f"WHERE instrument_id IN ({inlist}) AND date < toDate('{d0}') "
        f"GROUP BY instrument_id"
    ).result_rows
    seed = pd.Series({r[0]: float(r[1]) for r in seed_rows}, dtype="float64").reindex(codes)

    raw = pd.DataFrame(rows, columns=["instrument_id", "date", "is_st"])
    if raw.empty:
        pivot = pd.DataFrame(index=idx, columns=codes, dtype="float64")
    else:
        raw["date"] = pd.to_datetime(raw["date"])
        pivot = (raw.pivot_table(index="date", columns="instrument_id", values="is_st")
                 .reindex(index=idx, columns=codes).astype("float64"))

    # 复刻聚宽 get_extras 在请求窗口内的口径(返回值恒为 bool、无 NaN):
    #   - 上市前 -> False;上市中 -> 实际值;退市后 -> 最后状态前向填充(非恒 True)。
    # 实现:用窗口起点前的最近值给首行播种 -> 前向填充(带过退市)-> 剩余(上市前)填 False。
    pivot.iloc[0] = pivot.iloc[0].fillna(seed).to_numpy()
    wide = pivot.ffill().fillna(0.0).astype(bool)
    wide.index.name = None
    wide.columns.name = None

    if df:
        return wide
    return {c: wide[c].to_numpy() for c in codes}


def _fund_net_value(info: str, security_list: str | Sequence[str],
                    start_date, end_date, df: bool, count: int | None):
    """基金净值(unit/acc/adj_net_value),自 fund_net_value 派生,对齐聚宽口径。

    - 代码须带后缀(裸码报「找不到标的」);裸码 = 去后缀,作 fund_net_value.code 查询键。
    - adj_net_value 仅场外基金(.OF),复刻聚宽限制。
    - index=窗口内 A 股交易日;缺失格 NaN,不前向填充(净值为时点值)。
    """
    col = _NET_COL[info]
    codes = [security_list] if isinstance(security_list, str) else list(security_list)
    assert codes, "security_list is required"

    if info == "adj_net_value":  # 复刻聚宽:复权净值仅场外基金
        if any(not str(c).upper().endswith(".OF") for c in codes):
            raise Exception("adj_net_value现在仅支持场外基金")

    bare_of: dict[str, str] = {}
    for c in codes:
        s = str(c)
        if "." not in s:  # 复刻聚宽:无后缀无法解析标的
            raise Exception(f"找不到标的{s}")
        bare_of[c] = s.rsplit(".", 1)[0]
    uniq_bares = list(dict.fromkeys(bare_of.values()))

    days = list(get_trade_days(start_date=start_date, end_date=end_date, count=count))
    idx = pd.DatetimeIndex(pd.to_datetime(days))
    cli = get_client()

    inlist = ", ".join("'" + b.replace("'", "") + "'" for b in uniq_bares)
    # 存在性校验(复刻「找不到标的」):库中完全无该裸码即报错(窗口内无数据则返回 NaN,不报错)。
    existing = {r[0] for r in cli.query(
        f"SELECT DISTINCT code FROM {DATABASE}.fund_net_value WHERE code IN ({inlist})").result_rows}
    for c in codes:
        if bare_of[c] not in existing:
            raise Exception(f"找不到标的{c}")

    if not days:
        wide = pd.DataFrame(index=idx, columns=codes, dtype="float64")
        return wide if df else {c: wide[c].to_numpy() for c in codes}

    d0, dn = days[0].isoformat(), days[-1].isoformat()
    rows = cli.query(
        f"SELECT code, day, {col} FROM {DATABASE}.fund_net_value "
        f"WHERE code IN ({inlist}) AND day >= toDate('{d0}') AND day <= toDate('{dn}')"
    ).result_rows
    raw = pd.DataFrame(rows, columns=["code", "day", col])
    if raw.empty:
        piv = pd.DataFrame(index=idx)
    else:
        raw["day"] = pd.to_datetime(raw["day"])
        piv = (raw.pivot_table(index="day", columns="code", values=col)
               .reindex(index=idx).astype("float64"))

    # 列按输入顺序映射裸码(允许重复原始码各自成列);缺失裸码 -> 全 NaN 列。不 ffill。
    series = []
    for c in codes:
        b = bare_of[c]
        series.append(piv[b] if b in piv.columns else pd.Series(index=idx, dtype="float64"))
    wide = pd.concat(series, axis=1)
    wide.columns = codes
    wide.index.name = None
    wide.columns.name = None

    if df:
        return wide
    return {c: wide[c].to_numpy() for c in codes}
