"""Storage coverage for tmux runtime loss and explicit session rebinding."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest

from gobby.storage.sessions import SessionManager
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError
from gobby.terminal_context import parse_terminal_context_value

LOCAL_MACHINE_ID = "20000000-0000-4000-8000-000000000001"
FOREIGN_MACHINE_ID = "20000000-0000-4000-8000-000000000002"
SOCKET_PATH = "/private/tmp/tmux-501/gobby"
SOCKET_IDENTITY = f"tmux_socket_path:{SOCKET_PATH}"
TMUX_KEYS = {
    "tmux_pane",
    "tmux_window_id",
    "tmux_session",
    "tmux_socket_path",
    "tmux_socket_name",
    "tmux_socket",
}


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id.get_machine_id", return_value=LOCAL_MACHINE_ID):
        yield


def _terminal_context(index: int) -> dict[str, Any]:
    return {
        "tmux_pane": f"%{index}",
        "tmux_window_id": f"@{index}",
        "tmux_session": "gobby",
        "tmux_socket_path": SOCKET_PATH,
        "tmux_socket_name": "gobby",
        "tmux_socket": "legacy-gobby",
        "cwd": "/work/gobby",
        "runtime": {"pid": 9000 + index},
    }


def test_missing_socket_detaches_every_local_reference_without_page_limit(
    session_manager: SessionManager,
    sample_project: dict[str, str],
) -> None:
    local_sessions = [
        session_manager.register(
            external_id=f"socket-sweep-{index}",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
            terminal_context=_terminal_context(index),
        )
        for index in range(205)
    ]
    seeded_statuses = ["active", "paused", "handoff_ready", "expired", "deleted"]
    for session, status in zip(local_sessions[:5], seeded_statuses, strict=True):
        session_manager.db.execute(
            "UPDATE sessions SET status = %s WHERE id = %s", (status, session.id)
        )
    session_manager.record_skills_used(local_sessions[0].id, ["tasks"])

    with patch("gobby.utils.machine_id.get_machine_id", return_value=FOREIGN_MACHINE_ID):
        foreign = session_manager.register(
            external_id="foreign-same-socket",
            machine_id=FOREIGN_MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
            terminal_context=_terminal_context(999),
        )

    affected = session_manager.expire_tmux_socket_sessions(
        LOCAL_MACHINE_ID,
        SOCKET_IDENTITY,
    )

    assert set(affected) == {session.id for session in local_sessions}
    rows = session_manager.db.fetchall(
        "SELECT id, external_id, status, terminal_context FROM sessions WHERE id = ANY(%s)",
        (affected,),
    )
    rows_by_id = {str(row["id"]): row for row in rows}
    assert len(rows_by_id) == 205
    for index, session in enumerate(local_sessions):
        expected_status = "deleted" if index == 4 else "expired"
        row = rows_by_id[session.id]
        assert row["status"] == expected_status
        assert row["external_id"] == session.external_id
        context = parse_terminal_context_value(row["terminal_context"])
        assert context is not None
        assert TMUX_KEYS.isdisjoint(context)
        assert context["cwd"] == "/work/gobby"
        assert context["runtime"]["pid"] >= 9000

    assert session_manager.expire_tmux_socket_sessions(LOCAL_MACHINE_ID, SOCKET_IDENTITY) == []
    foreign_row = session_manager.db.fetchone(
        "SELECT status, terminal_context FROM sessions WHERE id = %s", (foreign.id,)
    )
    assert foreign_row is not None
    assert foreign_row["status"] == "active"
    foreign_context = parse_terminal_context_value(foreign_row["terminal_context"])
    assert foreign_context is not None
    assert foreign_context["tmux_socket_path"] == SOCKET_PATH
    skill_row = session_manager.db.fetchone(
        "SELECT skill_name FROM session_skills WHERE session_id = %s", (local_sessions[0].id,)
    )
    assert skill_row is not None
    assert skill_row["skill_name"] == "tasks"


def test_missing_pane_detaches_only_matching_pane_records(
    session_manager: SessionManager,
    sample_project: dict[str, str],
) -> None:
    first = session_manager.register(
        external_id="missing-pane-first",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=sample_project["id"],
        terminal_context=_terminal_context(41),
    )
    second = session_manager.register(
        external_id="missing-pane-second",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=sample_project["id"],
        terminal_context=_terminal_context(42),
    )

    affected = session_manager.expire_tmux_pane_sessions(
        LOCAL_MACHINE_ID,
        SOCKET_IDENTITY,
        "%41",
    )

    assert affected == [first.id]
    first_row = session_manager.get(first.id)
    second_row = session_manager.get(second.id)
    assert first_row is not None
    assert second_row is not None
    assert first_row.status == "expired"
    first_context = parse_terminal_context_value(first_row.terminal_context)
    second_context = parse_terminal_context_value(second_row.terminal_context)
    assert first_context is not None
    assert second_context is not None
    assert TMUX_KEYS.isdisjoint(first_context)
    assert second_row.status == "active"
    assert second_context["tmux_pane"] == "%42"


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
        "tmux_pane": "%88",
        "tmux_window_id": "@12",
        "tmux_session": "work",
        "tmux_socket_path": "/private/tmp/tmux-501/new-server",
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
