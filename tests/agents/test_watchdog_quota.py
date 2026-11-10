"""Quota fail-fast needs verified transcript evidence; pane text alone never fails a run."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from gobby.events.completion_registry import CompletionEventRegistry
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from tests.agents.test_lifecycle_monitor import _pane_text
from tests.agents.test_lifecycle_monitor_watchdog_idle_recovery import (
    LOCAL_MACHINE_ID,
    _make_idle_monitor_run,
    _write_codex_lifecycle_transcript,
)
from tests.fixtures.isolated_checkout import patch_local_machine_id

pytestmark = pytest.mark.unit

RESET_TIME = "Sep 6th, 2026 9:28 PM"
QUOTA_PANE = (
    "You've hit your usage limit. Visit https://chatgpt.com/codex/settings/usage "
    f"to purchase more credits or try again at {RESET_TIME}.\n"
)
USAGE_ERROR = {
    "message": "You've hit your usage limit. Account-specific reset details.",
    "codex_error_info": "usage_limit_exceeded",
}


@pytest.fixture(autouse=True)
def _local_machine_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_local_machine_id(monkeypatch, LOCAL_MACHINE_ID)


@pytest.fixture
def agent_run_manager(temp_db: HubDatabase) -> LocalAgentRunManager:
    return LocalAgentRunManager(temp_db)


def _run_id(label: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"gobby:test:{label}"))


@pytest.mark.asyncio
async def test_codex_usage_limit_marks_run_quota_exhausted(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "quota-exhausted.jsonl"
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id=_run_id("quota-exhausted"),
        transcript_path=transcript,
        session_age_seconds=1,
    )
    _write_codex_lifecycle_transcript(
        transcript,
        task_complete_error=USAGE_ERROR,
        event_timestamp=datetime.now(UTC),
    )

    with _pane_text(monitor, "❯\n"):
        handled = await monitor.check_idle_agents()

    failed = agent_run_manager.get(run.id)
    assert handled == 1
    assert failed is not None
    assert failed.status == "error"
    assert failed.terminal_reason == "provider_quota_exhausted"
    assert failed.error == "Codex provider error: usage_limit_exceeded"


@pytest.mark.asyncio
async def test_quota_reason_reaches_parent_completion_notification(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    notifications: list[tuple[str, str, dict[str, Any]]] = []

    async def wake_parent(
        session_id: str,
        message: str,
        result: dict[str, Any],
    ) -> dict[str, bool]:
        notifications.append((session_id, message, result))
        return {"ism_persisted": True}

    registry = CompletionEventRegistry(wake_callback=wake_parent)
    transcript = tmp_path / "quota-notification.jsonl"
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id=_run_id("quota-notification"),
        transcript_path=transcript,
        session_age_seconds=1,
        completion_registry=registry,
    )
    assert run.parent_session_id is not None
    registry.register(run.id, [run.parent_session_id])
    _write_codex_lifecycle_transcript(
        transcript,
        task_complete_error=USAGE_ERROR,
        event_timestamp=datetime.now(UTC),
    )

    with _pane_text(monitor, "❯\n"):
        handled = await monitor.check_idle_agents()

    assert handled == 1
    assert len(notifications) == 1
    session_id, message, result = notifications[0]
    assert session_id == run.parent_session_id
    assert message == f"Agent {run.id} failed"
    assert result["status"] == "error"
    assert result["terminal_reason"] == "provider_quota_exhausted"
    assert result["error"] == "Codex provider error: usage_limit_exceeded"


@pytest.mark.asyncio
async def test_quota_pane_text_alone_never_fails_run(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    sample_project: dict[str, Any],
    agent_run_manager: LocalAgentRunManager,
    tmp_path: Path,
) -> None:
    transcript = tmp_path / "pane-only.jsonl"
    monitor, run = _make_idle_monitor_run(
        temp_db=temp_db,
        session_manager=session_manager,
        sample_project=sample_project,
        agent_run_manager=agent_run_manager,
        run_id=_run_id("quota-pane-only"),
        transcript_path=transcript,
        session_age_seconds=1,
    )
    _write_codex_lifecycle_transcript(transcript)

    with _pane_text(monitor, QUOTA_PANE):
        await monitor.check_idle_agents()

    stored = agent_run_manager.get(run.id)
    assert stored is not None
    assert stored.status == "running"
    assert stored.terminal_reason is None
