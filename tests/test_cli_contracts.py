from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from gobby.code_index.gcode_gateway import GcodeGateway

CONTRACT_DIR = Path(__file__).parent / "contracts"
CLI_CONTRACT_TOOLS = ("gcode",)
pytestmark = pytest.mark.unit


def _contract(tool: str) -> dict[str, Any]:
    with (CONTRACT_DIR / f"{tool}.contract.json").open() as handle:
        value = json.load(handle)
    assert isinstance(value, dict)
    return value


def _gobby_cli_repo() -> Path:
    configured = os.environ.get("GOBBY_CLI_REPO")
    if configured:
        return Path(configured)
    root = Path(__file__).resolve().parents[1]
    if (root / "crates/gcode/Cargo.toml").exists():
        return root
    return root.parent / "gobby-cli"


def _command(contract: dict[str, Any], name: str) -> dict[str, Any]:
    for command in contract["commands"]:
        if command["name"] == name:
            assert isinstance(command, dict)
            return command
    raise AssertionError(f"{contract['tool']} contract is missing command {name}")


def _flag_names(flags: list[dict[str, Any]]) -> set[str]:
    return {flag["name"] for flag in flags}


def _allowed_flags(contract: dict[str, Any], command_name: str) -> set[str]:
    flags = _flag_names(contract["global_flags"])
    scope = contract.get("scope")
    if isinstance(scope, dict):
        flags |= _flag_names(scope["flags"])
    flags |= _flag_names(_command(contract, command_name)["flags"])
    return flags


def _allowed_gateway_flags(contract: dict[str, Any], command_name: str) -> set[str]:
    return _allowed_flags(contract, command_name)


def _observed_flags(argv: list[str]) -> set[str]:
    return {part.partition("=")[0] for part in argv if part.startswith("--")}


def _json_keys(contract: dict[str, Any], command_name: str) -> set[str]:
    return set(_command(contract, command_name)["json_output_keys"])


