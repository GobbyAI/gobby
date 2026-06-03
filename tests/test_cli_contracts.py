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


class RecordingResearchGateway:
    instances: list[RecordingResearchGateway] = []

    def __init__(
        self,
        *,
        binary: str | None = None,
        project_root: str | Path | None = None,
        topic: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.project_root = project_root
        self.topic = topic
        self.calls: list[dict[str, Any]] = []
        RecordingResearchGateway.instances.append(self)

    async def research(
        self,
        query: str | None = None,
        *,
        audit: bool = False,
        source_constraints: Sequence[str] | None = None,
        max_steps: int | None = None,
        max_tokens: int | None = None,
        max_sources: int | None = None,
        ai: str | None = None,
        require_ai: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "query": query,
                "audit": audit,
                "source_constraints": list(source_constraints or ()),
                "max_steps": max_steps,
                "max_tokens": max_tokens,
                "max_sources": max_sources,
                "ai": ai,
                "require_ai": require_ai,
            }
        )
        return {
            "ok": True,
            "command": "research",
            "payload": {"status": "completed", "changed_paths": ["research.md"]},
            "stderr": "",
        }


class RecordingResearchCoordinator:
    def __init__(self, gateway: RecordingResearchGateway) -> None:
        self.gateway = gateway

    async def handle_write_result(self, result: dict[str, Any]) -> dict[str, Any]:
        handled = dict(result)
        handled["index_handoff"] = {"status": "completed"}
        return handled


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
        (
            "research",
            "research",
            lambda: gateway.research(
                "freshness",
                audit=True,
                source_constraints=["https://example.com"],
                max_steps=3,
                max_tokens=1024,
                max_sources=2,
                ai="daemon",
                require_ai=True,
            ),
        ),
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
        if cli_name == "research":
            assert {
                "--audit",
                "--source-constraint",
                "--max-steps",
                "--max-tokens",
                "--max-sources",
                "--ai",
                "--require-ai",
            } <= _observed_flags(argv)
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
        "wiki_research": "research",
        "wiki_audit": "audit",
        "wiki_health": "health",
        "wiki_list_sources": "sources",
        "wiki_remove_source": "remove-source",
    }

    registry = create_wiki_registry(db=None)
    tool_names = {tool["name"] for tool in registry.list_tools()}

    assert set(tool_to_command) <= tool_names
    assert set(tool_to_command.values()) <= daemon_commands


async def test_wiki_research_mcp_routes_d5_options_to_gateway() -> None:
    RecordingResearchGateway.instances = []
    registry = create_wiki_registry(
        db=None,
        gateway_cls=RecordingResearchGateway,
        update_coordinator_cls=RecordingResearchCoordinator,
    )

    result = await registry.call(
        "wiki_research",
        {
            "topic": "freshness",
            "query": "Fill citation gaps",
            "audit": True,
            "source_constraints": ["https://example.com"],
            "max_steps": 4,
            "max_tokens": 2048,
            "max_sources": 3,
            "ai": "direct",
            "require_ai": True,
        },
    )

    gateway = RecordingResearchGateway.instances[-1]
    assert gateway.topic == "freshness"
    assert gateway.calls == [
        {
            "query": "Fill citation gaps",
            "audit": True,
            "source_constraints": ["https://example.com"],
            "max_steps": 4,
            "max_tokens": 2048,
            "max_sources": 3,
            "ai": "direct",
            "require_ai": True,
        }
    ]
    assert result["success"] is True
    assert result["paths"]["changed_paths"] == ["research.md"]
    assert result["index_handoff"] == {"status": "completed"}


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
    assert "--ai" in _allowed_flags(contract, "codewiki")
