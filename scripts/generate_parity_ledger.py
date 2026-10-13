#!/usr/bin/env python3
"""Generate the public-surface ledger without starting services or reading live state."""

from __future__ import annotations

import argparse
import ast
import asyncio
import inspect
import json
import re
import sys
import textwrap
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import click
from mcp.server.mcpserver import MCPServer
from starlette.routing import BaseRoute, Mount, Route, WebSocketRoute

from gobby.app_context import ServiceContainer
from gobby.cli import cli
from gobby.config.app import DaemonConfig
from gobby.mcp_proxy.registries import setup_internal_registries
from gobby.mcp_proxy.server import GobbyDaemonTools, create_mcp_server
from gobby.mcp_proxy.stdio_tools import register_proxy_tools
from gobby.servers.app_factory import _register_mcp_http_route
from gobby.servers.http import HTTPServer
from gobby.servers.websocket.server import WebSocketServer

ROOT = Path(__file__).resolve().parents[1]
LEDGER_PATH = ROOT / "docs/contracts/parity-ledger.md"
MAPPING_PATH = ROOT / "docs/contracts/parity-ledger-status.json"


def websocket_messages() -> set[str]:
    """Read the actual inbound dispatch table; refuse unsupported declarations."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(WebSocketServer._handle_message)))
    tables = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Attribute) and target.attr == "_dispatch_table"
            for target in node.targets
        )
    ]
    if len(tables) != 1 or not isinstance(tables[0], ast.Dict):
        raise ValueError("WebSocket dispatch table changed; update ledger collection")
    messages: set[str] = set()
    for key in tables[0].keys:
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            raise ValueError("WebSocket dispatch keys must be literal message names")
        messages.add(key.value)
    return messages


def cli_verbs(command: click.Command, prefix: str = "gobby") -> set[str]:
    """Enumerate registered verbs, groups and aliases without invoking callbacks."""
    if not isinstance(command, click.Group):
        return set()
    verbs: set[str] = set()
    with click.Context(command) as context:
        for name in command.list_commands(context):
            child = command.get_command(context, name)
            if child is None:
                raise ValueError(f"Missing registered CLI command: {prefix} {name}")
            verb = f"{prefix} {name}"
            verbs.add(verb)
            verbs.update(cli_verbs(child, verb))
    return verbs


def http_surfaces(routes: Sequence[BaseRoute], prefix: str = "") -> set[str]:
    """Flatten the registered ASGI route table into method/path pairs."""
    surfaces: set[str] = set()
    for route in routes:
        if isinstance(route, Mount):
            surfaces.update(http_surfaces(route.routes, f"{prefix}{route.path}"))
        elif isinstance(route, WebSocketRoute):
            surfaces.add(f"WS transport {prefix}{route.path}")
        elif isinstance(route, Route):
            surfaces.update(
                f"HTTP {method} {prefix}{route.path}" for method in route.methods or {"*"}
            )
        else:
            raise ValueError(f"Unsupported ASGI route: {type(route).__name__}")
    return surfaces


def collect_surfaces() -> set[str]:
    """Build real registrations against inert service dependencies.

    No handlers or lifecycle callbacks run. Prompt lookup supplies description
    text only. Empty skill hubs avoid credential lookup without changing tool names.
    """
    config = DaemonConfig()
    config.skills.hubs = {}
    service = MagicMock()
    with patch("gobby.prompts.loader.PromptLoader.load", return_value=SimpleNamespace(content="")):
        manager = setup_internal_registries(
            config_resolver=lambda: config,
            task_manager=service,
            session_manager=service,
            db=service,
            memory_manager_resolver=lambda: service,
            metrics_manager=service,
            agent_runner=service,
            worktree_storage=service,
            clone_storage=service,
            merge_storage=service,
            merge_resolver=service,
            inter_session_message_manager=service,
            config_service_getter=lambda: service,
            workspace_manager=service,
            workspace_ops_resolver=lambda: service,
            communications_manager=service,
        )
    mcp = create_mcp_server(GobbyDaemonTools(service, 0, 0, 0.0, manager, db=None))
    server = HTTPServer(
        ServiceContainer(database=service, session_manager=None, task_manager=service),
        test_mode=True,
    )
    _register_mcp_http_route(server.app, mcp.streamable_http_app(host="127.0.0.1"))
    surfaces = http_surfaces(server.app.routes)
    surfaces.update(f"WS message {name}" for name in websocket_messages())
    surfaces.update(f"CLI {name}" for name in cli_verbs(cli))
    surfaces.update(f"MCP HTTP gobby:{tool.name}" for tool in asyncio.run(mcp.list_tools()))
    stdio: MCPServer[None] = MCPServer("gobby")
    register_proxy_tools(stdio, service)
    surfaces.update(f"MCP stdio gobby:{tool.name}" for tool in asyncio.run(stdio.list_tools()))
    for registry in manager.get_all_registries():
        surfaces.update(f"MCP {registry.name}:{tool['name']}" for tool in registry.list_tools())
    surfaces.add("Gate native terminal default flip #21357")
    return surfaces


def load_statuses(path: Path = MAPPING_PATH) -> dict[str, str]:
    """Validate classifications; dropped surfaces must name their decision."""
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("Parity status mapping must be a JSON object")
    statuses: dict[str, str] = {}
    for surface, status in data.items():
        if not isinstance(status, str) or (
            status not in {"proxied", "delegated"}
            and re.fullmatch(r"dropped by decision #[1-9][0-9]*", status) is None
        ):
            raise ValueError(f"Invalid parity status for {surface}: {status!r}")
        statuses[surface] = status
    return statuses


def render_ledger(surfaces: set[str], statuses: Mapping[str, str]) -> bytes:
    """Render stable rows and keep recorded drops after Python surface removal."""
    stale = set(statuses) - surfaces
    invalid = {
        surface for surface in stale if not statuses[surface].startswith("dropped by decision ")
    }
    if invalid:
        raise ValueError(f"Mapping references absent surfaces: {', '.join(sorted(invalid))}")
    rows = surfaces | stale
    lines = [
        "# Parity ledger",
        "",
        "Generated by `uv run python scripts/generate_parity_ledger.py`; do not edit this document.",
        "Edit `docs/contracts/parity-ledger-status.json` to classify surfaces, then regenerate.",
        "",
        "Exit rule: every row must be `delegated` or `dropped by decision <ref>` with a recorded",
        "decision. `proxied` and **unclassified** rows still block retirement. This makes",
        "#21574 Retire gobby-backend a mechanical deletion; its exit check is owned there.",
        "The native terminal default flip has its own evidence row referencing #21357",
        "Plan native runtime completion. A closed planning task alone does not prove the flip.",
        "",
        "Sources: registered FastAPI/ASGI routes (including MCP and WS transports), inbound",
        "WebSocket dispatch messages, built-in MCP registries, daemon HTTP /mcp tools,",
        "the agent-facing stdio MCP bridge (`gobby mcp-server`), and",
        "registered Click verbs (groups and aliases included). Registration uses inert services;",
        "no daemon, database connection or handler runs. Deployment-specific external MCP",
        "servers and UI asset/dev-server mounts are outside this backend inventory.",
        "`HTTP *` denotes an ASGI route with no method filter in the parent route table.",
        "Dropped rows remain as decision evidence after removal; other mapping keys must",
        "identify currently registered surfaces. Unmapped surfaces are visibly unclassified.",
        "",
        f"Public surfaces and evidence gates: {len(rows)}. Unclassified: "
        f"{sum(surface not in statuses for surface in rows)}.",
        "",
        "| Public surface | Rust status |",
        "| --- | --- |",
    ]
    for surface in sorted(rows):
        escaped = surface.replace("|", "\\|")
        lines.append(f"| `{escaped}` | {statuses.get(surface, '**unclassified**')} |")
    return ("\n".join(lines) + "\n").encode()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdout", action="store_true", help="print instead of writing the ledger")
    args = parser.parse_args()
    content = render_ledger(collect_surfaces(), load_statuses())
    if args.stdout:
        sys.stdout.buffer.write(content)
    else:
        LEDGER_PATH.write_bytes(content)


if __name__ == "__main__":
    main()
