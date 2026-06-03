from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from gobby.gwiki_gateway import GwikiGateway
from gobby.mcp_proxy.tools.wiki import create_wiki_registry

CONTRACT_DIR = Path(__file__).parent / "contracts"


def _contract(tool: str) -> dict[str, Any]:
    with (CONTRACT_DIR / f"{tool}.contract.json").open() as handle:
        value = json.load(handle)
    assert isinstance(value, dict)
    return value


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


async def test_gwiki_gateway_argv_conforms_to_vendored_contract() -> None:
    contract = _contract("gwiki")
    gateway = RecordingGwikiGateway()
    calls: list[tuple[str, str, Callable[[], Awaitable[dict[str, Any]]]]] = [
        ("status", "status", gateway.status),
        ("index", "index", gateway.index),
        ("search", "search", lambda: gateway.search("ownership", limit=5)),
        ("ask", "ask", lambda: gateway.ask("ownership", llm=True)),
        ("read", "read", lambda: gateway.read(path="docs/wiki.md")),
        ("backlinks", "backlinks", lambda: gateway.backlinks("Home")),
        ("ingest_file", "ingest-file", lambda: gateway.ingest_file("notes.md")),
        ("ingest_url", "ingest-url", lambda: gateway.ingest_url(["https://example.com"])),
        ("collect", "collect", lambda: gateway.collect("inbox")),
        ("research", "research", lambda: gateway.research("freshness")),
        ("compile", "compile", lambda: gateway.compile("/tmp/out.md")),
        ("audit", "audit", gateway.audit),
        ("health", "health", gateway.health),
        ("sources", "sources", gateway.sources),
        (
            "remove_source",
            "remove-source",
            lambda: gateway.remove_source("src-1", dry_run=True, yes=False, keep_asset=True),
        ),
        ("refresh", "refresh", lambda: gateway.refresh(source_ids=["src-1"], dry_run=True)),
    ]

    for _command_name, _cli_name, call in calls:
        await call()

    for command_name, cli_name, _call in calls:
        command_contract = _command(contract, cli_name)
        assert command_contract["daemon_consumed"] is True
        argv = gateway.argv_by_command[command_name]
        assert argv[0] == "gwiki"
        assert argv[1] == cli_name
        assert "--scope" not in argv
        assert _observed_flags(argv) <= _allowed_flags(contract, cli_name)
        assert "--format" in argv
        if cli_name == "health":
            assert "--project" not in argv
        else:
            assert "--project" in argv


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
        "wiki_health": "health",
        "wiki_list_sources": "sources",
        "wiki_remove_source": "remove-source",
    }

    registry = create_wiki_registry(db=None)
    tool_names = {tool["name"] for tool in registry.list_tools()}

    assert set(tool_to_command) <= tool_names
    assert set(tool_to_command.values()) <= daemon_commands


def test_gwiki_contract_documents_daemon_parsed_keys() -> None:
    contract = _contract("gwiki")

    assert {"changed_paths", "citations", "raw_path", "source_path", "path"} <= _json_keys(
        contract, "ingest-file"
    )
    assert {"changed_paths", "citations", "raw_path", "raw_paths", "source_path"} <= _json_keys(
        contract, "ingest-url"
    )
    assert {"path", "raw_path", "source_path"} <= _json_keys(contract, "sources")
    assert {"hits", "related_pages", "sources", "warnings"} <= _json_keys(contract, "ask")
    assert {"changed_paths"} <= _json_keys(contract, "refresh")
    assert {"root", "text_path"} <= _json_keys(contract, "health")


def test_gcode_contract_covers_daemon_consumed_surface() -> None:
    contract = _contract("gcode")
    commands = {command["name"] for command in contract["commands"]}

    assert contract["contract_version"] == 1
    assert {"index", "search", "codewiki"} <= commands
    assert "--project" in _flag_names(contract["global_flags"])
    assert {"project_id", "results"} <= _json_keys(contract, "search")
    assert {"project_id", "project_root", "changed_paths"} <= _json_keys(contract, "codewiki")
