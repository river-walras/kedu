"""本地复刻聚宽 get_money_flow_pro（日频资金流向）。

数据源为 ClickHouse `jqdata.money_flow_pro`，由 scripts/backfill_money_flow.py
从 live get_money_flow_pro 拉取。仅支持 daily/1d；分钟资金流向属于聚宽付费模块，
本地没有采购数据时直接按 live 口径 fail-fast。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import pandas as pd

from .calendar import _today_cn, get_trade_days
from .db import DATABASE, get_client, query_df, quote_ident

BASE_FIELDS = [
    "inflow_xl",
    "inflow_l",
    "inflow_m",
    "inflow_s",
    "outflow_xl",
    "outflow_l",
    "outflow_m",
    "outflow_s",
]
NETFLOW_FIELDS = ["netflow_xl", "netflow_l", "netflow_m", "netflow_s"]
ALL_FIELDS = [*BASE_FIELDS, *NETFLOW_FIELDS]
DATA_TYPES = ("money", "volume", "deal")

_DAILY = {"daily", "1d"}
_MINUTE = {"minute", "minutes", "1m", "min"}
_MINUTE_MESSAGE = (
    "get_money_flow_pro(资金流向历史分钟数据) 属于付费模块，如果您有购买需求，请联系聚宽JQData运营人员：\n"
    "https://www.joinquant.com/help/api/doc?name=logon&id=9831"
)


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


def _raise_params_error(message: str) -> None:
    """优先抛 jqdatasdk.utils.ParamsError；运行时无 jqdatasdk 时退化为普通 Exception。"""
    try:
        from jqdatasdk.utils import ParamsError
    except Exception:  # noqa: BLE001
        raise Exception(message) from None
    raise ParamsError(message)


def _normalize_fields(fields: str | Sequence[str] | None) -> list[str]:
    """规范化 fields，None 返回 live 默认的 8 个流入/流出基础字段。"""
    if fields is None:
        return list(BASE_FIELDS)
    out = [fields] if isinstance(fields, str) else list(fields)
    bad = [f for f in out if f not in ALL_FIELDS]
    if bad:
        raise Exception(f"fields 包含不支持的字段: {bad}")
    return out


def _empty(fields: Sequence[str]) -> pd.DataFrame:
    """空结果 DataFrame，列序与 dtype 对齐 live 的主要非空口径。"""
    data: dict[str, pd.Series] = {
        "time": pd.Series([], dtype="datetime64[ns]"),
        "code": pd.Series([], dtype=object),
    }
    for f in fields:
        data[f] = pd.Series([], dtype="float64")
    return pd.DataFrame(data, columns=["time", "code", *fields])


def _to_datetime_ns(series: pd.Series) -> pd.Series:
    """ClickHouse DateTime via Arrow may arrive as Unix seconds; normalize to pandas ns."""
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_datetime(series, unit="s").astype("datetime64[ns]")
    return pd.to_datetime(series).astype("datetime64[ns]")


def _date_window(
    start_date, end_date, count: int | None
) -> tuple[dt.date, dt.date] | None:
    """解析 daily 窗口。count 按交易日从 end_date 往前取，非交易日自动回退。"""
    if start_date is not None and count is not None:
        _raise_params_error("(start_date, count) only one param is required")
    if count is not None and count <= 0:
        raise Exception("count 参数需要大于 0 或者为 None")

    end = _to_date(end_date) or _today_cn()
    if count is not None:
        days = list(get_trade_days(end_date=end, count=count))
        if not days:
            return None
        return days[0], days[-1]

    start = _to_date(start_date) or dt.date(2015, 1, 1)
    if end < start:
        return None
    return start, end


def get_money_flow_pro(
    security_list,
    start_date=None,
    end_date=None,
    frequency="daily",
    fields=None,
    count=None,
    data_type="money",
) -> pd.DataFrame:
    """获取股票日频资金流向，复刻 jqdatasdk.get_money_flow_pro 的日频子集。

    security_list 可为单代码 str 或代码 list；返回列序为 time、code、fields。
    fields=None 时仅返回 8 个流入/流出字段；显式传 fields 时保留传入顺序。
    """
    if frequency in _MINUTE:
        raise Exception(_MINUTE_MESSAGE)
    if frequency not in _DAILY:
        raise Exception("get_money_flow_pro 仅支持 daily/1d；分钟资金流向未采购")
    if data_type not in DATA_TYPES:
        raise Exception("data_type 只能是 ('money', 'volume', 'deal') 中的一个")

    flds = _normalize_fields(fields)
    codes = [security_list] if isinstance(security_list, str) else list(security_list)
    if not codes:
        return _empty(flds)

    window = _date_window(start_date, end_date, count)
    if window is None:
        return _empty(flds)
    start, end = window

    inlist = ", ".join(_q(c) for c in codes)
    sel = ", ".join(["time", "code", *(quote_ident(f) for f in flds)])
    sql = f"""
        SELECT {sel}
        FROM {DATABASE}.money_flow_pro FINAL
        WHERE code IN ({inlist})
          AND data_type = {_q(data_type)}
          AND time >= toDateTime('{start.isoformat()}')
          AND time < toDateTime('{(end + dt.timedelta(days=1)).isoformat()}')
        ORDER BY time, code
    """
    raw = query_df(get_client(), sql)
    if raw.empty:
        return _empty(flds)

    out = pd.DataFrame(
        {
            "time": _to_datetime_ns(raw["time"]),
            "code": raw["code"].astype(object),
        }
    )
    for f in flds:
        out[f] = pd.to_numeric(raw[f], errors="coerce").astype("float64")
    return out[["time", "code", *flds]].reset_index(drop=True)
