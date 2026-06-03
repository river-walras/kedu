"""从聚宽抓取行业/概念分类数据到 ClickHouse(读侧见 kedu.industry / kedu.concept):

  - industries        ← get_industries(name, date) 年度网格 + 标准切换日(及前一日)快照 diff 出有效区间
  - concepts          ← get_concepts()
  - industry_history  ← 逐交易日 get_industry(全市场在市股, date, df=True) 折叠成区间
  - concept_history   ← 逐交易日 get_concept(全市场在市股, date) 折叠成区间

为什么逐股 walk:get_history_industry 是聚宽付费模块(本账户无权限);get_industry /
get_concept 按「1 条/股」计费(一次返回该股全部 6 行业体系 / 全部概念),逐股扫全市场远比
逐类(get_industry_stocks / get_concept_stocks,1 条/成分)省 ~6 倍配额、~16 倍调用次数。
全历史一次性:行业 2005→今约 1586 万条、概念 2016→今约 1021 万条。

回补走 staging(industry_member_raw / concept_member_raw):逐日处理全市场股票(universe:
个股上市(含上市前缓冲)起一直扫到最新日,**不在退市日截断** —— 聚宽会把分类延续到退市之后
(2021 建 jq 体系时把个别老票追溯回填、开区间永不封口),退市后无分类的天返回空、不入库),
游标 = staging 的 max(date)(整日处理完才推进,照搬 update_bars 的全局 max 游标),配额低于
min_spare 在「日边界」优雅停止、重跑续传。折叠用 gaps-and-islands(按 trade_days 序号判相邻
连续),区间尾达到 staging 已覆盖的最大交易日 → end_date=NULL(仍是成分);因 universe 逐日
全扫,无需空成分占位、无需逐实体完整性闸门。

用法(就一条命令,反复跑即可——没补完就续传,补完后每次只补新交易日):
  uv run --env-file .env python scripts/backfill_industry.py
可选:--min-spare N、--batch N。彻底重灌某体系:先 TRUNCATE 对应 staging,再跑本命令(walk 见空表自动从 2005/2016 重走)。
日常也可不单独跑:update_jqdata.py 已在每日 cron 里调 sync() 一并更新。
"""
from __future__ import annotations

import argparse
import bisect
import datetime as dt
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jqdatasdk  # noqa: E402
from jqdatasdk import get_query_count  # noqa: E402

from kedu.db import DATABASE, auth_from_env, get_client  # noqa: E402
from kedu.schema import CLASSIFY_DDL  # noqa: E402
import scripts.backfill_jq as bk  # noqa: E402  (复用 jq_auth)

# 6 个行业体系,名称对齐 get_industries / get_industry 输出键。
TAXONOMIES = ["sw_l1", "sw_l2", "sw_l3", "jq_l1", "jq_l2", "zjw"]

# 已知行业分类标准切换日(申万 2014/2021、聚宽 2021、证监会 2024);用于 industries 有效区间 diff。
STD_CHANGE_DATES = ["2014-02-20", "2021-12-11", "2021-12-13", "2024-02-08"]

# 逐股 walk 的全历史起点。
INDUSTRY_START = "2005-01-01"
CONCEPT_START = "2016-07-31"
BATCH = 800           # get_industry / get_concept 单次股票数(配额按股计,与批大小无关)
FLUSH_ROWS = 300_000  # staging 累计行数阈值,整日边界落盘
# 上市前缓冲:聚宽行业/概念在 get_all_securities 登记的上市日之前若干交易日就已给分类
# (实测 300487 早 2 个、603435 早 13 个交易日)。walk 的证券宇宙把起始日前挪 N 个交易日 ——
# 只是多**查询**上市前 N 天,但只**存储**聚宽确给分类的天,故区间起点自动对齐聚宽真实分类起点
# (偏移 ≤ N 即精确)。取 20 覆盖实测最大 13 并留余量;>N 的极端新股仍会偏(需逐日回探才彻底消除)。
LIST_BUFFER_TRADE_DAYS = 20

