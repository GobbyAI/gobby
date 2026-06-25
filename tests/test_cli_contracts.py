from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from gobby.code_index.gcode_gateway import GcodeGateway
from gobby.gwiki_gateway import GwikiGateway
from gobby.mcp_proxy.tools.wiki import create_wiki_registry

CONTRACT_DIR = Path(__file__).parent / "contracts"
CLI_CONTRACT_TOOLS = ("gcode", "gwiki")
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
    return Path(__file__).resolve().parents[2] / "gobby-cli"


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
    return {part for part in argv if part.startswith("--")}


def _json_keys(contract: dict[str, Any], command_name: str) -> set[str]:
    return set(_command(contract, command_name)["json_output_keys"])


class RecordingGwikiGateway(GwikiGateway):
    def __init__(self) -> None:
        super().__init__(
            binary="gwiki",
            project_root="/tmp/project",
            timeout_seconds=30.0,
        )
        self.argv_by_command: dict[str, list[str]] = {}

    async def _run_command(
        self,
        command_name: str,
        argv: Sequence[str],
    ) -> tuple[bytes, str] | dict[str, Any]:
        self.argv_by_command[command_name] = list(argv)
        payload = {"command": command_name, "scope": {"kind": "project", "id": "demo"}}
        if command_name == "health":
            payload = {"command": "health", "root": "/tmp/project", "text_path": "health.md"}
        return json.dumps(payload).encode(), ""


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
    ) -> tuple[bytes, bytes]:
        command_key = " ".join(command[1:3]) if command[1] in {"graph", "vector"} else command[1]
        self.argv_by_command[command_key] = list(command)
        return json.dumps({"command": command_key}).encode(), b""


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gwiki_gateway_argv_conforms_to_vendored_contract() -> None:
    contract = _contract("gwiki")
    gateway = RecordingGwikiGateway()
    calls: list[tuple[str, str, Callable[[], Awaitable[dict[str, Any]]]]] = [
        ("status", "status", gateway.status),
        ("index", "index", gateway.index),
        ("search", "search", lambda: gateway.search("ownership", limit=5, token_budget=2048)),
        (
            "ask",
            "ask",
            lambda: gateway.ask(
                "ownership",
                llm=True,
                ai="direct",
                require_ai=True,
                token_budget=2048,
            ),
        ),
        ("read", "read", lambda: gateway.read(path="docs/wiki.md")),
        ("backlinks", "backlinks", lambda: gateway.backlinks("Home")),
        ("ingest_file", "ingest-file", lambda: gateway.ingest_file("notes.md")),
        ("ingest_url", "ingest-url", lambda: gateway.ingest_url(["https://example.com"])),
        ("collect", "collect", lambda: gateway.collect("inbox")),
        ("compile", "compile", lambda: gateway.compile("/tmp/out.md")),
        ("audit", "audit", gateway.audit),
        ("trust", "trust", gateway.trust),
        ("health", "health", gateway.health),
        ("sources", "sources", gateway.sources),
        (
            "remove_source",
            "remove-source",
            lambda: gateway.remove_source("src-1", dry_run=True, yes=False, keep_asset=True),
        ),
        ("refresh", "refresh", lambda: gateway.refresh(source_ids=["src-1"], dry_run=True)),
        (
            "sync_sessions",
            "sync-sessions",
            lambda: gateway.sync_sessions(archive_dir="/tmp/sessions", limit=10),
        ),
    ]

    for _command_name, _cli_name, call in calls:
        await call()

    assert _command(contract, "index")["daemon_consumed"] is True
    assert gateway.argv_by_command["index"] == [
        "gwiki",
        "index",
        "--project",
        "/tmp/project",
        "--format",
        "json",
    ]

    for command_name, cli_name, _call in calls:
        command_contract = _command(contract, cli_name)
        assert command_contract["daemon_consumed"] is True
        argv = gateway.argv_by_command[command_name]
        assert argv[0] == "gwiki"
        assert argv[1] == cli_name
        assert "--scope" not in argv
        assert _observed_flags(argv) <= _allowed_gateway_flags(contract, cli_name)
        assert "--format" in argv
        if cli_name == "search":
            assert "--token-budget" in _observed_flags(argv)
        if cli_name == "ask":
            assert {"--llm", "--ai", "--require-ai", "--token-budget"} <= _observed_flags(argv)
        if cli_name == "health":
            assert "--project" not in argv
        else:
            assert "--project" in argv


@pytest.mark.unit
def test_gwiki_setup_remains_cli_owned_outside_daemon_surface() -> None:
    contract = _contract("gwiki")
    commands_by_name = {command["name"]: command for command in contract["commands"]}

    assert commands_by_name["index"]["daemon_consumed"] is True
    assert "setup" not in commands_by_name
    assert not hasattr(GwikiGateway, "setup")


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
        assert _observed_flags(argv) <= _allowed_gateway_flags(contract, cli_name)
        assert "--format" in argv
        if cli_name == "graph clear":
            assert "--project-id" in argv
            assert "--project" not in argv
        else:
            assert "--project" in argv


