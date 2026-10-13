"""Inventory and drift checks for the generated Python-to-Rust parity ledger."""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from fastapi import FastAPI
from starlette.routing import Route

from scripts.generate_parity_ledger import (
    LEDGER_PATH,
    cli_verbs,
    collect_surfaces,
    http_surfaces,
    load_statuses,
    render_ledger,
)

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def surfaces() -> set[str]:
    return collect_surfaces()


def test_committed_ledger_matches_current_public_inventory(surfaces: set[str]) -> None:
    """Any inventory, mapping or generator drift must update the committed document."""
    assert render_ledger(surfaces, load_statuses()) == LEDGER_PATH.read_bytes()


def test_inventory_contains_all_four_protocols_and_terminal_flip(surfaces: set[str]) -> None:
    assert {
        "HTTP POST /api/auth/login",
        "HTTP * /mcp",
        "WS transport /ws",
        "WS message chat_message",
        "WS message terminal_create",
        "MCP gobby:call_tool",
        "MCP gobby-tasks:get_task",
        "MCP gobby-workspaces:read_pane",
        "CLI gobby status",
        "CLI gobby tasks list",
        "Gate native terminal default flip #21357",
    } <= surfaces


def test_route_inventory_detects_new_methods_and_mounts() -> None:
    def endpoint() -> dict[str, bool]:
        return {"ok": True}

    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    child = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    child.add_api_route("/fresh", endpoint, methods=["GET", "POST"])
    app.mount("/nested", child)
    assert http_surfaces(app.routes) == {"HTTP GET /nested/fresh", "HTTP POST /nested/fresh"}


def test_route_inventory_keeps_unrestricted_asgi_endpoints() -> None:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    route = Route("/mcp", endpoint=app)
    assert http_surfaces([route]) == {"HTTP * /mcp"}


def test_cli_inventory_includes_nested_verbs_and_aliases_without_running_callbacks() -> None:
    def callback() -> None:
        raise AssertionError("Inventory must not invoke CLI callbacks")

    root = click.Group("gobby", callback=callback)
    group = click.Group("nested", callback=callback)
    verb = click.Command("fresh", callback=callback)
    group.add_command(verb)
    group.add_command(verb, "alias")
    root.add_command(group)
    assert cli_verbs(root) == {"gobby nested", "gobby nested fresh", "gobby nested alias"}


def test_rendering_orders_rows_and_exposes_unclassified_and_recorded_drops() -> None:
    statuses = {
        "CLI gobby ready": "delegated",
        "HTTP GET /old": "dropped by decision #123",
        "MCP gobby:call_tool": "proxied",
    }
    rendered = render_ledger({"WS message new", "CLI gobby ready", "MCP gobby:call_tool"}, statuses)
    assert rendered.endswith(
        b"| `CLI gobby ready` | delegated |\n"
        b"| `HTTP GET /old` | dropped by decision #123 |\n"
        b"| `MCP gobby:call_tool` | proxied |\n"
        b"| `WS message new` | **unclassified** |\n"
    )
    assert b"Unclassified: 1." in rendered


def test_absent_mapping_key_fails_instead_of_silently_hiding_a_rename() -> None:
    with pytest.raises(ValueError, match="Mapping references absent surfaces: HTTP GET /old"):
        render_ledger({"HTTP GET /new"}, {"HTTP GET /old": "delegated"})


@pytest.mark.parametrize("status", ["native", "unclassified", "dropped", "dropped by decision", 1])
def test_mapping_rejects_invalid_status_or_unrecorded_drop(tmp_path: Path, status: object) -> None:
    mapping = tmp_path / "statuses.json"
    mapping.write_text(json.dumps({"HTTP GET /health": status}))
    with pytest.raises(ValueError, match="Invalid parity status for HTTP GET /health"):
        load_statuses(mapping)


def test_mapping_accepts_each_documented_status(tmp_path: Path) -> None:
    mapping = tmp_path / "statuses.json"
    mapping.write_text(
        '{"HTTP GET /a": "proxied", "HTTP GET /b": "delegated", '
        '"HTTP GET /c": "dropped by decision #123"}'
    )
    assert load_statuses(mapping) == {
        "HTTP GET /a": "proxied",
        "HTTP GET /b": "delegated",
        "HTTP GET /c": "dropped by decision #123",
    }
