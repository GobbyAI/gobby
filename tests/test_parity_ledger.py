"""Inventory and drift checks for the generated Python-to-Rust parity ledger."""

from __future__ import annotations

import json
from pathlib import Path

import click
import pytest
from fastapi import FastAPI
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send

from scripts.generate_parity_ledger import (
    LEDGER_PATH,
    ParityStatus,
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
        "MCP HTTP gobby:call_tool",
        "MCP stdio gobby:init_project",
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
        "CLI gobby ready": ParityStatus("delegated"),
        "HTTP GET /old": ParityStatus("dropped by decision #123"),
        "MCP HTTP gobby:call_tool": ParityStatus("proxied"),
    }
    rendered = render_ledger(
        {"WS message new", "CLI gobby ready", "MCP HTTP gobby:call_tool"}, statuses
    )
    assert rendered.endswith(
        b"| `CLI gobby ready` | delegated |  |\n"
        b"| `HTTP GET /old` | dropped by decision #123 |  |\n"
        b"| `MCP HTTP gobby:call_tool` | proxied |  |\n"
        b"| `WS message new` | **unclassified** |  |\n"
    )
    assert b"Unclassified: 1." in rendered


def test_absent_mapping_key_fails_instead_of_silently_hiding_a_rename() -> None:
    with pytest.raises(ValueError, match="Mapping references absent surfaces: HTTP GET /old"):
        render_ledger({"HTTP GET /new"}, {"HTTP GET /old": ParityStatus("delegated")})


@pytest.mark.parametrize("status", ["native", "unclassified", "dropped", "dropped by decision", 1])
def test_mapping_rejects_invalid_status_or_unrecorded_drop(tmp_path: Path, status: object) -> None:
    mapping = tmp_path / "statuses.json"
    mapping.write_text(json.dumps({"HTTP GET /health": status}))
    with pytest.raises(ValueError, match="Invalid parity status for HTTP GET /health"):
        load_statuses(tmp_path)


def test_mapping_accepts_each_documented_status(tmp_path: Path) -> None:
    mapping = tmp_path / "statuses.json"
    mapping.write_text(
        '{"HTTP GET /a": "proxied", "HTTP GET /b": "delegated", '
        '"HTTP GET /c": "dropped by decision #123"}'
    )
    assert load_statuses(tmp_path) == {
        "HTTP GET /a": ParityStatus("proxied"),
        "HTTP GET /b": ParityStatus("delegated"),
        "HTTP GET /c": ParityStatus("dropped by decision #123"),
    }


def test_mapping_merges_all_shards_in_stable_order(tmp_path: Path) -> None:
    (tmp_path / "z.json").write_text('{"HTTP GET /z": "delegated"}')
    (tmp_path / "a.json").write_text('{"HTTP GET /b": "proxied", "HTTP GET /a": "delegated"}')
    assert list(load_statuses(tmp_path).items()) == [
        ("HTTP GET /a", ParityStatus("delegated")),
        ("HTTP GET /b", ParityStatus("proxied")),
        ("HTTP GET /z", ParityStatus("delegated")),
    ]


def test_duplicate_surface_names_both_shards(tmp_path: Path) -> None:
    (tmp_path / "tasks.json").write_text('{"HTTP GET /api/tasks": "proxied"}')
    (tmp_path / "remainder.json").write_text('{"HTTP GET /api/tasks": "delegated"}')
    with pytest.raises(ValueError, match="Duplicate parity surface") as error:
        load_statuses(tmp_path)
    assert "tasks.json" in str(error.value)
    assert "remainder.json" in str(error.value)
    assert "HTTP GET /api/tasks" in str(error.value)


def test_missing_shards_fail_instead_of_unclassifying_everything(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No parity status shards"):
        load_statuses(tmp_path)


def test_object_and_string_entries_render_notes_without_changing_statuses(tmp_path: Path) -> None:
    entries = {
        "HTTP GET /a": {"status": "delegated", "note": "crates/gdaemon | route\r\ntest"},
        "HTTP GET /b": "proxied",
        "HTTP GET /removed": {
            "status": "dropped by decision #123",
            "note": "Recorded decision #123",
        },
    }
    (tmp_path / "statuses.json").write_text(json.dumps(entries))

    statuses = load_statuses(tmp_path)
    rendered = render_ledger({"HTTP GET /a", "HTTP GET /b"}, statuses)

    assert statuses["HTTP GET /a"] == ParityStatus("delegated", "crates/gdaemon | route\r\ntest")
    assert statuses["HTTP GET /b"] == ParityStatus("proxied")
    assert b"| Public surface | Rust status | Notes |" in rendered
    assert rendered.endswith(
        b"| `HTTP GET /a` | delegated | crates/gdaemon \\| route<br>test |\n"
        b"| `HTTP GET /b` | proxied |  |\n"
        b"| `HTTP GET /removed` | dropped by decision #123 | Recorded decision #123 |\n"
    )
    assert b"Unclassified: 0." in rendered


def test_note_backslashes_cannot_unescape_a_table_separator() -> None:
    rendered = render_ledger(
        {"HTTP GET /a"}, {"HTTP GET /a": ParityStatus("proxied", "Rust \\| work")}
    )

    assert rendered.endswith(b"| `HTTP GET /a` | proxied | Rust \\\\\\| work |\n")


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"status": "proxied"},
        {"note": "evidence"},
        {"status": "delegated", "note": 123},
        {"status": "delegated", "note": None},
        {"status": "delegated", "note": "evidence", "extra": "typo"},
    ],
)
def test_mapping_rejects_malformed_objects(tmp_path: Path, entry: object) -> None:
    (tmp_path / "statuses.json").write_text(json.dumps({"HTTP GET /a": entry}))

    with pytest.raises(ValueError, match="Invalid parity entry for HTTP GET /a in statuses.json"):
        load_statuses(tmp_path)


@pytest.mark.parametrize("status", [None, 123, "unknown", "dropped by decision #0"])
def test_object_entries_use_the_same_status_validation(tmp_path: Path, status: object) -> None:
    (tmp_path / "statuses.json").write_text(
        json.dumps({"HTTP GET /a": {"status": status, "note": "evidence"}})
    )

    with pytest.raises(ValueError, match="Invalid parity status for HTTP GET /a"):
        load_statuses(tmp_path)


def test_raw_asgi_mount_fails_instead_of_disappearing() -> None:
    async def endpoint(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("Inventory must not invoke the ASGI application")

    with pytest.raises(ValueError, match="Cannot enumerate raw ASGI mount: /proxy/raw"):
        http_surfaces([Mount("/raw", app=endpoint)], prefix="/proxy")
