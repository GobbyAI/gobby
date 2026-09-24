"""Rule conditions that ask Git run on the rule-engine executor, off every loop (#22829)."""

from __future__ import annotations

import asyncio
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.hooks.normalization import normalize_tool_fields
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.workflows import tdd_paths
from gobby.workflows.definitions import RuleDefinitionBody, RuleEffect, RuleTriggerEvent
from gobby.workflows.engine._offload import ENGINE_EXECUTOR_THREAD_PREFIX
from gobby.workflows.engine.core import RuleEngine

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"


def _thread_state() -> tuple[str, bool]:
    """Name the calling thread and say whether an event loop runs on it."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return threading.current_thread().name, False
    return threading.current_thread().name, True


async def _decide(db: HubDatabase, when: str, event: HookEvent, variables: dict[str, Any]) -> str:
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
    response = await RuleEngine(db).evaluate(event, session_id=SESSION_ID, variables=variables)
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
    assert thread_name.startswith(ENGINE_EXECUTOR_THREAD_PREFIX)
    assert loop_running is False


async def test_tdd_gate_asks_git_only_from_the_rule_engine_executor(
    temp_db: HubDatabase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A written test outside the project root makes tdd_path_identity ask Git
    # whether it belongs to a linked worktree of the same repository.
    project = tmp_path / "project"
    project.mkdir()
    written = tmp_path / "elsewhere" / "tests" / "test_app.py"
    written.parent.mkdir(parents=True)
    written.write_text("")
    calls: list[tuple[str, bool]] = []

    def git_out(_cwd: Path, *_args: str) -> str | None:
        calls.append(_thread_state())
        return None

    monkeypatch.setattr(tdd_paths, "_git_out", git_out)
    tdd_paths._git_identity.cache_clear()
    event = _event({"tool_name": "Write"}, project_path=str(project))

    decision = await _decide(
        temp_db, "tdd_gate_open(variables)", event, {"tdd_tests_written": [str(written)]}
    )

    assert decision == "block"
    assert calls
    assert all(name.startswith(ENGINE_EXECUTOR_THREAD_PREFIX) for name, _ in calls)
    assert not any(loop_running for _, loop_running in calls)