# 逐股 walk 的 staging(折叠成区间后可 --drop-raw)。
IND_RAW_DDL = f"""CREATE TABLE IF NOT EXISTS {DATABASE}.industry_member_raw (
  name String, stock String, industry_code String, date Date,
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at)
ORDER BY (name, stock, date)"""

CON_RAW_DDL = f"""CREATE TABLE IF NOT EXISTS {DATABASE}.concept_member_raw (
  concept_code String, stock String, date Date,
  _ingested_at DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(_ingested_at)
ORDER BY (concept_code, stock, date)"""


def _resolve_today(today: str | None) -> dt.date:
    return dt.date.fromisoformat(today) if today else dt.date.today()


def _as_date(v):
    """把聚宽/ClickHouse 返回的日期类值规整为 datetime.date,缺失为 None。"""
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return pd.Timestamp(v).date()


def _universe(secs, day):
    """某交易日要 walk 的股票:个股上市(含上市前缓冲)起一直扫到最新日,不在退市日截断。

    聚宽把行业分类延续到退市之后(个别老票被追溯回填、开区间永不封口),故退市后仍逐日问;
    无分类的天返回空、不入库,折叠时天然成空档。ed 不再参与过滤(保留以兼容 secs 三元组)。
    """
    return [code for code, sd, ed in secs if sd is None or sd <= day]


