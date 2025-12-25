"""
MCP server commands.
"""

import asyncio
import logging

import click

logger = logging.getLogger(__name__)


@click.command()
@click.pass_context
def mcp_server(ctx: click.Context) -> None:
    """
    Run stdio MCP server for Claude Code integration.

    This command starts a stdio-based MCP server that:
    - Auto-starts the daemon if not running
    - Provides daemon lifecycle tools (start/stop/restart)
    - Proxies all HTTP MCP tools from the daemon

    Usage with Claude Code:
      claude mcp add --transport stdio gobby-daemon -- gobby mcp-server
    """
    from gobby.mcp_proxy.stdio import main as mcp_main

    # Run the stdio MCP server
    asyncio.run(mcp_main())
