"""STK 表(finance.run_query 接口底表)统一同步:所有 stk_* 表走同一路径。

每张表幂等同步:
  - 表不存在 → 据聚宽模型列类型建表(CREATE IF NOT EXISTS);
  - 表为空   → 从 jqdatasdk 全量回补(自适应分块:初始大块,逼近 20 万上限即对半缩小并沿用);
  - 有数据   → 按 pub_date 近窗口增量(含 report_type=1 重述,ReplacingMergeTree 去重)。

覆盖财报三表 + 业绩预告/审计/预约披露/状态变动 + 市场汇总(成交概况/融资融券) +
上市公司基本信息族(含除权除息) + 基金族 10 张表(逻辑表名见 finance_schema.RUN_QUERY_TABLES)。
`--full` 强制全量(按年跳过已有、可续传);`--drop-finance` DROP 3 张 finance_* 表。
每次调用聚宽 API 后打印剩余额度。
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jqdatasdk import finance, get_query_count, query  # noqa: E402

from kedu.db import DATABASE, auth_from_env, get_client  # noqa: E402
from kedu.finance_schema import RUN_QUERY_TABLES, STK_TABLES, new_table_ddl, schema_from_model  # noqa: E402
from scripts.backfill_jq import jq_auth  # noqa: E402

FINANCE_TABLES = ["finance_income_statement", "finance_balance_sheet", "finance_cashflow_statement"]

OFFSET_CAP = 200_000      # 聚宽 run_offset_query 服务端硬上限
PAGE_FULL = 190_000       # 一页返回行数 >= 此值视为「可能还有」,继续按 id 翻页;< 此值即末页

# 个别表分块列覆盖:默认 _chunk_col 偏好 end_date,但这些表 end_date 常为空,
# 按 end_date 分块会漏行 -> STK_SHARES_FROZEN 用 pub_date;STK_LIST/COMPANY_INFO 无可靠
# 报告期/披露日列且体量小 -> 整表全量拉(None)。其余表不在此表,沿用 _chunk_col。
CHUNK_COL_OVERRIDE: dict[str, str | None] = {
    "STK_SHARES_FROZEN": "pub_date",
    "STK_LIST": None,
    "STK_COMPANY_INFO": None,
    # 基金主体信息无逐行报告期/披露日列(start_date/pub_date 为实体属性),体量小 -> 整表全量拉。
    "FUND_MAIN_INFO": None,
    # ETF 跟踪指数:_chunk_col 默认会选 end_date,但生效中的行 end_date 为空(未失效),
    # 按 end_date 分块会漏掉当前生效行 -> 改按 pub_date(永不为空)。
    "FUND_INVEST_TARGET": "pub_date",
}

# 小型可变主数据:无可靠水位列,或水位列是实体属性(发行日),按水位增量会漏新行/修订。
# 体量小 -> 每次增量也整表全量重拉(ReplacingMergeTree 按 id 去重,幂等)。
FULL_RELOAD_TABLES = {"FUND_MAIN_INFO", "STK_XR_XD"}

# 同步的规范逻辑表(按 CH 表名去重,剔除别名)
STK_SYNC_TABLES: list[str] = []
_seen_ch: set[str] = set()
for _jq, _ch in STK_TABLES.items():
    if _ch not in _seen_ch:
        _seen_ch.add(_ch)
        STK_SYNC_TABLES.append(_jq)


def qlog(msg: str) -> None:
    print(f"{msg} | spare quota: {get_query_count()}")


def _model(jq_name: str):
    return getattr(finance, jq_name)


def _chunk_col(cols: set[str]) -> str | None:
    """全量按年分块所用日期列(取报告期/披露日,无则用交易日 date/day)。

    末尾的 day 服务基金净值表(FUND_NET_VALUE 仅有 day 列),放最后不影响 STK 选择。
    """
    for c in ("end_date", "pub_date", "date", "day"):
        if c in cols:
            return c
    return None


def _watermark_col(cols: set[str]) -> str | None:
    """增量水位列:**优先 pub_date** —— 财报表靠披露日水位才能补到「旧报告期、新披露/重述」;
    市场汇总表(STK_MT_TOTAL / STK_EXCHANGE_TRADE_INFO)无 pub_date,退到交易日 date;
    基金净值表仅有 day(末尾,不影响 STK/财报表的优先级)。"""
    for c in ("pub_date", "date", "end_date", "day"):
        if c in cols:
            return c
    return None


def _prep(df: pd.DataFrame, schema: list[tuple[str, str]]) -> pd.DataFrame:
    df = df.copy()
    for col, ctype in schema:
        if col not in df.columns:
            df[col] = None
            continue
        if "Date" in ctype:
            s = pd.to_datetime(df[col], errors="coerce")
            df[col] = s.dt.date.where(s.notna(), None)
        elif "Int" in ctype or "UInt" in ctype:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
        elif "Float" in ctype or "Decimal" in ctype:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
        else:
            df[col] = df[col].astype("object").where(df[col].notna(), None)
    df["id"] = pd.to_numeric(df["id"], errors="coerce").fillna(0).astype("int64")
    return df[[c for c, _ in schema]]


def _resolve_chunk_col(jq_name: str, cols: set[str]) -> str | None:
    """解析全量分块列:个别表显式覆盖(end_date 常空者改 pub_date/整表全量),否则用 _chunk_col。"""
    if jq_name in CHUNK_COL_OVERRIDE:
        return CHUNK_COL_OVERRIDE[jq_name]
    return _chunk_col(cols)


def _month_ranges(year: int) -> list[tuple[dt.date, dt.date]]:
    """某年 12 个自然月的闭区间 [首日, 末日]。回补按月**顺序**入库,保证时间序,
    中断后增量(pub_date 水位 − overlap)能干净续传(残缺至多一个月,远在 180 天回拉内)。"""
    out = []
    for m in range(1, 13):
        lo = dt.date(year, m, 1)
        hi = dt.date(year, 12, 31) if m == 12 else dt.date(year, m + 1, 1) - dt.timedelta(days=1)
        out.append((lo, hi))
    return out


def _months_between(lo: dt.date, hi: dt.date) -> list[tuple[dt.date, dt.date]]:
    """[lo, hi] 跨任意年份按自然月切分(首/末段裁剪到 lo/hi)。供增量逐月顺序拉取。"""
    out = []
    cur = lo.replace(day=1)
    while cur <= hi:
        mend = (dt.date(cur.year, 12, 31) if cur.month == 12
                else dt.date(cur.year, cur.month + 1, 1) - dt.timedelta(days=1))
        out.append((max(cur, lo), min(mend, hi)))
        cur = mend + dt.timedelta(days=1)
    return out


def _keyset_pages(model, col, lo: dt.date, hi: dt.date):
    """对 [lo, hi] 按 id 升序 keyset 分页, **逐页 yield**(每页 ≤20 万,全满即继续翻页)。

    keyset(id > 游标)绕过单次 20 万上限,可拉任意大小区间:每页都满载、零丢弃,
    且单个 pub_date 即 >20 万(年报截止日全市场同日披露)时自然跨页,杜绝静默截断。"""
    last_id = None
    while True:
        q = query(model).filter(col >= lo.isoformat(), col <= hi.isoformat())
        if last_id is not None:
            q = q.filter(model.id > last_id)
        page = finance.run_offset_query(q.order_by(model.id))
        if page is None or page.empty:
            break
        yield page
        if len(page) < PAGE_FULL:
            break
        last_id = int(pd.to_numeric(page["id"]).max())


def _pull_paged_by_id(model, col, lo: dt.date, hi: dt.date) -> pd.DataFrame:
    """[lo, hi] 按 id 分页拉全并拼成单个 DataFrame(供 repair 等需要整份结果处)。"""
    parts = list(_keyset_pages(model, col, lo, hi))
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def _pull_keyset(client, ch, model, col, lo: dt.date, hi: dt.date, schema, label: str) -> int:
    """[lo, hi] 按 id keyset 分页, 逐页入库(流式, 不在内存攒整年)。返回行数。

    取代旧的「按日期分块 + 逼近上限缩块」:每页都满载 20 万、无丢弃拉取;
    单日 >20 万 自然跨页。轻量区间一两页即完。"""
    total = 0
    npage = 0
    for page in _keyset_pages(model, col, lo, hi):
        client.insert_df(f"{DATABASE}.{ch}", _prep(page, schema))
        total += len(page)
        npage += 1
        if npage % 5 == 0:  # 周期性进度,避免长时间静默
            print(f"  {label}: {npage} 页,累计 {total} 行 | spare {get_query_count()['spare']:,}",
                  flush=True)
    return total


def backfill_table(client, jq_name: str, schema: list[tuple[str, str]],
                   start_year: int, end_year: int, skip: bool) -> None:
    """全量回补:按分块列**逐月顺序**拉取,月内按 id keyset 分页(每页满载 20 万、零丢弃)。
    skip=True 跳过已有年份(续传)。逐月入库保证时间序,中断后增量水位可干净续传(残缺至多一月)。"""
    ch = RUN_QUERY_TABLES[jq_name]
    model = _model(jq_name)
    cols = {c for c, _ in schema}
    chunk_col = _resolve_chunk_col(jq_name, cols)

    if chunk_col is None:
        df = finance.run_offset_query(query(model))
        qlog(f"  {ch}: pulled {len(df)} (no chunk col)")
        if not df.empty:
            client.insert_df(f"{DATABASE}.{ch}", _prep(df, schema))
        return

    col = getattr(model, chunk_col)
    for y in range(start_year, end_year + 1):
        if skip:
            cnt = client.query(
                f"SELECT count() FROM {DATABASE}.{ch} "
                f"WHERE {chunk_col} >= toDate('{y}-01-01') AND {chunk_col} <= toDate('{y}-12-31')"
            ).result_rows[0][0]
            if cnt:
                print(f"  {ch} {y}: skip ({cnt} rows)")
                continue
        total = 0
        for mlo, mhi in _month_ranges(y):
            mt = _pull_keyset(client, ch, model, col, mlo, mhi, schema, f"{ch} {mlo:%Y-%m}")
            total += mt
            if mt:  # 仅非空月吐一行,空月静默
                print(f"  {ch} {mlo:%Y-%m}: +{mt:,} | spare {get_query_count()['spare']:,}", flush=True)
        qlog(f"  {ch} {y}: {total} rows")


def incremental(client, jq_name: str, schema: list[tuple[str, str]], overlap_days: int = 180) -> int:
    """按水位列近窗口增量 upsert(ReplacingMergeTree 幂等,含 report_type=1 重述)。

    水位列优先 pub_date(财报表补「旧期新披露/重述」);市场汇总表无 pub_date 时退到 date。
    """
    ch = RUN_QUERY_TABLES[jq_name]
    model = _model(jq_name)
    if jq_name in FULL_RELOAD_TABLES:
        # 整表重拉(小型可变主数据,无可靠水位列)。
        df = finance.run_offset_query(query(model))
        if not df.empty:
            client.insert_df(f"{DATABASE}.{ch}", _prep(df, schema))
        qlog(f"  {ch}: 整表重拉 {len(df)} 行")
        return len(df)
    cur = _watermark_col({c for c, _ in schema})
    if cur is None:
        qlog(f"  {ch}: 无水位列,跳过增量")
        return 0
    mx = client.query(f"SELECT max({cur}) FROM {DATABASE}.{ch}").result_rows[0][0]
    since = (mx - dt.timedelta(days=overlap_days)) if mx else dt.date(2005, 1, 1)
    col = getattr(model, cur)
    # 逐月顺序 + 月内 id keyset:小表一页完;高基数表/峰值日自然跨页,杜绝截断;
    # 逐月入库保证时间序,窗口跨多年(回补中断后续传)时也能干净前进、不丢数据。
    n = 0
    for mlo, mhi in _months_between(since, dt.date.today()):
        mt = _pull_keyset(client, ch, model, col, mlo, mhi, schema, f"{ch} {mlo:%Y-%m}")
        n += mt
        if mt:
            print(f"  {ch} {mlo:%Y-%m}: +{mt:,} | spare {get_query_count()['spare']:,}", flush=True)
    qlog(f"  {ch}: +{n} (since {since.isoformat()})")
    return n


# 起始年下探到 1990(沪深建市):STK_STATUS_CHANGE 的"已发行未上市/正常上市"等
# IPO 状态事件 pub_date 可早至 2000 年前,默认 2005 会漏拉(财报类表无 pre-2005 数据,
# 早年空块仅多几次返回 0 行的查询,配额可忽略)。
def sync_table(client, jq_name: str, full: bool = False, start_year: int = 1990,
               end_year: int | None = None, overlap_days: int = 180) -> None:
    """统一同步单表:建表(若无) → 空表/强制全量回补,否则增量。"""
    ch = RUN_QUERY_TABLES[jq_name]
    end_year = end_year or dt.date.today().year
    try:
        model = _model(jq_name)
        schema = schema_from_model(model)
    except Exception as e:
        print(f"  {jq_name}: skip 模型不可用 ({str(e)[:60]})")
        return
    client.command(new_table_ddl(ch, schema))  # 表不存在则建(空 ClickHouse 自动建表)
    cnt = client.query(f"SELECT count() FROM {DATABASE}.{ch}").result_rows[0][0]
    try:
        if cnt == 0:
            qlog(f"  {ch}: 空表 → 全量回补")
            backfill_table(client, jq_name, schema, start_year, end_year, skip=False)
        elif full:
            qlog(f"  {ch}: 强制全量({cnt} 行,按年续传)")
            backfill_table(client, jq_name, schema, start_year, end_year, skip=True)
        else:
            incremental(client, jq_name, schema, overlap_days)
    except Exception as e:
        print(f"  ERROR {ch}: {str(e)[:120]}")
        return
    client.command(f"OPTIMIZE TABLE {DATABASE}.{ch} FINAL")


def sync_all(client, tables=None, full: bool = False, start_year: int = 1990,
             end_year: int | None = None, overlap_days: int = 180) -> None:
    for jq_name in (tables or STK_SYNC_TABLES):
        print(f"== {jq_name} ({RUN_QUERY_TABLES[jq_name]}) ==")
        sync_table(client, jq_name, full, start_year, end_year, overlap_days)


def drop_finance(client) -> None:
    for t in FINANCE_TABLES:
        client.command(f"DROP TABLE IF EXISTS {DATABASE}.{t}")
        print(f"  dropped {t}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--tables", help="逗号分隔逻辑表名,缺省=全部 STK 表(去别名)")
    p.add_argument("--full", action="store_true", help="强制全量(按年跳过已有、可续传)")
    p.add_argument("--start-year", type=int, default=1990)
    p.add_argument("--end-year", type=int, default=dt.date.today().year)
    p.add_argument("--overlap-days", type=int, default=180)
    p.add_argument("--drop-finance", action="store_true", help="DROP 3 张 finance_* 表后退出")
    args = p.parse_args()

    auth_from_env()
    client = get_client()
    if args.drop_finance:
        print("== DROP finance_* ==")
        drop_finance(client)
        return

    jq_auth()
    tables = [t.strip().upper() for t in args.tables.split(",")] if args.tables else None
    print("== STK 同步(空表→全量,有数据→增量)==")
    sync_all(client, tables, args.full, args.start_year, args.end_year, args.overlap_days)
    qlog("SYNC DONE")


if __name__ == "__main__":
    main()