class RecordingGcodeGateway(GcodeGateway):
    def __init__(self) -> None:
        super().__init__(
            binary="gcode",
            timeout_seconds=30.0,
        )
        self._checked_version = "999.0.0"
        self.argv_by_command: dict[str, list[str]] = {}

    async def _run_command(
        self,
        command: Sequence[str],
        *,
        timeout: float | None = None,
        check_version: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> tuple[bytes, bytes]:
        command_key = " ".join(command[1:3]) if command[1] in {"graph", "vector"} else command[1]
        self.argv_by_command[command_key] = list(command)
        return json.dumps({"command": command_key}).encode(), b""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gcode_gateway_argv_conforms_to_vendored_contract() -> None:
    contract = _contract("gcode")
    gateway = RecordingGcodeGateway()
    project_root = Path("/tmp/project")
    calls: list[tuple[str, str, Callable[[], Awaitable[dict[str, Any]]]]] = [
        (
            "graph sync-file",
            "graph sync-file",
            lambda: gateway.graph_sync_file(project_root, "src/main.py"),
        ),
        (
            "vector sync-file",
            "vector sync-file",
            lambda: gateway.vector_sync_file(project_root, "src/main.py"),
        ),
        (
            "graph overview",
            "graph overview",
            lambda: gateway.graph_overview(project_root, limit=25),
        ),
        (
            "graph file",
            "graph file",
            lambda: gateway.graph_file(project_root, "src/main.py"),
        ),
        (
            "graph neighbors",
            "graph neighbors",
            lambda: gateway.graph_neighbors(project_root, "symbol-1", limit=12),
        ),
        (
            "graph blast-radius",
            "graph blast-radius",
            lambda: gateway.graph_blast_radius(
                project_root, symbol_id="symbol-1", depth=2, limit=9
            ),
        ),
        (
            "path",
            "path",
            lambda: gateway.symbol_path(project_root, "symbol-1", "symbol-2", 8),
        ),
        (
            "graph clear",
            "graph clear",
            lambda: gateway.graph_clear("project-1"),
        ),
        (
            "graph rebuild",
            "graph rebuild",
            lambda: gateway.graph_rebuild(project_root),
        ),
    ]

    for _command_key, _cli_name, call in calls:
        await call()

    for command_key, cli_name, _call in calls:
        command_contract = _command(contract, cli_name)
        if cli_name != "vector sync-file":
            assert command_contract["daemon_consumed"] is True
        argv = gateway.argv_by_command[command_key]
        assert argv[0] == "gcode"
        expected_parts = cli_name.split()
        assert argv[1 : 1 + len(expected_parts)] == expected_parts
        flags = _observed_flags(argv)
        assert flags <= _allowed_gateway_flags(contract, cli_name)
        assert "--format" in flags
        if cli_name == "graph clear":
            assert "--project-id" in flags
            assert "--project" not in flags
        else:
            assert "--project" in flags


@pytest.mark.unit
def test_gcode_contract_covers_daemon_consumed_surface() -> None:
    contract = _contract("gcode")
    commands = {command["name"] for command in contract["commands"]}

    assert contract["contract_version"] == 11
    assert "invalid_path_scope" in contract["error_codes"]
    assert {
        "ask",
        "index",
        "evidence",
        "search",
        "callees",
        "graph view",
        "graph sync-file",
        "vector sync-file",
        "graph overview",
        "graph file",
        "graph neighbors",
        "graph blast-radius",
        "path",
        "graph clear",
        "graph rebuild",
    } <= commands
    assert {
        "project_id",
        "project_root",
        "view",
        "seed",
        "depth",
        "incoming_truncated",
        "outgoing_truncated",
        "hint",
        "nodes",
        "edges",
        "communities",
        "mermaid",
    } <= _json_keys(contract, "graph view")
    assert {
        "total",
        "offset",
        "limit",
        "next_offset",
        "budget_exceeded",
        "results",
    } <= _json_keys(contract, "callees")
    # Ask is daemon-delegated, so the daemon parses this lifecycle payload and branches
    # on these typed lifecycle failures; contract 10 is exactly that surface. The other
    # ask_* codes are deliberately not pinned here because they never cross the
    # delegation boundary: ask_export_io is local export IO, and invalid_ask_request,
    # malformed_ask_response and ask_daemon_error describe the transport itself.
    assert {
        "run_id",
        "status",
        "current_stage",
        "answer_outcome",
        "typed_error",
        "deadline_at",
        "artifact_manifest",
    } <= _json_keys(contract, "ask")
    assert {
        "ask_run_not_found",
        "ask_unauthorized",
        "ask_wait_timeout",
        "ask_wait_disconnected",
        "ask_cancelled",
        "ask_failed",
        "ask_daemon_unavailable",
    } <= set(contract["error_codes"])
    assert "codewiki" not in commands
    assert "--project" in _flag_names(contract["global_flags"])
    assert {"project_id", "results"} <= _json_keys(contract, "search")
    assert {"nodes", "links", "center"} <= _json_keys(contract, "graph overview")
    assert {"nodes", "links", "center"} <= _json_keys(contract, "graph file")
    assert {"nodes", "links", "center"} <= _json_keys(contract, "graph neighbors")
    assert {"nodes", "links", "center"} <= _json_keys(contract, "graph blast-radius")
    assert "--max-depth" in _allowed_flags(contract, "path")
    assert {"status", "project_id", "summary"} <= _json_keys(contract, "graph clear")
    assert {"status", "project_id", "summary"} <= _json_keys(contract, "graph rebuild")
    assert {"--file", "--module", "--symbol", "--min-size", "--community"} <= _allowed_flags(
        contract, "graph view"
    )
    assert _command(contract, "graph view")["positionals"] == []
    assert _command(contract, "tree")["positionals"] == [
        {"name": "PATH", "required": False, "repeatable": True}
    ]
    assert "--allow-missing-indexed-file" in _allowed_flags(contract, "graph sync-file")
    assert "--allow-missing-indexed-file" in _allowed_flags(contract, "vector sync-file")


def _installed_cli_binary(tool: str) -> Path | None:
    """Locate an installed/managed CLI binary for contract verification."""
    on_path = shutil.which(tool)
    candidates = [Path(on_path)] if on_path else []
    candidates.append(Path.home() / ".gobby" / "bin" / tool)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _binary_contract(tool: str, binary: Path) -> dict[str, Any]:
    """Return the live contract emitted by an installed CLI binary."""
    result = subprocess.run(
        [str(binary), "contract", "--format", "json"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"`{binary} contract --format json` failed (exit {result.returncode}): {result.stderr}"
    )
    value = json.loads(result.stdout)
    assert isinstance(value, dict)
    return value


def _real_cli_contract_sources(tool: str) -> list[tuple[str, dict[str, Any]]]:
    """Return every available source of truth for a real CLI contract."""
    sources: list[tuple[str, dict[str, Any]]] = []

    source_path = _gobby_cli_repo() / f"crates/{tool}/contract/{tool}.contract.json"
    if source_path.exists():
        sources.append(
            (f"Rust workspace source {source_path}", json.loads(source_path.read_text()))
        )

    binary = _installed_cli_binary(tool)
    if binary is not None:
        sources.append((f"installed `{tool}` binary {binary}", _binary_contract(tool, binary)))

    return sources


def _has_rust_workspace_contracts() -> bool:
    repo = _gobby_cli_repo()
    return all(
        (repo / f"crates/{tool}/contract/{tool}.contract.json").exists()
        for tool in CLI_CONTRACT_TOOLS
    )


def _has_all_cli_binaries() -> bool:
    return all(_installed_cli_binary(tool) is not None for tool in CLI_CONTRACT_TOOLS)


def _missing_external_cli_contract_sources() -> bool:
    return not (_has_rust_workspace_contracts() or _has_all_cli_binaries())


@pytest.mark.unit
def test_real_cli_contract_sources_include_binary_when_source_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source contract must not skip installed binary verification."""
    tool = "gcode"
    source_contract = {"tool": tool, "origin": "source"}
    binary_contract = {"tool": tool, "origin": "binary"}
    repo = tmp_path / "gobby-cli"
    source_path = repo / f"crates/{tool}/contract/{tool}.contract.json"
    source_path.parent.mkdir(parents=True)
    source_path.write_text(json.dumps(source_contract))

    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    binary = binary_dir / tool
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)

    calls: list[tuple[str, Path]] = []

    def fake_binary_contract(observed_tool: str, observed_binary: Path) -> dict[str, Any]:
        calls.append((observed_tool, observed_binary))
        return binary_contract

    monkeypatch.setenv("GOBBY_CLI_REPO", str(repo))
    # _binary_contract is mocked here; keep real tools available for subprocess setup.
    monkeypatch.setenv("PATH", os.pathsep.join([str(binary_dir), os.environ.get("PATH", "")]))
    monkeypatch.setattr(sys.modules[__name__], "_binary_contract", fake_binary_contract)

    sources = _real_cli_contract_sources(tool)

    assert sources == [
        (f"Rust workspace source {source_path}", source_contract),
        (f"installed `{tool}` binary {binary}", binary_contract),
    ]
    assert calls == [(tool, binary)]


@pytest.mark.integration
@pytest.mark.skipif(
    _missing_external_cli_contract_sources(),
    reason="requires Rust workspace contracts or installed gcode binary",
)
@pytest.mark.parametrize("tool", ["gcode"])
def test_vendored_cli_contract_matches_real_cli(tool: str) -> None:
    """The vendored contract must match the real Rust CLI contract.

    Every available source is checked. A sibling checkout and an installed
    binary can catch different drift modes, so source presence must not skip
    live binary verification.
    """
    vendored = _contract(tool)
    sources = _real_cli_contract_sources(tool)

    assert sources
    for source_name, real_contract in sources:
        assert vendored == real_contract, f"vendored {tool} contract drifted from {source_name}"
