"""
Sector Rotation MCP Server.

Thin MCP wrapper around sector_rotation_core.scan_sector_rotation — see
that module for the actual scan logic and data-honesty notes. This file
just exposes it as a stdio MCP tool for Claude to call live.

Run as a stdio MCP server:
    python server.py
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent.parent))  # nse-trading-bot/

from mcp.server.fastmcp import FastMCP
from sector_rotation_core import scan_sector_rotation as _scan_sector_rotation

mcp = FastMCP("sector-rotation")


@mcp.tool()
def scan_sector_rotation(top_n: int = 3, stocks_per_sector: int = 3) -> dict:
    """
    Rank NSE sector indices by 5-day momentum and relative volume to find
    which sectors are rotating into strength right now, then rank a few
    representative large-cap stocks within the top sectors the same way.

    All figures are fetched live from yfinance at call time — nothing is
    cached or pre-computed. Returns data_as_of per instrument so the caller
    can verify freshness (yfinance NSE data is EOD/15-min-delayed, not
    tick-level). This is an educational momentum/volume screen, not
    investment advice — always verify live price in your broker terminal
    before placing any order.
    """
    return _scan_sector_rotation(top_n=top_n, stocks_per_sector=stocks_per_sector)


if __name__ == "__main__":
    mcp.run(transport="stdio")
