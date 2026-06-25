"""ClickHouse 连接.

凭证必须通过 `kedu.auth(username, password, ...)` 显式设定, 仿 jqdatasdk.auth.
未调用 auth() 时 get_client() 直接 raise, 不做任何环境变量或默认回退.
脚本场景可用 `auth_from_env()` 显式从环境变量拉起, 配合 `uv run --env-file .env`.
不再依赖 python-dotenv.
"""
from __future__ import annotations

import os

import clickhouse_connect

DATABASE = "jqdata"

# auth() 写入的连接凭证;为空表示尚未 auth,get_client() 将 raise。
_CREDENTIALS: dict = {}

# 默认库的客户端缓存。clickhouse_connect.get_client() 构造时会与服务端握手
# (查 version/timezone 等, 数次往返); 回测里每次 get_fundamentals 都新建客户端会
# 反复握手。默认库的客户端复用单例; 显式传 database 覆盖时仍每次新建(非热点路径)。
_DEFAULT_CLIENT = None


def auth(username, password, host: str = "localhost", port: int = 8123,
         database: str = DATABASE) -> None:
    """设定 ClickHouse 连接凭证.

    后续 get_client() 使用该凭证. 与 jqdatasdk.auth 对称, 交互式用法为
    `from kedu import auth; auth("user", "pwd")`.
    """
    global _DEFAULT_CLIENT
    _CREDENTIALS.update(
        host=host,
        port=int(port),
        username=username,
        password=password,
        database=database,
    )
    _DEFAULT_CLIENT = None  # 凭证变更,丢弃旧缓存客户端
    print(f"clickhouse auth: {username}@{host}:{port}/{database}")


def auth_from_env() -> None:
    """从环境变量显式拉起 auth.

    适用于脚本或 pm2, 配合 `uv run --env-file .env`. 缺少 CLICKHOUSE_USER 或
    CLICKHOUSE_PASSWORD 时 raise, 绝不静默回退默认值.
    """
    try:
        username = os.environ["CLICKHOUSE_USER"]
        password = os.environ["CLICKHOUSE_PASSWORD"]
    except KeyError as e:
        raise RuntimeError(
            f"缺少环境变量 {e.args[0]};请用 `uv run --env-file .env ...` 或显式 kedu.auth(...)"
        ) from None
    auth(
        username,
        password,
        host=os.getenv("CLICKHOUSE_HOST", "localhost"),
        port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
        database=os.getenv("CLICKHOUSE_DATABASE", DATABASE),
    )


def get_client(database: str | None = None):
    """返回 clickhouse_connect 客户端.

    须先调用 auth(), 否则 raise. database 可覆盖认证时设定的库名.
    默认库(database is None)复用缓存的单例客户端以省去重复握手;
    显式覆盖 database 时每次新建(非回测热点路径)。
    """
    if not _CREDENTIALS:
        raise RuntimeError(
            "尚未认证 ClickHouse:请先调用 kedu.auth(username, password) "
            "(脚本可用 kedu.auth_from_env())"
        )
    if database is not None:
        cfg = dict(_CREDENTIALS)
        cfg["database"] = database
        return clickhouse_connect.get_client(**cfg)

    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = clickhouse_connect.get_client(**_CREDENTIALS)
    return _DEFAULT_CLIENT


def query_df(client, sql: str):
    """查询 ClickHouse 并返回 DataFrame.

    使用 ClickHouse 原生 Arrow 列式格式再转 pandas, 避免行式反序列化成为大查询瓶颈.
    date_as_object=False 让日期列保持 datetime64, 与旧 query_df 行为一致.
    """
    return client.query_arrow(sql).to_pandas(date_as_object=False)


def quote_ident(value: str) -> str:
    """返回 ClickHouse 反引号标识符."""
    return "`" + value.replace("`", "``") + "`"
