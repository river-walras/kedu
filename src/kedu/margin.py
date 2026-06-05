"""本地复刻聚宽融资融券 API:get_mtss / get_margincash_stocks / get_marginsec_stocks。

数据源为 ClickHouse(由 scripts/backfill_margin.py 同步):
- jqdata.mtss                  逐股融资融券明细(sec_code, date, 7 个金额/数量字段)
- jqdata.margin_target_history 融资('cash')/融券('sec')标的列表折叠区间(逐日 walk 折叠)
- jqdata.margin_target_raw     标的列表逐日快照 staging(get_*_stocks(date=None) 的「最近披露日」锚点)

区间「某日活跃」语义同 index/industry:start_date <= d AND (end_date IS NULL OR end_date >= d)。
get_*_stocks(date=None) 的「最近一次披露」服务端语义,本地锚到 staging 的 max(date)
(折叠表开区间不记录覆盖到哪天)。
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import pandas as pd

from .db import DATABASE, get_client

# get_mtss 完整字段集与列序(对齐 reference/margin/融资融券信息.md 与 live 探针)。
MTSS_FIELDS = [
    "date", "sec_code", "fin_value", "fin_buy_value", "fin_refund_value",
    "sec_value", "sec_sell_value", "sec_refund_value", "fin_sec_value",
]
_MTSS_FLOAT = {f for f in MTSS_FIELDS if f not in ("date", "sec_code")}


def _to_date(x: str | dt.date | None) -> dt.date | None:
    """将日期类输入转换为 datetime.date。"""
    if x is None:
        return None
    if isinstance(x, dt.datetime):
        return x.date()
    if isinstance(x, dt.date):
        return x
    return pd.Timestamp(x).date()


def _today() -> dt.date:
    """北京时区今天(end_date 默认 today,与聚宽一致)。"""
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()


def _q(s: object) -> str:
    """转义为 SQL 单引号字符串字面量。"""
    return "'" + str(s).replace("'", "\\'") + "'"


def _active(iso: str) -> str:
    """区间某日活跃谓词。"""
    return f"start_date <= '{iso}' AND (end_date IS NULL OR end_date >= '{iso}')"


# ---------------------------------------------------------------------------
# 融资/融券标的列表
# ---------------------------------------------------------------------------
def _latest_disclose(cli, kind: str) -> dt.date | None:
    """staging margin_target_raw 中该 dataset 已 walk 到的最新披露日(空表返回 None)。

    ClickHouse 对**空表**的 max(非空 Date 列) 返回 1970-01-01 而非 NULL,故先 count 判空。
    """
    cnt, mx = cli.query(
        f"SELECT count(), max(date) FROM {DATABASE}.margin_target_raw WHERE type = {_q(kind)}"
    ).result_rows[0]
    return mx if cnt else None


def _targets(kind: str, date: str | dt.date | None) -> list[str]:
    """某 dataset(kind ∈ {'cash','sec'})在给定日期的标的列表(升序)。

    date=None 锚到 staging 最近披露日;无数据返回 []。
    """
    cli = get_client()
    if date is None:
        anchor = _latest_disclose(cli, kind)
        if anchor is None:
            return []
        iso = anchor.isoformat()
    else:
        iso = _to_date(date).isoformat()
    rows = cli.query(
        f"SELECT stock FROM {DATABASE}.margin_target_history "
        f"WHERE type = {_q(kind)} AND {_active(iso)} ORDER BY stock"
    ).result_rows
    return [r[0] for r in rows]


def get_margincash_stocks(date: str | dt.date | None = None) -> list[str]:
    """获取指定日期的融资标的列表,复刻 jqdatasdk.get_margincash_stocks。

    date 默认 None,返回上交所、深交所最近一次披露的可融资标的列表(按代码升序)。
    """
    return _targets("cash", date)


def get_marginsec_stocks(date: str | dt.date | None = None) -> list[str]:
    """获取指定日期的融券标的列表,复刻 jqdatasdk.get_marginsec_stocks。

    date 默认 None,返回上交所、深交所最近一次披露的可融券标的列表(按代码升序)。
    """
    return _targets("sec", date)


# ---------------------------------------------------------------------------
# 逐股融资融券明细
# ---------------------------------------------------------------------------
def get_mtss(security_list: str | Sequence[str], start_date: str | dt.date | None = None,
             end_date: str | dt.date | None = None,
             fields: str | Sequence[str] | None = None,
             count: int | None = None) -> pd.DataFrame:
    """获取一只或多只股票在一段时间内的融资融券信息,复刻 jqdatasdk.get_mtss。

    start_date 与 count 二选一(恰好其一,照搬聚宽源码 assert);end_date 默认今天;
    fields 字段名或 list,默认全字段。返回 DataFrame,整数索引,
    行序 = security_list 入参顺序、组内按 date 升序(对齐聚宽)。
    """
    # 照搬聚宽源码:assert (not start_date) ^ (not count) —— 恰好二选一(都缺/都给都报错)
    assert (not start_date) ^ (not count), "(start_date, count) only one param is required"

    cli = get_client()
    codes = [security_list] if isinstance(security_list, str) else list(security_list)
    if fields is None:
        flds = list(MTSS_FIELDS)
    else:
        flds = [fields] if isinstance(fields, str) else list(fields)
    if not codes:
        return pd.DataFrame(columns=flds)

    end_iso = (_to_date(end_date) or _today()).isoformat()
    need = list(dict.fromkeys(["sec_code", "date", *flds]))  # 排序需 sec_code/date,去重保序
    sel = ", ".join(f"`{c}`" for c in need)
    inlist = ", ".join(_q(c) for c in codes)
    where = [f"sec_code IN ({inlist})", f"date <= toDate('{end_iso}')"]
    if count is None:
        where.append(f"date >= toDate('{_to_date(start_date).isoformat()}')")
        order_lim = "ORDER BY sec_code, date"
    else:
        # 每票取 end_date 前 count 行(DESC + LIMIT BY),下方再升序还原
        order_lim = f"ORDER BY sec_code, date DESC LIMIT {int(count)} BY sec_code"
    sql = f"SELECT {sel} FROM {DATABASE}.mtss WHERE {' AND '.join(where)} {order_lim}"
    rows = cli.query(sql).result_rows
    df = pd.DataFrame(rows, columns=need)
    if df.empty:
        return pd.DataFrame(columns=flds)

    # 行序还原:按 security_list 入参序、组内 date 升序
    df["sec_code"] = pd.Categorical(df["sec_code"], categories=codes, ordered=True)
    df = df.sort_values(["sec_code", "date"]).reset_index(drop=True)
    df["sec_code"] = df["sec_code"].astype(object)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"]).astype("datetime64[ns]")
    for c in _MTSS_FLOAT:
        if c in df.columns:
            df[c] = df[c].astype("float64")
    return df[flds]
