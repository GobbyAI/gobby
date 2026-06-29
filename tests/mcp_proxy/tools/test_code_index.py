"""Tests for the internal ``gobby-index`` registry (read-only gcode shim).

Covers the read-only tool surface (only ``GCODE_READONLY_TOOLS`` exposed, no
mutators), project resolution from an absolute path, full ``registry.call``
dispatch into the ``gcode`` argv (no shell), and graceful error results when no
project resolves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gobby.ai import _tool_chat_tools
from gobby.ai._tool_chat_tools import GCODE_READONLY_TOOLS
from gobby.mcp_proxy.tools.code_index import create_index_registry
from gobby.mcp_proxy.tools.internal import InternalRegistryManager


def _registry() -> Any:
    return create_index_registry(db=None, default_project_id=None)


def test_registry_exposes_only_readonly_gcode_tools() -> None:
    registry = _registry()

    assert registry.name == "gobby-index"
    names = {tool["name"] for tool in registry.list_tools()}

    expected = {f"gcode_{sub.replace('-', '_')}" for sub in GCODE_READONLY_TOOLS}
    assert names == expected
    # No mutating subcommand is reachable through this surface.
    for mutator in ("gcode_index", "gcode_graph", "gcode_prune", "gcode_invalidate"):
        assert mutator not in names


def test_tool_schema_includes_args_and_project() -> None:
    registry = _registry()

    schema = registry.get_schema("gcode_search")
    assert schema is not None
    props = schema["inputSchema"]["properties"]
    assert "args" in props and props["args"]["type"] == "array"
    assert "project" in props and props["project"]["type"] == "string"


async def test_call_dispatches_readonly_gcode_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    async def fake_run_argv(argv: list[str], *, cwd: str, timeout: float, byte_cap: int) -> str:
        captured["argv"] = argv
        captured["cwd"] = cwd
        return "SEARCH RESULTS"

    monkeypatch.setattr(_tool_chat_tools, "run_argv", fake_run_argv)
    registry = _registry()

    result = await registry.call(
        "gcode_search", {"args": ["auth", "crates/ghook"], "project": str(tmp_path)}
    )

    assert result["output"] == "SEARCH RESULTS"
    assert result["project_root"] == str(tmp_path.resolve())
    # argv is gcode + subcommand + raw args, executed in the resolved repo root.
    assert captured["argv"] == ["gcode", "search", "auth", "crates/ghook"]
    assert captured["cwd"] == str(tmp_path.resolve())


async def test_call_with_no_args_runs_bare_subcommand(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    async def fake_run_argv(argv: list[str], *, cwd: str, timeout: float, byte_cap: int) -> str:
        captured["argv"] = argv
        return "OUTLINE"

    monkeypatch.setattr(_tool_chat_tools, "run_argv", fake_run_argv)
    registry = _registry()

    result = await registry.call("gcode_repo_outline", {"project": str(tmp_path)})

    assert result["output"] == "OUTLINE"
    assert captured["argv"] == ["gcode", "repo-outline"]


async def test_call_without_project_returns_error_not_raise() -> None:
    registry = _registry()

    result = await registry.call("gcode_search", {"args": ["x"]})

    assert "error" in result
    assert "project" in result["error"].lower()
    assert "output" not in result


async def test_call_rejects_shell_metacharacters_in_args(tmp_path: Path) -> None:
    registry = _registry()

    # A metacharacter must be rejected by ToolRuntime before any execution; the
    # handler surfaces it as a structured error, never runs gcode.
    result = await registry.call(
        "gcode_search", {"args": ["foo; rm -rf /"], "project": str(tmp_path)}
    )

    assert "error" in result


def test_registry_routes_through_proxy_manager() -> None:
    # The proxy lists + routes gobby-index exactly like any other internal server.
    manager = InternalRegistryManager()
    manager.add_registry(_registry())

    assert manager.is_internal("gobby-index")
    assert manager.get_registry("gobby-index") is not None
    assert "gobby-index" in {server["name"] for server in manager.list_servers()}
    # A gcode_* tool resolves back to the gobby-index server for call routing.
    assert manager.find_tool_server("gcode_search") == "gobby-index"
