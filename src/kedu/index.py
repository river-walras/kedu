"""本地复刻聚宽指数 API:get_index_stocks / get_index_weights / get_index_valuation。

数据源为 ClickHouse(由 scripts/backfill_index.py 同步):
- jqdata.index_member_history  指数成分纳入/剔除区间(逐日/二分 walk 折叠;带 position 保留返回序)
- jqdata.index_weights         月度成分权重快照(逐月扫描;带 position)
- jqdata.index_valuation       指数估值日序(聚宽仅 9 指数)

指数列表 get_all_securities(['index']) 由 kedu.securities 读 securities 表(type='index')。
指数日线 get_price(index, ...) 由 kedu.prices 读 bar_1d(指数 factor≡1)。

读侧 FINAL 约定(与写侧维护方式对应):
- index_member_history:由 staging 折叠后 TRUNCATE+reload,无重复版本 → 不加 FINAL。
- index_weights / index_valuation:逐月/逐日增量重插,可能有待合并版本 → 加 FINAL(表小)。
区间「某日活跃」语义同 industry:start_date <= d AND (end_date IS NULL OR end_date >= d)。
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import pandas as pd

from .db import DATABASE, get_client

# get_index_valuation 估值字段(对齐 reference/index/指数估值.md;与 stock valuation 模型不同)。
INDEX_VAL_FIELDS = [
    "pe_ratio", "turnover_ratio", "pb_ratio", "ps_ratio", "pcf_ratio", "capitalization",
    "market_cap", "circulating_cap", "circulating_market_cap", "pe_ratio_lyr", "pcf_ratio2",
    "dividend_ratio", "free_cap", "free_market_cap", "a_cap", "a_market_cap",
]

# 聚宽 get_index_valuation 目前支持的指数(reference/index/指数估值.md)。
INDEX_VAL_SECURITIES = [
    "000001.XSHG", "000016.XSHG", "000300.XSHG", "000905.XSHG", "000852.XSHG",
    "000688.XSHG", "399001.XSHE", "399006.XSHE", "000510.XSHG",
]


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
    """北京时区今天(date=None 时的查询锚点,与聚宽 today 语义一致)。"""
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date()


def _q(s: object) -> str:
    """转义为 SQL 单引号字符串字面量。"""
    return "'" + str(s).replace("'", "\\'") + "'"


def _active(iso: str) -> str:
    """区间某日活跃谓词。"""
    return f"start_date <= '{iso}' AND (end_date IS NULL OR end_date >= '{iso}')"


def get_index_stocks(index_symbol: str, date: str | dt.date | None = None) -> list[str]:
    """获取某指数在给定日期的成分股,复刻 jqdatasdk.get_index_stocks。返回股票代码 list。

    顺序对齐聚宽返回序(ORDER BY position),date 默认北京今天。
    """
    cli = get_client()
    iso = (_to_date(date) or _today()).isoformat()
    sql = (f"SELECT stock FROM {DATABASE}.index_member_history "
           f"WHERE index_code = {_q(index_symbol)} AND {_active(iso)} "
           f"ORDER BY position, stock")
    return [r[0] for r in cli.query(sql).result_rows]


def get_index_weights(index_id: str, date: str | dt.date | None = None) -> pd.DataFrame:
    """获取某指数成分权重,复刻 jqdatasdk.get_index_weights。

    返回 DataFrame:index 为股票代码,列为 date(披露日)/weight(权重)/display_name(名称)。
    无当日数据时返回「距离查询日期最近」披露日的快照(等距取较晚披露日);date 默认北京今天。
    """
    cli = get_client()
    iso = (_to_date(date) or _today()).isoformat()
    # 选最近披露日:绝对天数差最小,等距 tie-break 取较晚披露日。
    pick = cli.query(
        f"SELECT weight_date FROM {DATABASE}.index_weights FINAL "
        f"WHERE index_code = {_q(index_id)} "
        f"ORDER BY abs(dateDiff('day', weight_date, toDate('{iso}'))), weight_date DESC LIMIT 1"
    ).result_rows
    cols = ["date", "weight", "display_name"]
    if not pick:
        return pd.DataFrame(columns=cols)
    wd = pick[0][0].isoformat()
    rows = cli.query(
        f"SELECT code, weight_date, weight, display_name FROM {DATABASE}.index_weights FINAL "
        f"WHERE index_code = {_q(index_id)} AND weight_date = '{wd}' "
        f"ORDER BY position, code"
    ).result_rows
    df = pd.DataFrame(rows, columns=["code", "date", "weight", "display_name"]).set_index("code")
    df.index = df.index.astype(object)
    df.index.name = None
    df["date"] = pd.to_datetime(df["date"]).astype("datetime64[ns]")
    df["display_name"] = df["display_name"].astype(object)
    return df[cols]


def get_index_valuation(security_list: str | Sequence[str],
                        start_date: str | dt.date | None = None,
                        end_date: str | dt.date | None = None,
                        fields: Sequence[str] | None = None,
                        count: int | None = None) -> pd.DataFrame:
    """获取指数估值数据,复刻 jqdatasdk.get_index_valuation(聚宽仅支持 9 指数)。

    security_list 单 str 或 list;fields 默认 16 字段全集(含 code/day 自动去重);
    count 与 start_date 互斥(count=每指数 end_date 前 count 个交易日)。
    返回长表,整数索引,列为 code、day 与请求字段。
    """
    if start_date is not None and count is not None:   # 对齐聚宽:start_date 不能与 count 共用
        raise ValueError("start_date 不能与 count 共用")
    cli = get_client()
    codes = [security_list] if isinstance(security_list, str) else list(security_list)
    if fields is None:
        flds = list(INDEX_VAL_FIELDS)
    else:
        flds = [f for f in fields if f not in ("code", "day")]  # 去重 meta,避免重复列
    out_cols = ["code", "day", *flds]
    if not codes:
        return pd.DataFrame(columns=out_cols)

    inlist = ", ".join(_q(c) for c in codes)
    where = [f"code IN ({inlist})"]
    if end_date is not None:
        where.append(f"day <= toDate('{_to_date(end_date).isoformat()}')")
    if count is None:
        if start_date is not None:
            where.append(f"day >= toDate('{_to_date(start_date).isoformat()}')")
        order_lim = "ORDER BY code, day"
    else:
        order_lim = f"ORDER BY code, day DESC LIMIT {int(count)} BY code"

    sel = ", ".join(["code", "day", *(f"`{f}`" for f in flds)])
    sql = (f"SELECT {sel} FROM {DATABASE}.index_valuation FINAL "
           f"WHERE {' AND '.join(where)} {order_lim}")
    rows = cli.query(sql).result_rows
    df = pd.DataFrame(rows, columns=out_cols)
    if df.empty:
        return df
    if count is not None:                         # count 路径取了 DESC,还原升序
        df = df.sort_values(["code", "day"]).reset_index(drop=True)
    df["code"] = df["code"].astype(object)
    df["day"] = pd.to_datetime(df["day"]).astype("datetime64[ns]")
    return df
