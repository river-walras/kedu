"""本地证券列表, 复刻聚宽 get_all_securities(types=[], date=None).

数据源为 ClickHouse `jqdata.securities`(股票 + 指数 + 场内基金, type 区分), 由
update_jqdata.update_securities 同步. 返回 DataFrame, index 为 code, 列为
display_name, name, start_date, end_date, type. 与 jqdatasdk 逐项一致,
start_date/end_date 为 datetime64[ns], 未退市哨兵为 2200-01-01.

类型语义对齐聚宽(实测 P0b):
- types=[] 仅返回股票.
- 伞型 'fund'(场内基金)展开为细分类型 etf/lof/fja/fjb/reits/fjm/mmf.
- 细分类型 etf/lof/fja/fjb 可直接作参数;mmf/reits/fjm **不可**作参数(聚宽报错),
  只能经 ['fund'] 返回后按 type 过滤 —— 本地同样对其报错以保持一致.
- 本地仅有股票/指数/场内基金;场外(open_fund 等合法类型)无数据返回空(scope 限定).
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import pandas as pd

from .db import DATABASE, get_client
from .finance_schema import FUND_ONEXCHANGE_TYPES

_COLS = ["display_name", "name", "start_date", "end_date", "type"]

# 聚宽 get_all_securities 合法的 types 取值(实测 P0b 错误信息原文顺序)。传入此集合之外的
# 类型(如 mmf/reits/fjm)聚宽直接报错 —— 本地复刻同款校验。
_VALID_TYPES = ("stock", "index", "fund", "futures", "etf", "lof", "fja", "fjb",
                "open_fund", "bond_fund", "stock_fund", "QDII_fund", "money_market_fund",
                "mixture_fund", "options", "conbond", "bjse", "csi", "spi")


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
        for t in tlist:
            if t not in _VALID_TYPES:   # 复刻聚宽校验(mmf/reits/fjm 等非法参数报错)
                raise Exception(
                    f"存在无效的标的类型(={t})， types应该是{_VALID_TYPES}中的一个或者多个组成的列表")
        # 伞型 'fund' 展开为场内细分类型;其余合法类型按字面过滤(场外类型本地无数据→空)。
        concrete: list[str] = []
        for t in tlist:
            concrete.extend(FUND_ONEXCHANGE_TYPES if t == "fund" else [t])
        quoted = ", ".join("'" + str(t).replace("'", "\\'") + "'" for t in dict.fromkeys(concrete))
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
