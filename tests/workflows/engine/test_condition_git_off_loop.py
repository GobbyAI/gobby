"""Rule conditions that ask Git run on the rule-engine executor, off every loop (#22829)."""

from __future__ import annotations

import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.tasks.transcript_evidence_models import TranscriptEvidence, TranscriptTaskClaim
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine._offload import ENGINE_EXECUTOR_THREAD_PREFIX, RULE_LOOP_THREAD_PREFIX
from gobby.workflows.engine.core import RuleEngine

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
# Hook evaluation runs synchronous rule work on a rule-loop worker (#22864);
# direct offloads use the rule-engine pool. Both keep Git off the event loop.
RULE_WORKER_PREFIXES = (ENGINE_EXECUTOR_THREAD_PREFIX, RULE_LOOP_THREAD_PREFIX)


def _thread_state() -> tuple[str, bool]:
    """Name the calling thread and say whether it is the caller's event-loop thread.

    A rule-loop worker runs a private loop for its pass, so a running loop alone
    does not mean the caller's loop is blocked; the test's loop owns the main thread.
    """
    thread = threading.current_thread()
    return thread.name, thread is threading.main_thread()


async def _decide(
    db: HubDatabase,
    when: str,
    event: HookEvent,
    variables: dict[str, Any],
    *,
    engine: RuleEngine | None = None,
) -> str:
    RuleDefinitionManager(db).create(
        name="git-condition",
        definition_json=RuleDefinitionBody(
            event=RuleTriggerEvent.BEFORE_TOOL,
            when=when,
            effects=[RuleEffect(type="block", reason="condition held")],
        ).model_dump_json(),
        priority=10,
        enabled=True,
    )
    response = await (engine or RuleEngine(db)).evaluate(
        event, session_id=SESSION_ID, variables=variables
    )
    return response.decision


def _event(data: dict[str, Any], **metadata: Any) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=SESSION_ID,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data=data,
        metadata=metadata,
    )


async def test_navigation_asks_git_only_from_the_rule_engine_executor(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Adapters and chat backends normalize tool events on the event loop, so
    # normalizing must not ask Git; the rule condition that needs the answer asks.
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "src/app.py").write_text("VALUE = 1\n")
    calls: list[tuple[str, bool]] = []

    def git_ignored(_root: Path, _paths: list[Path]) -> set[Path]:
        calls.append(_thread_state())
        return set()

    monkeypatch.setattr("gobby.hooks.code_navigation_recovery._git_ignored", git_ignored)
    data = normalize_tool_fields(
        {
            "tool_name": "Bash",
            "tool_input": {"command": "rg VALUE src"},
            "cwd": str(repo),
            "project_path": str(repo),
        }
    )
    assert calls == []

    decision = await _decide(temp_db, "navigation_requires_index(event.data)", _event(data), {})

    assert decision == "block"
    assert len(calls) == 1
    thread_name, loop_running = calls[0]
    assert thread_name.startswith(RULE_WORKER_PREFIXES)
    assert loop_running is False


async def test_tdd_gate_asks_git_only_from_the_rule_engine_executor(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An unedited explicit regression reaches the server's Git baseline check.
    # Written-path variables alone cannot authorize the gate.
    project = tmp_path / "project"
    test_path = "tests/test_app.py"
    test_file = project / test_path
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_app():\n    assert True\n", encoding="utf-8")
    task_id = "22222222-2222-4222-8222-222222222222"
    task = SimpleNamespace(
        id=task_id,
        seq_num=1,
        claimed_by_session_id=SESSION_ID,
        labels=["tdd:required"],
        additional_skills=[],
        validation_criteria=f"1. regression-only: existing behavior; test: {test_path}::test_app",
    )
    task_manager = SimpleNamespace(get_task=lambda _task_id: task)
    session_manager = Mock()
    session_manager.get.return_value = SimpleNamespace(id=SESSION_ID, machine_id="isolated")
    evidence = TranscriptEvidence(
        task_claims=(TranscriptTaskClaim(task_id, datetime.now(UTC)),),
    )
    monkeypatch.setattr(
        "gobby.tasks.transcript_evidence._derive_transcript_evidence_sync",
        Mock(return_value=(evidence, None)),
    )
    calls: list[tuple[str, bool]] = []
    real_run = subprocess.run

    def git_run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["git", "rev-list"]:
            calls.append(_thread_state())
            # An unavailable claim baseline must leave the server gate closed.
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="no baseline")
        return real_run(args, **kwargs)

    monkeypatch.setattr("gobby.tasks.tdd_write_authorization.subprocess.run", git_run)
    event = _event(
        {"tool_name": "Write"}, project_path=str(project), _platform_session_id=SESSION_ID
    )
    engine = RuleEngine(temp_db, task_manager=task_manager, session_manager=session_manager)

    decision = await _decide(
        temp_db,
        "not tdd_gate_open(variables)",
        event,
        {
            "claimed_tasks": {task_id: "#1"},
            "tdd_tests_written": [str(test_file)],
            "project": {"name": "test", "id": "unknown", "path": str(project)},
        },
        engine=engine,
    )

    assert decision == "block"
    assert calls
    assert all(name.startswith(RULE_WORKER_PREFIXES) for name, _ in calls)
    assert not any(loop_running for _, loop_running in calls)
