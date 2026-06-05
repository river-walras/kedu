"""kedu 本地复刻聚宽 get_fundamentals / get_price / finance.run_query 等 API.

查询表面 query / income / balance / cash_flow / indicator / valuation 由本项目 vendored
(见 kedu._jqsdk, 自 jqdatasdk copy 出, 运行时不再依赖 jqdatasdk), 纯本地且无网络,
执行层走本地 ClickHouse. 报告期 STK_* 表用本地自建 SQLAlchemy 模型, 见 kedu.finance, 无需 auth.
"""
from __future__ import annotations

# vendored 的 SQLAlchemy 模型对象作为查询表面(运行时零 jqdatasdk 依赖)
from ._jqsdk import (  # noqa: F401
    query,
    income,
    balance,
    cash_flow,
    indicator,
    valuation,
)

from .db import get_client, DATABASE, auth, auth_from_env  # noqa: F401
from .fundamentals import (  # noqa: F401
    get_fundamentals,
    get_fundamentals_continuously,
    get_history_fundamentals,
)
from .prices import get_price  # noqa: F401
from .finance import finance  # noqa: F401
from .calendar import get_trade_days, get_all_trade_days  # noqa: F401
from .securities import get_all_securities  # noqa: F401
from .extras import get_extras  # noqa: F401
from .industry import (  # noqa: F401
    get_industries,
    get_industry_stocks,
    get_history_industry,
    get_industry,
)
from .concept import (  # noqa: F401
    get_concepts,
    get_concept_stocks,
    get_concept,
)
from .index import (  # noqa: F401
    get_index_stocks,
    get_index_weights,
    get_index_valuation,
)
from .margin import (  # noqa: F401
    get_mtss,
    get_margincash_stocks,
    get_marginsec_stocks,
)

get_table_info = finance.get_table_info

__all__ = [
    "query",
    "income",
    "balance",
    "cash_flow",
    "indicator",
    "valuation",
    "get_fundamentals",
    "get_fundamentals_continuously",
    "get_history_fundamentals",
    "get_price",
    "get_table_info",
    "get_trade_days",
    "get_all_trade_days",
    "get_all_securities",
    "get_extras",
    "get_industries",
    "get_industry_stocks",
    "get_history_industry",
    "get_industry",
    "get_concepts",
    "get_concept_stocks",
    "get_concept",
    "get_index_stocks",
    "get_index_weights",
    "get_index_valuation",
    "get_mtss",
    "get_margincash_stocks",
    "get_marginsec_stocks",
    "finance",
    "get_client",
    "auth",
    "auth_from_env",
    "DATABASE",
]
