"""live 响应快照缓存层 —— 省 JQ 配额的核心。

测试比对的是「本地 ClickHouse 结果」vs「joinquant live 结果」。live 结果对历史数据
是不变的,所以首次拉取后落盘 pickle,重跑直接读缓存(0 配额)。

- 缓存目录:tests/_snapshots/(.gitignore,不入库 —— 快照是 live 数据)。
- key:人类可读前缀 + 参数 hash,文件名 `<prefix>__<hash>.pkl`。
- 命中且非 refresh -> pickle.load;否则惰性触发 JQ auth -> producer() -> 落盘。
- 纯命中跑无需 JQ 凭证,只需 ClickHouse 凭证。

时间相关的 live 查询(fq='pre' 锚最新交易日、get_trade_days 到 today)不应缓存,
调用方用 use_cache=False 直连,或在 --refresh-snapshots 时才比对。
"""
from __future__ import annotations

import hashlib
import os
import pickle
import re
from pathlib import Path
from typing import Any, Callable

SNAP_DIR = Path(__file__).resolve().parent / "_snapshots"

_jq_authed = False
_n_live_calls = 0


def _safe(prefix: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "-", prefix)[:80]


def _key_path(prefix: str, params: Any) -> Path:
    blob = repr(params).encode("utf-8")
    h = hashlib.sha1(blob).hexdigest()[:12]
    return SNAP_DIR / f"{_safe(prefix)}__{h}.pkl"


def ensure_jq_auth() -> None:
    """惰性登录 JQ:仅在发生缓存 miss(真要打 live)时调用。"""
    global _jq_authed
    if _jq_authed:
        return
    import jqdatasdk

    user, pwd = os.getenv("JQDATA_USER"), os.getenv("JQDATA_PASSWORD")
    if not user or not pwd:
        raise RuntimeError(
            "缓存 miss 需要拉 live,但缺少 JQDATA_USER / JQDATA_PASSWORD。"
            "请用 `uv run --env-file .env pytest ...`,或去掉 --refresh-snapshots 走已有快照。"
        )
    jqdatasdk.auth(user, pwd)
    _jq_authed = True


def live_calls() -> int:
    """本会话实际打到 live 的次数(缓存 miss 数)。"""
    return _n_live_calls


class Snapshotter:
    """绑定 refresh 标志的快照取数器。"""

    def __init__(self, refresh: bool = False):
        self.refresh = refresh

    def get(self, prefix: str, params: Any, producer: Callable[[], Any]) -> Any:
        """返回 live 结果:命中缓存读盘,否则惰性 auth + 拉取 + 落盘。"""
        path = _key_path(prefix, params)
        if path.exists() and not self.refresh:
            with open(path, "rb") as f:
                return pickle.load(f)
        global _n_live_calls
        ensure_jq_auth()
        result = producer()
        _n_live_calls += 1
        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".pkl.tmp")
        with open(tmp, "wb") as f:
            pickle.dump(result, f, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(path)
        return result
