"""本地证券列表, 复刻聚宽 get_all_securities(types=[], date=None).

数据源为 ClickHouse `jqdata.securities`(股票 + 指数, type 区分), 由 update_jqdata.update_securities 同步.
返回 DataFrame, index 为 code, 列为 display_name, name, start_date, end_date, type.
与 jqdatasdk 逐项一致, start_date/end_date 为 datetime64[ns], 未退市哨兵为 2200-01-01.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import pandas as pd

from .db import DATABASE, get_client

_COLS = ["display_name", "name", "start_date", "end_date", "type"]


def _to_date(x: str | dt.date | None) -> dt.date | None:
    """将日期类输入转换为 datetime.date."""
    if x is None:
        return None
    if isinstance(x, dt.datetime):
        return x.date()
    if isinstance(x, dt.date):
        return x
    return pd.Timestamp(x).date()


def get_all_securities(types: Sequence[str] | str = [],  # noqa: B006  只读不改, 对齐聚宽默认 []
                       date: str | dt.date | None = None) -> pd.DataFrame:
    """获取证券列表.

    types 为空时仅返回股票, 对齐聚宽语义. types 非空时按 type 过滤,
    本地仅有股票, 其它类型返回空. date 给定时仅返回该日仍在上市的证券.
    """
    cli = get_client()
    where = []
    if types:
        tlist = [types] if isinstance(types, str) else list(types)
        quoted = ", ".join("'" + str(t).replace("'", "\\'") + "'" for t in tlist)
        where.append(f"type IN ({quoted})")
    else:
        where.append("type = 'stock'")
    d = _to_date(date)
    if d is not None:
        iso = d.isoformat()
        where.append(f"start_date <= '{iso}' AND end_date >= '{iso}'")

    sql = (f"SELECT instrument_id, display_name, name, start_date, end_date, type "
           f"FROM {DATABASE}.securities WHERE {' AND '.join(where)} ORDER BY instrument_id")
    rows = cli.query(sql).result_rows
    df = pd.DataFrame(rows, columns=["instrument_id", *_COLS]).set_index("instrument_id")
    df.index = df.index.astype(object)
    df.index.name = None
    # 对齐聚宽 dtype:字符串列 object,日期列 datetime64[ns]
    for c in ("display_name", "name", "type"):
        df[c] = df[c].astype(object)
    df["start_date"] = pd.to_datetime(df["start_date"]).astype("datetime64[ns]")
    df["end_date"] = pd.to_datetime(df["end_date"]).astype("datetime64[ns]")
    return df[_COLS]
