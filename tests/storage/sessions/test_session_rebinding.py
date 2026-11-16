"""Storage coverage for native session rebinding and web continuation."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest

from gobby.storage.sessions import SessionManager
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError

LOCAL_MACHINE_ID = "20000000-0000-4000-8000-000000000001"
FOREIGN_MACHINE_ID = "20000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id.get_machine_id", return_value=LOCAL_MACHINE_ID):
        yield


def _terminal_context(index: int) -> dict[str, Any]:
    return {
        "gobby_terminal_id": f"20000000-0000-4000-8000-{index:012d}",
        "cwd": "/work/gobby",
        "runtime": {"pid": 9000 + index},
    }


def test_explicit_terminal_resume_rebinds_same_row_and_replaces_runtime_context(
    session_manager: SessionManager,
    sample_project: dict[str, str],
) -> None:
    original = session_manager.register(
        external_id="resume-in-place",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=sample_project["id"],
        transcript_path="/tmp/old.jsonl",
        terminal_context=_terminal_context(10),
    )
    session_manager.update_status(original.id, "expired")
    fresh_context = {
        "gobby_terminal_id": "20000000-0000-4000-8000-000000000088",
        "pid": 4242,
        "tty": "/dev/ttys012",
        "cwd": "/work/gobby",
    }
    assert original.last_activity is not None
    activation_time = original.last_activity + timedelta(minutes=1)

    with patch("gobby.storage.sessions._terminal.utc_now", return_value=activation_time):
        rebound = session_manager.rebind_resumed_terminal_session(
            original.id,
            machine_id=LOCAL_MACHINE_ID,
            project_id=sample_project["id"],
            source="codex",
            transcript_path="/tmp/new.jsonl",
            terminal_context=fresh_context,
            workflow_name=None,
            agent_depth=0,
            sandbox_enabled=False,
        )

    assert rebound is not None
    assert rebound.id == original.id
    assert rebound.external_id == original.external_id
    assert rebound.status == "active"
    assert rebound.terminal_context == fresh_context
    assert rebound.transcript_path == "/tmp/new.jsonl"
    assert rebound.last_activity == activation_time
    paused = session_manager.update_status(rebound.id, "paused")
    assert paused is not None and paused.status == "paused"


def test_ordinary_reregistration_cannot_rebind_expired_terminal(
    session_manager: SessionManager,
    sample_project: dict[str, str],
) -> None:
    original = session_manager.register(
        external_id="delayed-hook",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=sample_project["id"],
        transcript_path="/tmp/original.jsonl",
        terminal_context=_terminal_context(20),
    )
    session_manager.update_status(original.id, "expired")

    delayed = session_manager.register(
        external_id=original.external_id,
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=sample_project["id"],
        transcript_path="/tmp/delayed.jsonl",
        terminal_context=_terminal_context(21),
    )

    assert delayed.id == original.id
    assert delayed.status == "expired"
    assert delayed.transcript_path == "/tmp/original.jsonl"
    assert delayed.terminal_context == original.terminal_context


def test_web_continuation_converts_same_terminal_row_and_clears_context(
    session_manager: SessionManager,
    sample_project: dict[str, str],
) -> None:
    original = session_manager.register(
        external_id="terminal-to-web",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=sample_project["id"],
        terminal_context=_terminal_context(30),
    )
    session_manager.update_status(original.id, "expired")
    assert original.last_activity is not None
    activation_time = original.last_activity + timedelta(minutes=1)

    with patch("gobby.storage.sessions._terminal.utc_now", return_value=activation_time):
        continued = session_manager.continue_terminal_session_as_web_chat(
            original.id,
            source="claude",
            model="claude-opus-4-6",
            project_id=sample_project["id"],
            sandbox_policy_hash="policy-v2",
        )

    assert continued is not None
    assert continued.id == original.id
    assert continued.external_id == original.external_id
    assert continued.session_type == "web_chat"
    assert continued.status == "active"
    assert continued.terminal_context == {}
    assert continued.last_activity == activation_time


def test_web_continuation_rejects_foreign_terminal_before_mutation(
    session_manager: SessionManager,
    sample_project: dict[str, str],
) -> None:
    with patch("gobby.utils.machine_id.get_machine_id", return_value=FOREIGN_MACHINE_ID):
        foreign = session_manager.register(
            external_id="foreign-terminal-to-web",
            machine_id=FOREIGN_MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
            terminal_context=_terminal_context(31),
        )
        session_manager.update_status(foreign.id, "expired")

    with pytest.raises(MachineOwnershipMismatchError):
        session_manager.continue_terminal_session_as_web_chat(
            foreign.id,
            source="claude",
            model="claude-opus-4-6",
            project_id=sample_project["id"],
            sandbox_policy_hash="policy-v2",
        )

    unchanged = session_manager.get(foreign.id)
    assert unchanged is not None
    assert unchanged.session_type == "terminal"
    assert unchanged.status == "expired"
    assert unchanged.terminal_context == _terminal_context(31)
