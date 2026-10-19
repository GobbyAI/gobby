"""Node-origin deliveries skip hub-only effects (#24531, plan 2.8).

Each guard applies to a node delivery only. The provider-file and run-command
tests run the same input as a local control first, so a node failure cannot come
from a broken fixture. The worktree, rtk and found-work tests have no local
control; they assert the node outcome alone.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gobby.adapters.claude_code import _saved_mcp_result
from gobby.adapters.codex_impl.hooks_adapter import _command_execution_result
from gobby.adapters.grok import _spilled_mcp_output
from gobby.hooks import checkout_reads
from gobby.hooks.checkout_reads import delivery_scope
from gobby.hooks.event_handlers import _misc
from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.storage.definitions.rules import RuleDefinitionRow
from gobby.utils import spawn
from gobby.workflows import found_work_gate
from gobby.workflows.definitions import RuleEffect
from gobby.workflows.engine import proxy_hooks, run_command_effects
from tests.hooks.checkout_read_fakes import NODE_MACHINE

pytestmark = pytest.mark.unit

_PROJECT_ID = "project-b"


def _event(event_type: HookEventType, cwd: str, data: dict[str, Any]) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id="node-session",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data,
        machine_id=NODE_MACHINE,
        cwd=cwd,
    )


def _worktree_owner() -> Any:
    return SimpleNamespace(logger=logging.getLogger("test_node_origin"), _worktree_manager=None)


def _rule_row(name: str, row_id: str = "row-1") -> RuleDefinitionRow:
    now = datetime.now(UTC)
    return RuleDefinitionRow(
        id=row_id,
        name=name,
        enabled=True,
        enabled_pinned=False,
        priority=100,
        definition_json={},
        source="installed",
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_node_worktree_hooks_touch_nothing_on_hub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = asyncio.get_running_loop()
    hub_effects: list[str] = []

    def resolve_project(cwd: str, *args: Any, **kwargs: Any) -> tuple[None, None, str]:
        hub_effects.append("resolve_project_context")
        return None, None, "hub resolution reached"

    def workflow_project(path: Path) -> None:
        hub_effects.append("get_workflow_project_path")
        return None

    monkeypatch.setattr(_misc, "resolve_project_context", resolve_project)
    monkeypatch.setattr(_misc, "get_workflow_project_path", workflow_project)
    owner = _worktree_owner()

    create = _event(HookEventType.WORKTREE_CREATE, str(tmp_path), {"name": "node-wt"})
    remove = _event(
        HookEventType.WORKTREE_REMOVE,
        str(tmp_path),
        {"worktree_path": str(tmp_path / "node-wt-gone")},
    )

    with delivery_scope(NODE_MACHINE, loop, str(tmp_path)):
        created = await _misc.MiscEventHandlerMixin.handle_worktree_create(owner, create)
        removed = await _misc.MiscEventHandlerMixin.handle_worktree_remove(owner, remove)

    assert created.decision == "allow"
    assert created.worktree_path is None
    assert removed.decision == "allow"
    assert hub_effects == []


@pytest.mark.asyncio
async def test_node_provider_files_are_not_read(tmp_path: Path) -> None:
    loop = asyncio.get_running_loop()

    # Claude: the saved MCP result file, named by the notice on the node.
    session_id = "claude-session"
    saved = tmp_path / session_id / "tool-results" / "mcp-readme.txt"
    saved.parent.mkdir(parents=True)
    saved.write_text("FULL CLAUDE RESULT", encoding="utf-8")
    claude_notice = (
        "Error: result (12,345 characters) exceeds maximum allowed tokens. "
        f"Output has been saved to {saved}.\n"
    )
    assert _saved_mcp_result(claude_notice, session_id) == "FULL CLAUDE RESULT"
    with delivery_scope(NODE_MACHINE, loop, str(tmp_path)):
        assert _saved_mcp_result(claude_notice, session_id) == claude_notice

    # Grok: the spilled MCP output file, named by the truncation notice on the node.
    tool_use_id = "tu-1"
    spilled = tmp_path / "mcp" / f"{tool_use_id}.json"
    spilled.parent.mkdir()
    spilled.write_text('{"full": true}', encoding="utf-8")
    grok_notice = (
        "partial output\n\n[MCP output truncated: 9000 chars Full output written to: "
        f"{spilled}. The full output is valid JSON; read it.]"
    )
    assert _spilled_mcp_output(grok_notice, tool_use_id) == '{"full": true}'
    with delivery_scope(NODE_MACHINE, loop, str(tmp_path)):
        assert _spilled_mcp_output(grok_notice, tool_use_id) == grok_notice

    # Codex: a transcript record with a matching result on the hub.
    transcript = tmp_path / "rollout.jsonl"
    record = {
        "payload": {
            "type": "item_completed",
            "item": {
                "id": "call-1",
                "type": "CommandExecution",
                "command": ["/bin/zsh", "-lc", "echo ok"],
                "exit_code": 0,
                "aggregated_output": "ok\n",
            },
        }
    }
    transcript.write_text(json.dumps(record) + "\n", encoding="utf-8")
    codex_payload = {"transcript_path": str(transcript), "tool_use_id": "call-1"}
    local_result = _command_execution_result(codex_payload)
    assert local_result is not None
    assert local_result["exit_code"] == 0
    with delivery_scope(NODE_MACHINE, loop, str(tmp_path)):
        assert _command_execution_result(codex_payload) is None


@pytest.mark.asyncio
async def test_node_shell_skips_rtk_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = asyncio.get_running_loop()
    probed: list[str] = []
    spawned: list[tuple[str, ...]] = []

    def probe_rtk(*args: Any, **kwargs: Any) -> SimpleNamespace:
        probed.append("resolve_rtk")
        return SimpleNamespace(path="/fake/rtk")

    async def spawn_rewrite(*argv: str, **kwargs: Any) -> None:
        spawned.append(argv)
        raise OSError("rtk rewrite must not spawn on a node delivery")

    monkeypatch.setattr(proxy_hooks, "resolve_rtk", probe_rtk)
    monkeypatch.setattr(spawn, "create_subprocess_exec", spawn_rewrite)
    invocation = proxy_hooks.ProxyHookInvocation(
        effect=RuleEffect(type="proxy_hook", handler="rtk-rewrite"),
        row=_rule_row("rtk-rewrite"),
    )
    event = _event(
        HookEventType.BEFORE_TOOL,
        str(tmp_path),
        {"tool_input": {"command": "git status"}},
    )

    with delivery_scope(NODE_MACHINE, loop, str(tmp_path)):
        result = await proxy_hooks.ProxyHooksMixin._run_rtk_proxy(
            proxy_hooks.ProxyHooksMixin(), invocation, event, blocking_deadline=None
        )

    assert result is False
    assert probed == []
    assert spawned == []


@pytest.mark.asyncio
async def test_node_run_command_spawns_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = asyncio.get_running_loop()
    prepared: list[tuple[str, ...]] = []
    executed: list[tuple[str, ...]] = []

    async def prepare(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        prepared.append(tuple(argv))
        return SimpleNamespace(argv=tuple(argv))

    async def execute(run: Any, *args: Any, **kwargs: Any) -> SimpleNamespace:
        executed.append(run.argv)
        return SimpleNamespace(context=None)

    owner = run_command_effects.RunCommandEffectsMixin()
    monkeypatch.setattr(owner, "_prepare_run_command", prepare)
    monkeypatch.setattr(owner, "_execute_run_command", execute)
    effect = RuleEffect(type="run_command", command=["node", "hook.mjs"], background=False)
    row = _rule_row("design-detector")
    event = _event(
        HookEventType.BEFORE_TOOL,
        str(tmp_path),
        {"tool_name": "Edit", "tool_input": {}},
    )

    # Local control: the same detector runs on the hub, so the fixture reaches execution.
    await run_command_effects.RunCommandEffectsMixin._apply_run_command(
        owner, effect, row, {"event": event}, []
    )
    assert prepared == [("node", "hook.mjs")]
    assert executed == [("node", "hook.mjs")]

    prepared.clear()
    executed.clear()
    with delivery_scope(NODE_MACHINE, loop, str(tmp_path)):
        await run_command_effects.RunCommandEffectsMixin._apply_run_command(
            owner, effect, row, {"event": event}, []
        )

    assert prepared == []
    assert executed == []


@pytest.mark.asyncio
async def test_node_found_work_terminal_evidence_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = asyncio.get_running_loop()
    node_reads: list[str] = []

    def send_spy(*args: Any, **kwargs: Any) -> None:
        node_reads.append("checkout.read")
        raise OSError("node read sent")

    monkeypatch.setattr(checkout_reads, "_send", send_spy)
    session = SimpleNamespace(
        id="node-session",
        machine_id=NODE_MACHINE,
        project_id=_PROJECT_ID,
        created_at=datetime.now(UTC),
    )
    owner = found_work_gate.FoundWorkStopAnalyzer(
        llm_service_resolver=lambda: None,
        config_resolver=lambda: None,
        session_manager=SimpleNamespace(get=lambda session_id: session),
        session_task_manager=None,
    )

    with delivery_scope(NODE_MACHINE, loop, str(tmp_path)):
        failures = await found_work_gate.FoundWorkStopAnalyzer._terminal_failures(
            owner,
            session_id="node-session",
            variables={},
            project_path=str(tmp_path),
            project_id=_PROJECT_ID,
        )

    assert failures == ()
    assert node_reads == []