def _batches(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _securities(client):
    return client.query(
        f"SELECT instrument_id, start_date, end_date FROM {DATABASE}.securities "
        f"WHERE type='stock' ORDER BY instrument_id").result_rows


def _buffered_securities(client, n: int = LIST_BUFFER_TRADE_DAYS):
    """证券宇宙,start_date 向前挪 n 个交易日(贴合聚宽上市前即给分类的口径)。

    只放宽 walk 查询窗口的左端;end_date(退市)不动。多查的上市前天若聚宽无分类则返回空、不入库,
    故区间起点仍落在聚宽真实分类起点。
    """
    secs = _securities(client)
    if n <= 0:
        return secs
    td = _trade_days(client, "1990-01-01", "2099-12-31")
    if not td:
        return secs
    out = []
    for code, sd, ed in secs:
        if sd is not None:
            i = bisect.bisect_left(td, sd)
            sd = td[max(0, i - n)]
        out.append((code, sd, ed))
    return out


def _trade_days(client, start_iso, end_iso):
    return [r[0] for r in client.query(
        f"SELECT day FROM {DATABASE}.trade_days WHERE day BETWEEN '{start_iso}' AND '{end_iso}' "
        f"ORDER BY day").result_rows]


def _max_date(client, table):
    """staging 的 max(date);空表返回 None。

    注意:ClickHouse 对**空表**的 max(date)(非空 Date 列)返回默认值 1970-01-01 而非 NULL,
    若直接当游标会从 1970/2005 起走(concept 起点本应 2016)。故先 count 判空。
    """
    cnt, mx = client.query(f"SELECT count(), max(date) FROM {DATABASE}.{table}").result_rows[0]
    return mx if cnt else None


def _raw_cols(kind: str) -> list[str]:
    return ["name", "stock", "industry_code", "date"] if kind == "industry" else ["concept_code", "stock", "date"]


def _pull_day(kind: str, stocks: list[str], d: dt.date, batch: int) -> list[tuple]:
    """逐股拉某交易日的行业/概念成员,分批调用,返回 staging 行(配额 1 条/股)。"""
    iso = d.isoformat()
    rows: list[tuple] = []
    for chunk in _batches(stocks, batch):
        if kind == "industry":
            df = jqdatasdk.get_industry(chunk, date=iso, df=True)
            if df is None or df.empty:
                continue
            for code, typ, icode, _ in df[["code", "type", "industry_code", "industry_name"]].itertuples(index=False):
                rows.append((str(typ), str(code), str(icode), d))
        else:
            res = jqdatasdk.get_concept(chunk, date=iso)
            for stock, info in (res or {}).items():
                for con in info.get("jq_concept", []):
                    rows.append((str(con["concept_code"]), str(stock), d))
    return rows


# ---------------------------------------------------------------------------
# 列表(全量权威拉取 + TRUNCATE+reload)
# ---------------------------------------------------------------------------
def _industries_snapshot_dates(today: dt.date) -> list[dt.date]:
    """行业列表快照日:年度网格(2005 起)+ 各标准切换日及其前一日 + today。

    标准切换日前一日能把退役旧码的 end_date 精确钉到切换日-1(对齐聚宽真实失效日)。
    """
    dates: set[dt.date] = {dt.date(y, 1, 1) for y in range(2005, today.year + 1)}
    for s in STD_CHANGE_DATES:
        b = dt.date.fromisoformat(s)
        dates.add(b)
        dates.add(b - dt.timedelta(days=1))
    dates.add(today)
    return sorted(d for d in dates if d <= today)


def sync_industries(client, today: str | None = None) -> None:
    """逐 taxonomy 在快照日网格调 get_industries(name, date),diff 出每个行业码的 [start_date, end_date]。

    start_date 取聚宽返回值;end_date 取该码最后一次出现的快照日(若今日仍在则 NULL)。TRUNCATE+reload。
    """
    client.command(CLASSIFY_DDL["industries"])
    today_d = _resolve_today(today)
    snap_dates = _industries_snapshot_dates(today_d)
    rows: list[tuple] = []
    for name in TAXONOMIES:
        meta: dict[str, tuple] = {}
        last_present: dict[str, dt.date] = {}
        present_today: set[str] = set()
        for d in snap_dates:
            df = jqdatasdk.get_industries(name=name, date=d.isoformat())
            if df is None or df.empty:
                continue
            for code, row in df.iterrows():
                meta[str(code)] = (str(row["name"]), _as_date(row["start_date"]))
                last_present[str(code)] = d
                if d == today_d:
                    present_today.add(str(code))
        for code, (iname, sd) in meta.items():
            end_date = None if code in present_today else last_present[code]
            rows.append((name, code, iname, sd, end_date))
        print(f"  industries {name}: {sum(1 for r in rows if r[0] == name)} 码")
    out = pd.DataFrame(rows, columns=["name", "industry_code", "industry_name", "start_date", "end_date"])
    client.command(f"TRUNCATE TABLE {DATABASE}.industries")
    if not out.empty:
        client.insert_df(f"{DATABASE}.industries", out)
    print(f"  industries: {len(out)} 行({len(TAXONOMIES)} taxonomy × {len(snap_dates)} 快照)")


def sync_concepts(client) -> None:
    """get_concepts() 全量列表,TRUNCATE+reload jqdata.concepts。"""
    client.command(CLASSIFY_DDL["concepts"])
    df = jqdatasdk.get_concepts()
    if df is None or df.empty:
        print("  concepts: empty"); return
    out = df.reset_index()
    out = out.rename(columns={out.columns[0]: "concept_code", "name": "concept_name"})
    out["concept_code"] = out["concept_code"].astype(str)
    out["concept_name"] = out["concept_name"].astype(str)
    out["start_date"] = pd.to_datetime(out["start_date"]).dt.date
    client.command(f"TRUNCATE TABLE {DATABASE}.concepts")
    client.insert_df(f"{DATABASE}.concepts", out[["concept_code", "concept_name", "start_date"]])
    print(f"  concepts: {len(out)}")


# ---------------------------------------------------------------------------
# 逐股 walk(staging,可续传)
# ---------------------------------------------------------------------------
def _walk(client, kind: str, ddl: str, raw_table: str, start_default: str,
          today: str | None, batch: int, min_spare: int) -> None:
    """逐交易日扫全市场在市股的通用 walk。kind ∈ {'industry','concept'}。

    游标 = staging 的 max(date);整日处理完才落盘(配额检查在日边界,故 staging 只含完整日),
    剩余配额低于 min_spare 优雅停止,重跑续传。
    """
    client.command(ddl)
    today_d = _resolve_today(today)
    secs = _buffered_securities(client)
    have = _max_date(client, raw_table)
    start_d = (have + dt.timedelta(days=1)) if have else dt.date.fromisoformat(start_default)
    days = _trade_days(client, start_d.isoformat(), today_d.isoformat())
    if not days:
        print(f"  {kind}_raw: 无待补交易日(已到 {have})"); return

    cols = _raw_cols(kind)
    buf: list[tuple] = []
    total = 0

    def _flush():
        nonlocal buf
        if buf:
            client.insert_df(f"{DATABASE}.{raw_table}", pd.DataFrame(buf, columns=cols))
            buf = []

    for di, d in enumerate(days, 1):
        if get_query_count()["spare"] < min_spare:
            _flush()
            print(f"  {kind}_raw: 剩余配额不足 {min_spare:,},优雅停止于 {d}"
                  f"(已补到 {days[di - 2] if di > 1 else have},重跑续传)", flush=True)
            return
        day_rows = _pull_day(kind, _universe(secs, d), d, batch)
        buf.extend(day_rows)
        total += len(day_rows)
        if len(buf) >= FLUSH_ROWS:
            _flush()
        if di % 100 == 0 or di == len(days):
            print(f"  {kind}_raw {d}: 累计 +{total:,} 行 | {di}/{len(days)} 日 | spare {get_query_count()['spare']:,}",
                  flush=True)
    _flush()
    print(f"  {kind}_raw: 本轮 +{total:,} 行(补到 {days[-1]})", flush=True)


def backfill_industry_raw(client, today=None, batch=BATCH, min_spare=2_000_000) -> None:
    _walk(client, "industry", IND_RAW_DDL, "industry_member_raw", INDUSTRY_START, today, batch, min_spare)


def backfill_concept_raw(client, today=None, batch=BATCH, min_spare=2_000_000) -> None:
    _walk(client, "concept", CON_RAW_DDL, "concept_member_raw", CONCEPT_START, today, batch, min_spare)


# ---------------------------------------------------------------------------
# 折叠 staging → 区间表(gaps-and-islands)
# ---------------------------------------------------------------------------
def _fold(client, raw_table: str, part_cols: list[str], today: str | None):
    """通用 gaps-and-islands:把 staging 逐日成员折叠成 [start_date, end_date] 区间。

    G = staging 已覆盖的最大交易日序号;区间尾达 G → end_date=NULL(仍是成分)。
    返回 (DataFrame[part_cols + start_date + end_date], 覆盖最大日)。
    """
    today_d = _resolve_today(today)
    iso = today_d.isoformat()
    mx = _max_date(client, raw_table)
    if mx is None:
        return None, None
    gmax = client.query(
        f"SELECT count() FROM {DATABASE}.trade_days WHERE day <= '{mx.isoformat()}'").result_rows[0][0]
    part = ", ".join(part_cols)
    sel = ", ".join(part_cols)
    sql = (
        "WITH "
        f"td AS (SELECT day, row_number() OVER (ORDER BY day) AS idx "
        f"       FROM {DATABASE}.trade_days WHERE day <= '{iso}'), "
        f"raw AS (SELECT {sel}, t.idx AS idx, r.date AS date "
        f"        FROM (SELECT {sel}, date FROM {DATABASE}.{raw_table} FINAL) AS r "
        "        INNER JOIN td AS t ON t.day = r.date), "
        f"islands AS (SELECT {sel}, idx, date, "
        f"            idx - row_number() OVER (PARTITION BY {part} ORDER BY idx) AS grp FROM raw) "
        f"SELECT {sel}, min(date) AS start_date, max(date) AS end_date_raw, max(idx) AS end_idx "
        f"FROM islands GROUP BY {part}, grp"
    )
    rows = client.query(sql).result_rows
    if not rows:
        return None, gmax
    df = pd.DataFrame(rows, columns=[*part_cols, "start_date", "end_date_raw", "end_idx"])
    df["start_date"] = df["start_date"].map(_as_date)
    df["end_date"] = df["end_date_raw"].map(_as_date)
    df.loc[df["end_idx"] == gmax, "end_date"] = None
    return df, gmax


def build_industry_history(client, today=None) -> None:
    client.command(CLASSIFY_DDL["industry_history"])
    df, _ = _fold(client, "industry_member_raw", ["name", "stock", "industry_code"], today)
    if df is None:
        print("  build_industry_history: staging 为空,先跑 --industry-backfill"); return
    out = df[["name", "industry_code", "stock", "start_date", "end_date"]]
    client.command(f"TRUNCATE TABLE {DATABASE}.industry_history")
    client.insert_df(f"{DATABASE}.industry_history", out)
    print(f"  industry_history: {len(out)} 区间 / {df['stock'].nunique()} 股 / {df['name'].nunique()} 体系")


def build_concept_history(client, today=None) -> None:
    client.command(CLASSIFY_DDL["concept_history"])
    df, _ = _fold(client, "concept_member_raw", ["concept_code", "stock"], today)
    if df is None:
        print("  build_concept_history: staging 为空,先跑 --concept-backfill"); return
    out = df[["concept_code", "stock", "start_date", "end_date"]]
    client.command(f"TRUNCATE TABLE {DATABASE}.concept_history")
    client.insert_df(f"{DATABASE}.concept_history", out)
    print(f"  concept_history: {len(out)} 区间 / {df['concept_code'].nunique()} 概念 / {df['stock'].nunique()} 股")


# ---------------------------------------------------------------------------
# 唯一对外入口:一站式增量(反复跑即可,断点续传,天然增量)
# ---------------------------------------------------------------------------
def sync(client, today=None, batch=BATCH, min_spare=2_000_000):
    """行业 + 概念一站式更新。反复跑即可:历史没补完就续传,补完后每次只补新交易日。

    每次执行:
      1) 刷新 industries / concepts 列表(便宜,TRUNCATE+reload);
      2) 行业 / 概念逐股 walk:自 staging 的 max(date) 续拉到 today —— 只下**新交易日**、
         绝不重下;剩余配额低于 min_spare 在日边界优雅停止,重跑自动续传;
      3) 折叠 staging → industry_history / concept_history(本地重算,不耗配额)。

    种子阶段与日常增量是**同一条路径**:不存在"重新下载",staging 游标保证只取增量。
    彻底重灌某体系:TRUNCATE 对应 staging(industry_member_raw / concept_member_raw),
    walk 见空表自动从 2005/2016 重走。staging 需保留以支撑续传与折叠,否则别 --drop-raw。
    """
    print("== 行业/概念列表 ==")
    sync_industries(client, today=today)
    sync_concepts(client)
    print("== 行业逐股 walk(自 max(date) 续传到 today)==")
    backfill_industry_raw(client, today=today, batch=batch, min_spare=min_spare)
    print("== 概念逐股 walk(自 max(date) 续传到 today)==")
    backfill_concept_raw(client, today=today, batch=batch, min_spare=min_spare)
    print("== 折叠区间 ==")
    build_industry_history(client, today=today)
    build_concept_history(client, today=today)


def main() -> None:
    p = argparse.ArgumentParser(
        description="行业/概念增量更新:反复跑即可——历史没补完就续传(只下新交易日、不重下),补完后每次只补新日。")
    p.add_argument("--drop-raw", action="store_true",
                   help="完成后删 staging —— 会导致下次无法续传/增量,仅在彻底不再更新时用")
    p.add_argument("--batch", type=int, default=BATCH, help="get_industry/get_concept 单次股票数")
    p.add_argument("--min-spare", type=int, default=2_000_000, help="逐股 walk 剩余配额低于此值优雅停止")
    args = p.parse_args()

    bk.jq_auth()
    auth_from_env()
    client = get_client()
    sync(client, batch=args.batch, min_spare=args.min_spare)
    if args.drop_raw:
        client.command(f"DROP TABLE IF EXISTS {DATABASE}.industry_member_raw")
        client.command(f"DROP TABLE IF EXISTS {DATABASE}.concept_member_raw")
        print("  staging dropped(下次将无法续传/增量)")
    print("query count:", get_query_count())
    print("DONE")


if __name__ == "__main__":
    main()
