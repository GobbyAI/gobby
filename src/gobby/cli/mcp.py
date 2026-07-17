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
    Run stdio MCP server for AI CLI integration.

    This command starts a stdio-based MCP server that:
    - Auto-starts the daemon if not running
    - Provides daemon lifecycle tools (start/stop/restart)
    - Proxies all HTTP MCP tools from the daemon

    Example usage:
      claude mcp add --transport stdio gobby -- gobby mcp-server
    """
    from gobby.mcp_proxy.stdio import main as mcp_main

    # Run the stdio MCP server
    try:
        asyncio.run(mcp_main())
    except Exception as e:
        logger.error("MCP server failed: %s", e)
        # Use simple exit code to avoid scary tracebacks for common errors
        import sys

        sys.exit(1)
