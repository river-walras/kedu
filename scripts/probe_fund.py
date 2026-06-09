"""Phase 0 live 探针:确定基金 API 的 parity 契约,实现前先跑(需 live auth)。

用法:
  uv run --env-file .env python scripts/probe_fund.py

只读、低配额(每探针 1~数次调用)。把输出贴回,据此定稿:
  P0a 10 张 FUND_* ORM 是否存在 + 列名/列序(对照 reference/基金/*.md)
  P0b get_all_securities 细分类语义(mmf/reits/fjm 能否作参数;fund/etf/lof/fja/fjb 各返回什么)
  P0c get_extras 净值:index 口径(A股交易日 / 净值日 / 窗口)、缺失是否 ffill、后缀 vs 裸码
  P0d get_price 基金:复权价 round 位数、avg 精度、舍入方向、volume 是否整数
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

import jqdatasdk  # noqa: E402
from jqdatasdk import auth, finance, get_all_securities, get_extras, get_price, get_query_count, query  # noqa: E402

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 60)

FUND_TABLES = [
    "FUND_MAIN_INFO", "FUND_NET_VALUE", "FUND_FIN_INDICATOR", "FUND_PORTFOLIO",
    "FUND_PORTFOLIO_BOND", "FUND_PORTFOLIO_STOCK", "FUND_INVEST_TARGET",
    "FUND_DIVIDEND", "FUND_SHARE_DAILY", "FUND_MF_DAILY_PROFIT",
]


def hr(title: str) -> None:
    print("\n" + "=" * 78 + f"\n== {title}\n" + "=" * 78)


def p0a() -> None:
    hr("P0a 基金 ORM 存在性 + 列名/列序")
    for name in FUND_TABLES:
        try:
            model = getattr(finance, name)
        except Exception as e:  # noqa: BLE001
            print(f"[{name}] getattr 失败: {type(e).__name__}: {e}")
            continue
        try:
            df = finance.run_query(query(model).limit(1))
            print(f"[{name}] OK  列序={list(df.columns)}")
            print(f"          dtypes={ {c: str(t) for c, t in df.dtypes.items()} }")
        except Exception as e:  # noqa: BLE001
            print(f"[{name}] run_query 失败: {type(e).__name__}: {e}")


def p0b() -> None:
    hr("P0b get_all_securities 细分类语义")
    for types in (["fund"], ["open_fund"], ["etf"], ["lof"], ["fja"], ["fjb"],
                  ["mmf"], ["reits"], ["fjm"]):
        try:
            df = get_all_securities(types=types)
            tc = df["type"].value_counts().to_dict() if "type" in df.columns else {}
            print(f"types={types}: shape={df.shape}, type 分布={tc}")
        except Exception as e:  # noqa: BLE001
            print(f"types={types}: 报错 {type(e).__name__}: {e}")
    # 场内全集的 type 取值
    try:
        df = get_all_securities(types=["fund"])
        print(f"\n['fund'] 全部 type 取值: {sorted(df['type'].unique())}")
        print(df.head(3))
    except Exception as e:  # noqa: BLE001
        print(f"['fund'] 取值探测失败: {e}")


def p0c() -> None:
    hr("P0c get_extras 净值 index/ffill/后缀")
    onex = "510300.XSHG"   # 场内 ETF
    otc = "000001.OF"      # 场外基金
    for info in ("unit_net_value", "acc_net_value", "adj_net_value"):
        try:
            df = get_extras(info, [onex, otc], start_date="2021-01-04", end_date="2021-01-15", df=True)
            print(f"\n[{info}] df=True  index.dtype={df.index.dtype}  shape={df.shape}")
            print(f"  index={[str(x)[:10] for x in df.index]}")
            print(f"  含 NaN? {df.isna().any().to_dict()}")
            print(df)
        except Exception as e:  # noqa: BLE001
            print(f"[{info}] 失败: {type(e).__name__}: {e}")
    # df=False 形态
    try:
        d = get_extras("unit_net_value", [onex], start_date="2021-01-04", end_date="2021-01-08", df=False)
        print(f"\ndf=False -> type={type(d)}, keys={list(d)}, sample={ {k: v[:3] for k, v in d.items()} }")
    except Exception as e:  # noqa: BLE001
        print(f"df=False 失败: {e}")
    # 后缀 vs 裸码
    for code in ("510300", "510300.XSHG", "510300.OF"):
        try:
            df = get_extras("unit_net_value", [code], start_date="2021-01-04", end_date="2021-01-08", df=True)
            print(f"code={code!r}: 列={list(df.columns)}, 非空={int(df.notna().sum().sum())}")
        except Exception as e:  # noqa: BLE001
            print(f"code={code!r}: 报错 {type(e).__name__}: {e}")
    # count 语义
    try:
        df = get_extras("unit_net_value", [onex], end_date="2021-01-15", count=5, df=True)
        print(f"\ncount=5 -> shape={df.shape}, index={[str(x)[:10] for x in df.index]}")
    except Exception as e:  # noqa: BLE001
        print(f"count 语义失败: {e}")


def p0d() -> None:
    hr("P0d get_price 基金 round/avg/方向/volume")
    etf = "510300.XSHG"
    flds = ["open", "close", "high", "low", "volume", "money", "avg", "factor", "pre_close"]
    for fq in (None, "post", "pre"):
        for rnd in (True, False):
            try:
                df = get_price(etf, start_date="2021-01-04", end_date="2021-01-08",
                               frequency="daily", fields=flds, fq=fq, round=rnd, panel=False)
                print(f"\nfq={fq!r} round={rnd}:")
                print(df.head(5).to_string())
            except Exception as e:  # noqa: BLE001
                print(f"fq={fq!r} round={rnd}: 失败 {type(e).__name__}: {e}")
    # 小数位数观察:取 fq='post' round=True 的 close/avg 最大小数位
    try:
        df = get_price(etf, start_date="2021-01-04", end_date="2021-01-29", frequency="daily",
                       fields=["close", "avg", "volume"], fq="post", round=True, panel=False)
        def maxdec(s):
            return max((len(str(float(x)).split(".")[1].rstrip("0")) for x in s.dropna() if "." in str(float(x))), default=0)
        print(f"\nfq=post round=True: close 最大小数位={maxdec(df['close'])}, avg={maxdec(df['avg'])}, "
              f"volume 全整数? {bool((df['volume'].dropna() % 1 == 0).all())}")
    except Exception as e:  # noqa: BLE001
        print(f"小数位观察失败: {e}")


def main() -> None:
    auth(os.getenv("JQDATA_USER"), os.getenv("JQDATA_PASSWORD"))
    print("auth ok | query count:", get_query_count())
    print("jqdatasdk version:", getattr(jqdatasdk, "__version__", "?"))
    for fn in (p0a, p0b, p0c, p0d):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"\n!! {fn.__name__} 整体异常: {type(e).__name__}: {e}")
    print("\nquery count after probes:", get_query_count())


if __name__ == "__main__":
    main()
