"""本地复刻聚宽 get_valuation(市值表按标的+时间切片)。

数据源为 ClickHouse `jqdata.stock_valuation`，与 get_fundamentals(query(valuation), date=d)
的横截面切片同源、成员资格一致，由 scripts/backfill_jq.backfill_valuation +
scripts/update_jqdata.py step 3 持续维护，无需为本 API 另建表或另写回补脚本。

与聚宽对齐的输出口径(经 live 探针确认):
- 列序恒为 code、day，随后是请求 fields(fields=None 时为 valuation 模型全列序)。
- code 为 str(object)；day 为 datetime.date(object，非 datetime64)；数值列 float64。
- 行序 ORDER BY (day, code)(单票即按 day 升序)；整数索引。
- count 表示每标的自 end_date 往前取 count 个交易日；start_date 与 count 互斥。
  已实证 stock_valuation 无停牌空洞(万科停牌期逐日仍有估值行)，故 count 用
  `LIMIT count BY code` 与文档"停牌减少行数"等价(次新股 count 超其历史时两法都返回更少)。
- 本地不施加聚宽每次最多 10000 行的截断(与本项目一贯口径一致)。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence

import pandas as pd

from ._jqsdk import valuation
from .calendar import _today_cn
from .db import DATABASE, get_client, query_df, quote_ident
from .schema import data_columns

# valuation 模型全列序(fields=None 默认返回的字段集与顺序)。
VALUATION_FIELDS = data_columns(valuation)
_ALLOWED = {*VALUATION_FIELDS, "code", "day"}
_META = ("code", "day")


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
    """规范化 fields，剔除 code/day(它们恒被前置)，None 返回 valuation 全列序。

    非白名单字段抛错;保留传入顺序、去重。
    """
    if fields is None:
        return list(VALUATION_FIELDS)
    raw = [fields] if isinstance(fields, str) else list(fields)
    bad = [f for f in raw if f not in _ALLOWED]
    if bad:
        raise Exception(f"get_valuation fields 包含不支持的字段: {bad}")
    out: list[str] = []
    for f in raw:
        if f in _META or f in out:
            continue  # code/day 恒前置;去重保序
        out.append(f)
    return out


def _empty(fields: Sequence[str]) -> pd.DataFrame:
    """空结果 DataFrame，列序 [code, day, *fields]，dtype 对齐非空口径。"""
    data: dict[str, pd.Series] = {
        "code": pd.Series([], dtype=object),
        "day": pd.Series([], dtype=object),
    }
    for f in fields:
        data[f] = pd.Series([], dtype="float64")
    return pd.DataFrame(data, columns=["code", "day", *fields])


def get_valuation(
    security_list,
    start_date=None,
    end_date=None,
    fields=None,
    count=None,
) -> pd.DataFrame:
    """获取多个标的在指定交易日范围内的市值表数据，复刻 jqdatasdk.get_valuation。

    security_list 可为单代码 str 或代码 list；返回列序为 code、day、fields。
    fields=None 返回 valuation 全字段；start_date 不能与 count 共用。本地不截断 10000 行。
    """
    if start_date is not None and count is not None:
        _raise_params_error("(start_date, count) only one param is required")
    if count is not None and count <= 0:
        raise Exception("count 参数需要大于 0 或者为 None")

    flds = _normalize_fields(fields)
    codes = [security_list] if isinstance(security_list, str) else list(security_list)
    if not codes:
        return _empty(flds)

    inlist = ", ".join(_q(c) for c in codes)
    sel = ", ".join(["code", "day", *(quote_ident(f) for f in flds)])
    where = [f"code IN ({inlist})"]
    end = _to_date(end_date) or _today_cn()
    where.append(f"day <= toDate('{end.isoformat()}')")

    if count is not None:
        # 每票各自取最近 count 个交易日(DESC + LIMIT ... BY code)，随后统一升序。
        order_lim = f"ORDER BY code, day DESC LIMIT {int(count)} BY code"
    else:
        start = _to_date(start_date) or dt.date(2005, 1, 1)
        where.append(f"day >= toDate('{start.isoformat()}')")
        order_lim = "ORDER BY code, day"

    sql = (
        f"SELECT {sel} FROM {DATABASE}.stock_valuation FINAL "
        f"WHERE {' AND '.join(where)} {order_lim}"
    )
    raw = query_df(get_client(), sql)
    if raw.empty:
        return _empty(flds)

    out = pd.DataFrame(
        {
            "code": raw["code"].astype(object),
            # day 输出 Python datetime.date object(对齐聚宽,非 datetime64)。
            "day": pd.to_datetime(raw["day"]).dt.date,
        }
    )
    for f in flds:
        out[f] = pd.to_numeric(raw[f], errors="coerce").astype("float64")
    # 聚宽 get_valuation 按 (day, code) 升序返回;count 路径取了 DESC，此处统一还原。
    out = out.sort_values(["day", "code"], kind="stable").reset_index(drop=True)
    return out[["code", "day", *flds]]
