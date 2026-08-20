"""Tests for clear_self continuation markers, take, seed, and retrieval."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.sessions._handoff import register_handoff_tools
from gobby.sessions import clear_continuation
from gobby.sessions.clear_continuation import (
    CLEAR_HANDOFF_TTL_SECONDS,
    ClearContinuationResolution,
    build_clear_self_continue_prompt,
    clear_failed_attempt,
    resolve_clear_continuation,
    schedule_clear_self_continuation,
    seed_clear_handoff_variables,
    stage_clear_attempt,
    take_clear_handoff_marker,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit

PRED_ID = "00000000-0000-4000-8000-000000000011"
SUCC_ID = "00000000-0000-4000-8000-000000000012"
SUCC2_ID = "00000000-0000-4000-8000-000000000013"
PROJECT_ID = "00000000-0000-4000-8000-000000000014"
OTHER_PROJECT_ID = "00000000-0000-4000-8000-000000000015"
MACHINE_ID = "21000000-0000-4000-8000-000000000021"
OTHER_MACHINE_ID = "21000000-0000-4000-8000-000000000022"
SOURCE = "grok"
ATTEMPT_ID = "clear-attempt-1"
HANDOFF = "Continue epic #20539: the staged clear handoff is the work context."
TERMINAL = {
    "tmux_pane": "%12",
    "tmux_socket_path": "/tmp/tmux-clear-test",
    "parent_pid": 4242,
    "parent_create_time": 1_700_000_000.0,
}


@pytest.fixture
def session_db(hub_db: HubDatabase) -> HubDatabase:
    hub_db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s)",
        (PROJECT_ID, "clear-continuation-test"),
    )
    _insert_session(
        hub_db,
        PRED_ID,
        external_id="clear-predecessor",
        machine_id=MACHINE_ID,
        project_id=PROJECT_ID,
        terminal_context=TERMINAL,
        summary_markdown="prior summary before staging",
    )
    return hub_db


def _insert_session(
    db: HubDatabase,
    session_id: str,
    *,
    external_id: str,
    machine_id: str,
    project_id: str,
    source: str = SOURCE,
    status: str = "active",
    terminal_context: dict[str, Any] | None = None,
    summary_markdown: str | None = None,
    parent_session_id: str | None = None,
) -> None:
    db.execute(
        """
        INSERT INTO sessions (
            id, external_id, machine_id, source, project_id,
            session_type, status, terminal_context, summary_markdown, parent_session_id
        )
        VALUES (%s, %s, %s, %s, %s, 'terminal', %s, %s::jsonb, %s, %s)
        """,
        (
            session_id,
            external_id,
            machine_id,
            source,
            project_id,
            status,
            json.dumps(terminal_context) if terminal_context is not None else None,
            summary_markdown,
            parent_session_id,
        ),
    )


def _variables(db: HubDatabase, session_id: str) -> dict[str, Any]:
    row = db.fetchone(
        "SELECT variables FROM session_variables WHERE session_id = %s",
        (session_id,),
    )
    if row is None:
        return {}
    raw = row["variables"]
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _session_row(db: HubDatabase, session_id: str) -> Any:
    row = db.fetchone("SELECT * FROM sessions WHERE id = %s", (session_id,))
    assert row is not None
    return row


def _resolve(
    db: HubDatabase,
    *,
    source: str = SOURCE,
    project_id: str = PROJECT_ID,
    machine_id: str = MACHINE_ID,
    terminal_context: dict[str, Any] | None = TERMINAL,
    predecessor_hint: str | None = None,
) -> ClearContinuationResolution:
    return resolve_clear_continuation(
        db,
        source=source,
        project_id=project_id,
        machine_id=machine_id,
        terminal_context=terminal_context,
        predecessor_hint=predecessor_hint,
    )


def test_stage_clear_attempt_writes_unconsumed_marker_and_returns_prior_summary(
    session_db: HubDatabase,
) -> None:
    prior = stage_clear_attempt(
        session_db,
        PRED_ID,
        attempt_id=ATTEMPT_ID,
        terminal_context=TERMINAL,
        chat_context={"model": "grok-4", "mode": "plan", "ignored": "x"},
    )

    assert prior["summary_markdown"] == "prior summary before staging"
    row = _session_row(session_db, PRED_ID)
    assert row["status"] != "handoff_ready"
    marker = _variables(session_db, PRED_ID)["clear_attempt"]
    assert marker["attempt_id"] == ATTEMPT_ID
    assert marker["consumed_by"] is None
    assert marker["terminal_context"] == TERMINAL
    assert marker["chat"] == {"model": "grok-4", "mode": "plan"}
    created_at = datetime.fromisoformat(marker["created_at"])
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - created_at.astimezone(UTC)).total_seconds()
    assert 0 <= age < CLEAR_HANDOFF_TTL_SECONDS


def test_resolve_binds_trusted_predecessor_hint(session_db: HubDatabase) -> None:
    stage_clear_attempt(
        session_db,
        PRED_ID,
        attempt_id=ATTEMPT_ID,
        terminal_context=TERMINAL,
        chat_context=None,
    )

    resolved = _resolve(session_db, terminal_context=None, predecessor_hint=PRED_ID)

    assert resolved.predecessor is not None
    assert resolved.predecessor.id == PRED_ID
    assert resolved.attempt_id == ATTEMPT_ID
    assert resolved.degrade_reason is None


def test_resolve_consumed_markers_do_not_starve_candidate_window(
    session_db: HubDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(clear_continuation, "MAX_CLEAR_CONTINUATION_CANDIDATES", 3)
    stage_clear_attempt(
        session_db,
        PRED_ID,
        attempt_id=ATTEMPT_ID,
        terminal_context=TERMINAL,
        chat_context=None,
    )
    for index in range(3):
        newer_id = str(uuid4())
        _insert_session(
            session_db,
            newer_id,
            external_id=f"consumed-predecessor-{index}",
            machine_id=MACHINE_ID,
            project_id=PROJECT_ID,
        )
        stage_clear_attempt(
            session_db,
            newer_id,
            attempt_id=f"consumed-attempt-{index}",
            terminal_context=None,
            chat_context=None,
        )
        session_db.execute(
            "UPDATE session_variables SET variables = "
            "jsonb_set(variables, '{clear_attempt,consumed_by}', '\"x\"') "
            "WHERE session_id = %s",
            (newer_id,),
        )

    resolved = _resolve(session_db, terminal_context=None, predecessor_hint=PRED_ID)

    assert resolved.predecessor is not None
    assert resolved.predecessor.id == PRED_ID
    assert resolved.attempt_id == ATTEMPT_ID
    assert resolved.degrade_reason is None


def test_resolve_binds_terminal_process_identity_without_hint(session_db: HubDatabase) -> None:
    stage_clear_attempt(
        session_db,
        PRED_ID,
        attempt_id=ATTEMPT_ID,
        terminal_context=TERMINAL,
        chat_context=None,
    )

    resolved = _resolve(session_db, predecessor_hint=None)

    assert resolved.predecessor is not None
    assert resolved.predecessor.id == PRED_ID
    assert resolved.attempt_id == ATTEMPT_ID
    assert resolved.degrade_reason is None


def test_resolve_missing_marker_has_no_predecessor(session_db: HubDatabase) -> None:
    resolved = _resolve(session_db)

    assert resolved.predecessor is None
    assert resolved.attempt_id is None
    assert resolved.degrade_reason is None


def test_resolve_expired_marker_degrades(session_db: HubDatabase) -> None:
    stage_clear_attempt(
        session_db,
        PRED_ID,
        attempt_id=ATTEMPT_ID,
        terminal_context=TERMINAL,
        chat_context=None,
    )
    variables = _variables(session_db, PRED_ID)
    variables["clear_attempt"]["created_at"] = (
        datetime.now(UTC) - timedelta(seconds=CLEAR_HANDOFF_TTL_SECONDS + 1)
    ).isoformat()
    session_db.execute(
        "UPDATE session_variables SET variables = %s WHERE session_id = %s",
        (json.dumps(variables), PRED_ID),
    )

    resolved = _resolve(session_db, predecessor_hint=PRED_ID)

    assert resolved.predecessor is None
    assert resolved.degrade_reason == "expired"


def test_resolve_reused_terminal_identity_degrades(session_db: HubDatabase) -> None:
    stage_clear_attempt(
        session_db,
        PRED_ID,
        attempt_id=ATTEMPT_ID,
        terminal_context=TERMINAL,
        chat_context=None,
    )
    reused = dict(TERMINAL)
    reused["parent_create_time"] = 1_700_000_010.0

    resolved = _resolve(session_db, terminal_context=reused, predecessor_hint=None)

    assert resolved.predecessor is None
    assert resolved.degrade_reason == "identity_mismatch"


def test_resolve_cross_project_marker_degrades(session_db: HubDatabase) -> None:
    session_db.execute(
        "INSERT INTO projects (id, name) VALUES (%s, %s)",
        (OTHER_PROJECT_ID, "other-clear-project"),
    )
    other_id = "00000000-0000-4000-8000-000000000031"
    _insert_session(
        session_db,
        other_id,
        external_id="other-project-pred",
        machine_id=MACHINE_ID,
        project_id=OTHER_PROJECT_ID,
        terminal_context=TERMINAL,
    )
    stage_clear_attempt(
        session_db,
        other_id,
        attempt_id=ATTEMPT_ID,
        terminal_context=TERMINAL,
        chat_context=None,
    )

    resolved = _resolve(session_db, predecessor_hint=other_id)

    assert resolved.predecessor is None
    assert resolved.degrade_reason == "cross_project"


def test_resolve_cross_machine_marker_degrades(session_db: HubDatabase) -> None:
    other_id = "00000000-0000-4000-8000-000000000032"
    _insert_session(
        session_db,
        other_id,
        external_id="other-machine-pred",
        machine_id=OTHER_MACHINE_ID,
        project_id=PROJECT_ID,
        terminal_context=TERMINAL,
    )
    stage_clear_attempt(
        session_db,
        other_id,
        attempt_id=ATTEMPT_ID,
        terminal_context=TERMINAL,
        chat_context=None,
    )

    resolved = _resolve(session_db, predecessor_hint=other_id)

    assert resolved.predecessor is None
    assert resolved.degrade_reason == "cross_machine"


def test_resolve_ambiguous_markers_degrade(session_db: HubDatabase) -> None:
    other_id = "00000000-0000-4000-8000-000000000033"
    _insert_session(
        session_db,
        other_id,
        external_id="second-pred",
        machine_id=MACHINE_ID,
        project_id=PROJECT_ID,
        terminal_context=TERMINAL,
    )
    stage_clear_attempt(
        session_db,
        PRED_ID,
        attempt_id=ATTEMPT_ID,
        terminal_context=TERMINAL,
        chat_context=None,
    )
    stage_clear_attempt(
        session_db,
        other_id,
        attempt_id="clear-attempt-2",
        terminal_context=TERMINAL,
        chat_context=None,
    )

    resolved = _resolve(session_db, predecessor_hint=None)

    assert resolved.predecessor is None
    assert resolved.degrade_reason == "ambiguous"


def test_resolve_lookup_exception_degrades(session_db: HubDatabase) -> None:
    with patch.object(session_db, "fetchall", side_effect=RuntimeError("db down")):
        resolved = _resolve(session_db)

    assert resolved.predecessor is None
    assert resolved.attempt_id is None
    assert resolved.degrade_reason == "exception"


def test_take_writes_parentage_and_only_one_concurrent_winner(session_db: HubDatabase) -> None:
    stage_clear_attempt(
        session_db,
        PRED_ID,
        attempt_id=ATTEMPT_ID,
        terminal_context=TERMINAL,
        chat_context=None,
    )
    _insert_session(
        session_db,
        SUCC_ID,
        external_id="clear-successor-a",
        machine_id=MACHINE_ID,
        project_id=PROJECT_ID,
        terminal_context=TERMINAL,
    )
    _insert_session(
        session_db,
        SUCC2_ID,
        external_id="clear-successor-b",
        machine_id=MACHINE_ID,
        project_id=PROJECT_ID,
        terminal_context=TERMINAL,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda successor_id: take_clear_handoff_marker(
                    session_db,
                    PRED_ID,
                    attempt_id=ATTEMPT_ID,
                    successor_id=successor_id,
                ),
                (SUCC_ID, SUCC2_ID),
            )
        )

    assert results.count(True) == 1
    assert results.count(False) == 1
    winner = SUCC_ID if results[0] else SUCC2_ID
    loser = SUCC2_ID if winner == SUCC_ID else SUCC_ID
    assert _session_row(session_db, winner)["parent_session_id"] == PRED_ID
    assert _session_row(session_db, loser)["parent_session_id"] is None
    marker = _variables(session_db, PRED_ID)["clear_attempt"]
    assert marker["consumed_by"] == winner
    assert (
        take_clear_handoff_marker(
            session_db,
            PRED_ID,
            attempt_id=ATTEMPT_ID,
            successor_id=loser,
        )
        is False
    )


def test_seed_writes_injectable_and_pending_without_parentage(session_db: HubDatabase) -> None:
    session_db.execute(
        "UPDATE sessions SET summary_markdown = %s WHERE id = %s",
        (HANDOFF, PRED_ID),
    )
    _insert_session(
        session_db,
        SUCC_ID,
        external_id="clear-successor-seed",
        machine_id=MACHINE_ID,
        project_id=PROJECT_ID,
        terminal_context=TERMINAL,
    )
    manager = SessionManager(session_db)
    predecessor = manager.get(PRED_ID)
    assert predecessor is not None

    seed_clear_handoff_variables(manager, SUCC_ID, predecessor)

    variables = SessionVariableManager(session_db).get_variables(SUCC_ID)
    assert variables["handoff_summary_injectable"] == HANDOFF
    assert variables["clear_handoff_inject_pending"] is True
    assert _session_row(session_db, SUCC_ID)["parent_session_id"] is None


def test_seed_oversized_handoff_uses_predecessor_breadcrumb(session_db: HubDatabase) -> None:
    oversized = "x" * 300
    session_db.execute(
        "UPDATE sessions SET summary_markdown = %s WHERE id = %s",
        (oversized, PRED_ID),
    )
    _insert_session(
        session_db,
        SUCC_ID,
        external_id="clear-successor-oversize",
        machine_id=MACHINE_ID,
        project_id=PROJECT_ID,
        terminal_context=TERMINAL,
    )
    manager = SessionManager(session_db)
    predecessor = manager.get(PRED_ID)
    assert predecessor is not None

    with patch(
        "gobby.sessions.clear_continuation.handoff_summary_inject_budget_for",
        return_value=200,
    ):
        seed_clear_handoff_variables(manager, SUCC_ID, predecessor)

    injectable = SessionVariableManager(session_db).get_variables(SUCC_ID)[
        "handoff_summary_injectable"
    ]
    assert injectable != oversized
    assert "get_handoff_context" in injectable
    seq_num = _session_row(session_db, PRED_ID)["seq_num"]
    predecessor_ref = f"#{seq_num}" if seq_num is not None else PRED_ID
    assert predecessor_ref in injectable


def test_clear_failed_attempt_restores_summary_and_removes_unconsumed_marker(
    session_db: HubDatabase,
) -> None:
    prior = stage_clear_attempt(
        session_db,
        PRED_ID,
        attempt_id=ATTEMPT_ID,
        terminal_context=TERMINAL,
        chat_context=None,
    )
    session_db.execute(
        "UPDATE sessions SET summary_markdown = %s WHERE id = %s",
        (HANDOFF, PRED_ID),
    )

    restored = clear_failed_attempt(
        session_db,
        PRED_ID,
        attempt_id=ATTEMPT_ID,
        prior_summary_state=prior,
    )

    assert restored is True
    assert _session_row(session_db, PRED_ID)["summary_markdown"] == "prior summary before staging"
    assert "clear_attempt" not in _variables(session_db, PRED_ID)


def test_clear_failed_attempt_is_noop_after_successful_take(session_db: HubDatabase) -> None:
    prior = stage_clear_attempt(
        session_db,
        PRED_ID,
        attempt_id=ATTEMPT_ID,
        terminal_context=TERMINAL,
        chat_context=None,
    )
    _insert_session(
        session_db,
        SUCC_ID,
        external_id="clear-successor-taken",
        machine_id=MACHINE_ID,
        project_id=PROJECT_ID,
        terminal_context=TERMINAL,
    )
    session_db.execute(
        "UPDATE sessions SET summary_markdown = %s WHERE id = %s",
        (HANDOFF, PRED_ID),
    )
    assert take_clear_handoff_marker(
        session_db,
        PRED_ID,
        attempt_id=ATTEMPT_ID,
        successor_id=SUCC_ID,
    )

    restored = clear_failed_attempt(
        session_db,
        PRED_ID,
        attempt_id=ATTEMPT_ID,
        prior_summary_state=prior,
    )

    assert restored is False
    assert _session_row(session_db, PRED_ID)["summary_markdown"] == HANDOFF
    assert _variables(session_db, PRED_ID)["clear_attempt"]["consumed_by"] == SUCC_ID


def test_clear_failed_attempt_is_noop_when_attempt_id_changes(session_db: HubDatabase) -> None:
    prior = stage_clear_attempt(
        session_db,
        PRED_ID,
        attempt_id=ATTEMPT_ID,
        terminal_context=TERMINAL,
        chat_context=None,
    )
    stage_clear_attempt(
        session_db,
        PRED_ID,
        attempt_id="clear-attempt-replaced",
        terminal_context=TERMINAL,
        chat_context=None,
    )
    session_db.execute(
        "UPDATE sessions SET summary_markdown = %s WHERE id = %s",
        (HANDOFF, PRED_ID),
    )

    restored = clear_failed_attempt(
        session_db,
        PRED_ID,
        attempt_id=ATTEMPT_ID,
        prior_summary_state=prior,
    )

    assert restored is False
    assert _session_row(session_db, PRED_ID)["summary_markdown"] == HANDOFF
    assert _variables(session_db, PRED_ID)["clear_attempt"]["attempt_id"] == (
        "clear-attempt-replaced"
    )


@pytest.mark.asyncio
async def test_bound_successor_reads_expired_predecessor_handoff(
    session_db: HubDatabase,
) -> None:
    session_db.execute(
        "UPDATE sessions SET summary_markdown = %s, status = 'expired' WHERE id = %s",
        (HANDOFF, PRED_ID),
    )
    _insert_session(
        session_db,
        SUCC_ID,
        external_id="clear-successor-bound",
        machine_id=MACHINE_ID,
        project_id=PROJECT_ID,
        terminal_context=TERMINAL,
        parent_session_id=PRED_ID,
    )
    registry = InternalToolRegistry(name="gobby-sessions", description="test")
    register_handoff_tools(registry, SessionManager(session_db))

    with (
        session_context_for_test(SUCC_ID),
        patch(
            "gobby.mcp_proxy.tools.sessions._handoff.get_project_context",
            return_value={"id": PROJECT_ID},
        ),
    ):
        result = await registry.call("get_handoff_context", {"session_id": PRED_ID})

    assert result["success"] is True
    assert result["found"] is True
    assert result["context"] == HANDOFF
    assert result["session_id"] == PRED_ID


def test_build_clear_self_continue_prompt_names_predecessor() -> None:
    prompt = build_clear_self_continue_prompt(predecessor_ref="#42")

    assert "clear_self" in prompt
    assert "#42" in prompt
    assert "get_handoff_context" in prompt


def test_schedule_clear_self_continuation_requires_tmux_target() -> None:
    class _Session:
        id = str(uuid4())
        terminal_context: dict[str, Any] | None = None

    session = _Session()
    assert schedule_clear_self_continuation(session, "continue") is False
    session.terminal_context = {"tmux_pane": "%12"}
    with (
        patch(
            "gobby.sessions.compact_continuation.get_tmux_manager_for_context",
            return_value=object(),
        ),
        patch(
            "gobby.sessions.compact_continuation._schedule_coroutine",
            return_value=True,
        ) as scheduled,
    ):
        assert schedule_clear_self_continuation(session, "continue", delay_seconds=0) is True
        scheduled.assert_called_once()
