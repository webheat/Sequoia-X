"""MCP server：给外部 Agent / Claude 直接调的 stdio 工具。

启动::

    python -m sequoia_x.api.mcp_server
    # 或
    mcp run sequoia_x.api.mcp_server:mcp    # 走 mcp CLI 即可

提供 5 个 tool：
- ``list_symbols``           全部股票代码
- ``get_symbol_meta``        单只元信息
- ``get_ohlcv``              单只日 K（支持 start/end/limit/order）
- ``get_cross_section``      多只 × 单日
- ``get_latest``             多只最新一行
- ``get_db_stats``           数据库总览

跟 HTTP 接口共享 ``sequoia_x.api.db``，数据一致。

mcp 2.x：``FastMCP`` 已改名为 ``MCPServer``，``@tool()`` 装饰器签名同 v1。
"""

from __future__ import annotations

import os

from mcp.server.mcpserver import MCPServer

from sequoia_x.api import db

mcp = MCPServer(
    name="sequoia-x-data",
    instructions=(
        "Sequoia-X A 股日 K 数据服务。后复权，数据源 baostock。\n"
        "symbol 接受 6 位数字或带 sh./sz./bj. 前缀；日期格式 YYYY-MM-DD。\n"
        "只读，写不进 DB。"
    ),
)

# 启动时取一次 db_path；用 .env 或 fallback 默认
try:
    from sequoia_x.core.config import get_settings

    DB_PATH = get_settings().db_path
except Exception:
    DB_PATH = os.environ.get("DB_PATH", "data/sequoia_v2.db")


@mcp.tool()
def list_symbols() -> list[str]:
    """返回本地有数据的全部 A 股代码。"""
    return db.list_symbols(DB_PATH)


@mcp.tool()
def get_db_stats() -> dict:
    """数据库总览：股票数、行数、覆盖起止日期。"""
    return db.db_stats(DB_PATH)


@mcp.tool()
def get_symbol_meta(symbol: str) -> dict:
    """单只股票元信息：起止日期、记录数。

    Args:
        symbol: 6 位代码，可带 sh./sz./bj. 前缀
    """
    meta = db.get_symbol_meta(DB_PATH, symbol)
    if meta is None:
        return {"symbol": db.normalize_symbol(symbol), "exists": False}
    return {**meta, "exists": True}


@mcp.tool()
def get_ohlcv(
    symbol: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = 500,
    order: str = "asc",
) -> list[dict]:
    """单只股票日 K。

    Args:
        symbol: 6 位代码（必填）
        start: 起始日期 YYYY-MM-DD（含），可选
        end: 结束日期 YYYY-MM-DD（含），可选
        limit: 最多返回行数，1..5000，默认 500
        order: ``asc`` 或 ``desc``，默认升序
    """
    return db.query_ohlcv(
        DB_PATH, symbol,
        start=start, end=end,
        limit=limit, offset=0, order=order,
    )


@mcp.tool()
def get_cross_section(symbols: list[str], date: str) -> list[dict]:
    """横截面：多只股票 × 单一日期。

    Args:
        symbols: 1..200 只代码
        date: 目标日期 YYYY-MM-DD
    """
    return db.cross_section(DB_PATH, symbols, date)


@mcp.tool()
def get_latest(symbols: list[str]) -> list[dict]:
    """批量取每只股票的最新一行。最多 500 只。"""
    return db.latest_for_symbols(DB_PATH, symbols)


if __name__ == "__main__":
    mcp.run()  # 默认 stdio
