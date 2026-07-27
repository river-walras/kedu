"""kedu 的 MCP server(分层混合: 高频 API 直出 tool + 反射 dispatcher 兜长尾).

启动:
    uv run --extra mcp --env-file .env python -m kedu.mcp_server                 # stdio
    uv run --extra mcp --env-file .env python -m kedu.mcp_server --transport http

设计取舍见 kedu.mcp_server.server / _render / _dsl 的模块 docstring。
"""

from __future__ import annotations

from .server import build_server, main

__all__ = ["build_server", "main"]
