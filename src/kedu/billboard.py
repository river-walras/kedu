"""本地复刻聚宽 get_billboard_list（龙虎榜数据）。

数据源为 ClickHouse `jqdata.billboard`，由 scripts/backfill_billboard.py 按交易日
从 live get_billboard_list(stock_list=None) 拉取。读侧按 day DESC、_position ASC
还原 live 行序，_position 不对外返回。
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from .calendar import get_trade_days
from .db import DATABASE, get_client, query_df

BILLBOARD_COLUMNS = [
    "code",
    "day",
    "direction",
    "rank",
    "abnormal_code",
    "abnormal_name",
    "sales_depart_name",
    "buy_value",
    "buy_rate",
    "sell_value",
    "sell_rate",
    "total_value",
    "net_value",
    "amount",
]
_STRING_COLUMNS = ["code", "direction", "abnormal_name", "sales_depart_name"]
_INT_COLUMNS = ["rank", "abnormal_code"]
_FLOAT_COLUMNS = [
    "buy_value",
    "buy_rate",
    "sell_value",
    "sell_rate",
    "total_value",
    "net_value",
    "amount",
]


def _direction_key(direction: object, one_active_code: bool) -> int:
    """stock_list 多元素查询的服务端方向排序。"""
    if direction == "ALL":
        return 0
    if one_active_code:
        return {"BUY": 1, "SELL": 2}.get(direction, 3)
    return {"SELL": 1, "BUY": 2}.get(direction, 3)


def _direction_key_sell_first(direction: object) -> int:
    """跨日单票查询的服务端方向排序。"""
    return {"SELL": 0, "BUY": 1, "ALL": 2}.get(direction, 3)


def _to_date(x: str | dt.date | None) -> dt.date | None:
    """将日期类输入转换为 datetime.date，None 原样返回。"""
    if x is None:
        return None
    if isinstance(x, dt.datetime):
        return x.date()
    if isinstance(x, dt.date):
        return x
    return pd.Timestamp(x).date()


def _q(value: object) -> str:
    """转义为 SQL 单引号字符串字面量。"""
    return "'" + str(value).replace("'", "\\'") + "'"


def _empty() -> pd.DataFrame:
    """空结果 DataFrame，列序固定。"""
    data: dict[str, pd.Series] = {
        "code": pd.Series([], dtype=object),
        "day": pd.Series([], dtype=object),
        "direction": pd.Series([], dtype=object),
        "rank": pd.Series([], dtype="int64"),
        "abnormal_code": pd.Series([], dtype="int64"),
        "abnormal_name": pd.Series([], dtype=object),
        "sales_depart_name": pd.Series([], dtype=object),
    }
    for c in _FLOAT_COLUMNS:
        data[c] = pd.Series([], dtype="float64")
    return pd.DataFrame(data, columns=BILLBOARD_COLUMNS)


def _table_exists(client) -> bool:
    """billboard 表是否存在。"""
    return bool(client.query(f"EXISTS TABLE {DATABASE}.billboard").result_rows[0][0])


def _covered_end(client) -> dt.date | None:
    """本地 billboard 已覆盖的最大日期；空表返回 None。"""
    if not _table_exists(client):
        return None
    cnt, mx = client.query(
        f"SELECT count(), max(day) FROM {DATABASE}.billboard"
    ).result_rows[0]
    return mx if cnt else None


def _window(
    client, start_date, end_date, count: int | None
) -> tuple[dt.date, dt.date] | None:
    """解析龙虎榜日期窗口。count 按交易日往前取，非交易日 end_date 自动回退。"""
    if count is not None and count <= 0:
        raise Exception("get_billboard_list 必须指定 start_date 或 count 之一")
    if start_date is None and count is None:
        raise Exception("get_billboard_list 必须指定 start_date 或 count 之一")
    if start_date is not None and count is not None:
        raise Exception("get_billboard_list 不能同时指定 start_date 和 count 两个参数")

    if count is not None:
        days = list(get_trade_days(end_date=_to_date(end_date), count=count))
        if not days:
            return None
        return days[0], days[-1]

    start = _to_date(start_date)
    end = _to_date(end_date)
    if end is None:
        end = _covered_end(client)
        if end is None:
            return None
    if end < start:
        return None
    return start, end


def get_billboard_list(
    stock_list=None, start_date=None, end_date=None, count=None
) -> pd.DataFrame:
    """获取指定日期区间内的龙虎榜数据，复刻 jqdatasdk.get_billboard_list。

    stock_list=None 返回全市场；str 与 list 均支持；空 list 返回固定列空 DataFrame。
    day 列输出 Python datetime.date object，行序按 live 的日期倒序与日内原始顺序。
    """
    cli = get_client()
    window = _window(cli, start_date, end_date, count)
    if window is None:
        return _empty()
    start, end = window

    if stock_list is None:
        codes = None
    else:
        codes = [stock_list] if isinstance(stock_list, str) else list(stock_list)
        if not codes:
            return _empty()

    if not _table_exists(cli):
        return _empty()

    where = [
        f"day >= toDate('{start.isoformat()}')",
        f"day <= toDate('{end.isoformat()}')",
    ]
    if codes is not None:
        where.append("code IN (" + ", ".join(_q(c) for c in codes) + ")")
    raw = query_df(
        cli,
        f"""
        SELECT {", ".join(BILLBOARD_COLUMNS)}, _position
        FROM {DATABASE}.billboard
        WHERE {" AND ".join(where)}
        ORDER BY day DESC, _position ASC
    """,
    )
    if raw.empty:
        return _empty()

    out = raw[BILLBOARD_COLUMNS].copy()
    out["day"] = pd.to_datetime(out["day"]).dt.date
    for c in _STRING_COLUMNS:
        out[c] = out[c].astype(object)
    for c in _INT_COLUMNS:
        out[c] = pd.to_numeric(out[c], errors="raise").astype("int64")
    for c in _FLOAT_COLUMNS:
        out[c] = pd.to_numeric(out[c], errors="coerce").astype("float64")

    # live 对 stock_list=list 且长度>1的结果会重新排序,不同于单字符串/全市场的
    # 原始 _position 顺序。按探针复刻:日期倒序,股票按其全市场位置,单日只命中一票时
    # ALL -> BUY/SELL rank 升序;命中多票时 ALL -> SELL/BUY rank 降序。
    if codes is not None and len(codes) > 1:
        work = out.copy()
        work["_position"] = pd.to_numeric(raw["_position"], errors="raise").astype(
            "int64"
        )
        active_count = work.groupby("day")["code"].transform("nunique")
        one_active = active_count == 1
        code_pos = work.groupby(["day", "code"])["_position"].transform("min")
        work["_code_pos"] = code_pos
        work["_direction_pos"] = [
            _direction_key(direction, bool(is_one))
            for direction, is_one in zip(work["direction"], one_active, strict=True)
        ]
        work["_group_pos"] = [
            0 if direction == "ALL" else 1 for direction in work["direction"]
        ]
        work["_rank_pos"] = [
            int(rank) if is_one else -int(rank)
            for rank, is_one in zip(work["rank"], one_active, strict=True)
        ]
        work["_day_sort"] = pd.to_datetime(work["day"])
        out = work.sort_values(
            [
                "_day_sort",
                "_code_pos",
                "_group_pos",
                "_rank_pos",
                "_direction_pos",
                "_position",
            ],
            ascending=[False, True, True, True, True, True],
            kind="stable",
        ).reset_index(drop=True)[BILLBOARD_COLUMNS]
    elif codes is not None and isinstance(stock_list, str) and start < end:
        work = out.copy()
        work["_position"] = pd.to_numeric(raw["_position"], errors="raise").astype(
            "int64"
        )
        work["_group_pos"] = [
            1 if direction == "ALL" else 0 for direction in work["direction"]
        ]
        work["_rank_pos"] = [-int(rank) for rank in work["rank"]]
        work["_direction_pos"] = [
            _direction_key_sell_first(direction) for direction in work["direction"]
        ]
        work["_day_sort"] = pd.to_datetime(work["day"])
        out = work.sort_values(
            ["_day_sort", "_group_pos", "_rank_pos", "_direction_pos", "_position"],
            ascending=[False, True, True, True, True],
            kind="stable",
        ).reset_index(drop=True)[BILLBOARD_COLUMNS]
    return out.reset_index(drop=True)[BILLBOARD_COLUMNS]