@pytest.mark.unit
def test_wiki_mcp_tools_are_backed_by_documented_gwiki_commands() -> None:
    contract = _contract("gwiki")
    daemon_commands = {
        command["name"] for command in contract["commands"] if command["daemon_consumed"]
    }
    tool_to_command = {
        "wiki_search": "search",
        "wiki_ask": "ask",
        "wiki_read": "read",
        "wiki_attach": "ingest-file",
        "wiki_ingest": "ingest-url",
        "wiki_compile": "compile",
        "wiki_audit": "audit",
        "wiki_trust": "trust",
        "wiki_health": "health",
        "wiki_list_sources": "sources",
        "wiki_remove_source": "remove-source",
        "wiki_sync_sessions": "sync-sessions",
    }

    registry = create_wiki_registry(db=None)
    tool_names = {tool["name"] for tool in registry.list_tools()}

    assert set(tool_to_command) <= tool_names
    assert set(tool_to_command.values()) <= daemon_commands
    assert "wiki_research" not in tool_names
    assert "research" not in daemon_commands


@pytest.mark.unit
def test_gwiki_contract_documents_daemon_parsed_keys() -> None:
    contract = _contract("gwiki")

    assert contract["contract_version"] == 7
    assert {"changed_paths", "citations", "raw_path", "source_path", "path"} <= _json_keys(
        contract, "ingest-file"
    )
    assert {"changed_paths", "citations", "raw_path", "raw_paths", "source_path"} <= _json_keys(
        contract, "ingest-url"
    )
    assert {"path", "raw_path", "source_path"} <= _json_keys(contract, "sources")
    assert {
        "hits",
        "sources",
        "code_citations",
        "evidence",
        "prompt_token_budget",
        "prompt_tokens_estimated",
        "warnings",
    } <= _json_keys(contract, "ask")
    assert "related_pages" not in _json_keys(contract, "ask")
    assert {"results", "code_citations", "snippet", "source_path", "result_type"} <= _json_keys(
        contract, "search"
    )
    assert {"changed_paths"} <= _json_keys(contract, "refresh")
    assert {"archive_dir", "accepted", "indexed"} <= _json_keys(contract, "sync-sessions")
    assert {
        "trust_status",
        "runtime",
        "services",
        "index_counts",
        "degradations",
        "freshness",
        "audit_state",
        "audit_summary",
        "link_summary",
        "graph_metrics",
        "health_summary",
    } <= _json_keys(contract, "trust")
    assert {"root", "text_path"} <= _json_keys(contract, "health")


@pytest.mark.unit
def test_gcode_contract_covers_daemon_consumed_surface() -> None:
    contract = _contract("gcode")
    commands = {command["name"] for command in contract["commands"]}

    assert contract["contract_version"] == 2
    assert {
        "index",
        "search",
        "codewiki",
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
    assert "--project" in _flag_names(contract["global_flags"])
    assert {"project_id", "results"} <= _json_keys(contract, "search")
    assert {"project_id", "project_root", "changed_paths"} <= _json_keys(contract, "codewiki")
    assert "--ai" in _allowed_flags(contract, "codewiki")
    assert {"nodes", "links", "summary"} <= _json_keys(contract, "graph overview")
    assert {"nodes", "links", "summary"} <= _json_keys(contract, "graph file")
    assert {"nodes", "links", "summary"} <= _json_keys(contract, "graph neighbors")
    assert {"nodes", "links", "summary"} <= _json_keys(contract, "graph blast-radius")
    assert "--max-depth" in _allowed_flags(contract, "path")
    assert {"status", "project_id", "summary"} <= _json_keys(contract, "graph clear")
    assert {"status", "project_id", "summary"} <= _json_keys(contract, "graph rebuild")
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
        sources.append((f"gobby-cli source {source_path}", json.loads(source_path.read_text())))

    binary = _installed_cli_binary(tool)
    if binary is not None:
        sources.append((f"installed `{tool}` binary {binary}", _binary_contract(tool, binary)))

    return sources


def _has_gobby_cli_contract_checkout() -> bool:
    repo = _gobby_cli_repo()
    return all(
        (repo / f"crates/{tool}/contract/{tool}.contract.json").exists()
        for tool in CLI_CONTRACT_TOOLS
    )


def _has_all_cli_binaries() -> bool:
    return all(_installed_cli_binary(tool) is not None for tool in CLI_CONTRACT_TOOLS)


def _missing_external_cli_contract_sources() -> bool:
    return not (_has_gobby_cli_contract_checkout() or _has_all_cli_binaries())


@pytest.mark.unit
def test_real_cli_contract_sources_include_binary_when_source_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sibling gobby-cli checkout must not skip installed binary verification."""
    tool = "gwiki"
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
        (f"gobby-cli source {source_path}", source_contract),
        (f"installed `{tool}` binary {binary}", binary_contract),
    ]
    assert calls == [(tool, binary)]


@pytest.mark.integration
@pytest.mark.skipif(
    _missing_external_cli_contract_sources(),
    reason="requires sibling gobby-cli checkout or installed gcode and gwiki binaries",
)
@pytest.mark.parametrize("tool", ["gcode", "gwiki"])
def test_vendored_cli_contract_matches_real_cli(tool: str) -> None:
    """The vendored contract must match the real gobby-cli contract.

    Every available source is checked. A sibling checkout and an installed
    binary can catch different drift modes, so source presence must not skip
    live binary verification.
    """
    vendored = _contract(tool)
    sources = _real_cli_contract_sources(tool)

    assert sources
    for source_name, real_contract in sources:
        assert vendored == real_contract, f"vendored {tool} contract drifted from {source_name}"
