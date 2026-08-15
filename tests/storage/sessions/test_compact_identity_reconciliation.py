"""Storage coverage for compact identity ownership reconciliation."""

from __future__ import annotations

import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.sessions.compact_identity import (
    MAX_COMPACT_CONTINUATION_CANDIDATES,
    resolve_compact_continuation,
)
from gobby.storage import session_activity
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_activity import reconcile_compact_session_activity
from gobby.storage.sessions import SessionManager
from tests.fixtures.postgres import TEST_MACHINE_ID_PREFIX

LOCAL_MACHINE_ID = f"{TEST_MACHINE_ID_PREFIX}000000000006"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


TERMINAL_CONTEXT = {
    "tmux_pane": "%214",
    "tmux_socket_path": "/tmp/tmux-compact-test",
    "parent_pid": 25098,
    "parent_create_time": 1785221537.612984,
}


def _register(
    manager: SessionManager,
    *,
    project_id: str,
    external_id: str,
    terminal_context: dict[str, Any] | None = None,
) -> str:
    return manager.register_session(
        external_id=external_id,
        machine_id=LOCAL_MACHINE_ID,
        source="claude",
        project_id=project_id,
        terminal_context=terminal_context or dict(TERMINAL_CONTEXT),
    )


def _mark_compact(
    db: HubDatabase,
    session_id: str,
    *,
    message_count: int = 0,
) -> None:
    db.execute(
        """
        UPDATE sessions
        SET status = 'expired',
            message_count = %s
        WHERE id = %s
        """,
        (message_count, session_id),
    )
    db.execute(
        """
        INSERT INTO session_variables (session_id, variables)
        VALUES (%s, %s::jsonb)
        ON CONFLICT (session_id)
        DO UPDATE SET variables = EXCLUDED.variables
        """,
        (session_id, json.dumps({"handoff_source": "compact"})),
    )


def test_explicit_compact_activity_restores_canonical_row_and_deletes_empty_ghost(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = SessionManager(temp_db)
    canonical_id = _register(
        manager,
        project_id=sample_project["id"],
        external_id="canonical-provider-id",
    )
    ghost_id = _register(
        manager,
        project_id=sample_project["id"],
        external_id="observed-ghost-id",
    )
    _mark_compact(temp_db, canonical_id, message_count=1838)

    resolution = reconcile_compact_session_activity(manager, canonical_id)

    assert resolution.success
    assert resolution.session is not None
    assert resolution.session.id == canonical_id
    assert resolution.session.status == "active"
    assert resolution.deleted_ghost_ids == (ghost_id,)
    assert manager.get(ghost_id) is None

    restarted_manager = SessionManager(temp_db)
    restarted = restarted_manager.find_by_external_id(
        "canonical-provider-id",
        sample_project["id"],
        "claude",
    )
    assert restarted is not None
    assert restarted.id == canonical_id
    assert restarted.ref == resolution.session.ref


def test_compact_reconciliation_preserves_ghost_when_owner_update_loses_race(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SessionManager(temp_db)
    canonical_id = _register(
        manager,
        project_id=sample_project["id"],
        external_id="canonical-raced-id",
    )
    ghost_id = _register(
        manager,
        project_id=sample_project["id"],
        external_id="observed-raced-ghost-id",
    )
    _mark_compact(temp_db, canonical_id, message_count=1838)
    monkeypatch.setattr(session_activity, "_updated_once", lambda cursor: False)

    resolution = reconcile_compact_session_activity(manager, canonical_id)

    assert resolution.error_code == "session_deleted"
    assert manager.get(ghost_id) is not None


def test_compact_reconciliation_ignores_post_commit_notification_failure(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = SessionManager(temp_db)
    canonical_id = _register(
        manager,
        project_id=sample_project["id"],
        external_id="canonical-notify-id",
    )
    ghost_id = _register(
        manager,
        project_id=sample_project["id"],
        external_id="observed-notify-ghost-id",
    )
    _mark_compact(temp_db, canonical_id, message_count=1838)
    caplog.set_level("WARNING", logger="gobby.storage.session_activity")

    def fail_notification(event: str, session_id: str) -> None:
        raise RuntimeError(f"{event}:{session_id}")

    monkeypatch.setattr(manager, "_notify_session_change", fail_notification)

    resolution = reconcile_compact_session_activity(manager, canonical_id)

    assert resolution.success
    assert resolution.deleted_ghost_ids == (ghost_id,)
    assert manager.get(ghost_id) is None
    assert "Session change notification failed" in caplog.text


def test_populated_duplicate_blocks_compact_reconciliation_without_mutation(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = SessionManager(temp_db)
    canonical_id = _register(
        manager,
        project_id=sample_project["id"],
        external_id="canonical-provider-id",
    )
    duplicate_id = _register(
        manager,
        project_id=sample_project["id"],
        external_id="populated-duplicate-id",
    )
    _mark_compact(temp_db, canonical_id, message_count=20)
    temp_db.execute(
        "UPDATE sessions SET message_count = 1 WHERE id = %s",
        (duplicate_id,),
    )

    resolution = reconcile_compact_session_activity(manager, canonical_id)

    assert not resolution.success
    assert resolution.error_code == "compact_identity_conflict"
    assert resolution.conflicting_session_ids == (duplicate_id,)
    canonical = manager.get(canonical_id)
    assert canonical is not None
    assert canonical.status == "expired"
    assert manager.get(duplicate_id) is not None


def test_populated_historical_terminal_row_does_not_block_compact_reactivation(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = SessionManager(temp_db)
    historical_id = _register(
        manager,
        project_id=sample_project["id"],
        external_id="historical-provider-id",
    )
    canonical_id = _register(
        manager,
        project_id=sample_project["id"],
        external_id="canonical-provider-id",
    )
    temp_db.execute(
        """
        UPDATE sessions
        SET status = 'expired',
            message_count = 10,
            created_at = created_at - INTERVAL '1 minute'
        WHERE id = %s
        """,
        (historical_id,),
    )
    _mark_compact(temp_db, canonical_id, message_count=20)

    resolution = reconcile_compact_session_activity(manager, canonical_id)

    assert resolution.success
    assert resolution.session is not None
    assert resolution.session.id == canonical_id
    assert resolution.session.status == "active"
    historical = manager.get(historical_id)
    assert historical is not None
    assert historical.status == "expired"


def test_ended_later_populated_sibling_does_not_block_compact_reactivation(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = SessionManager(temp_db)
    canonical_id = _register(
        manager,
        project_id=sample_project["id"],
        external_id="canonical-provider-id",
    )
    sibling_id = _register(
        manager,
        project_id=sample_project["id"],
        external_id="ended-sibling-provider-id",
    )
    _mark_compact(temp_db, canonical_id, message_count=20)
    temp_db.execute(
        """
        UPDATE sessions
        SET status = 'handoff_ready',
            message_count = 4,
            created_at = created_at + INTERVAL '1 second'
        WHERE id = %s
        """,
        (sibling_id,),
    )

    resolution = reconcile_compact_session_activity(manager, canonical_id)

    assert resolution.success
    assert resolution.session is not None
    assert resolution.session.id == canonical_id
    assert resolution.session.status == "active"
    sibling = manager.get(sibling_id)
    assert sibling is not None
    assert sibling.status == "handoff_ready"
    assert sibling.id not in resolution.deleted_ghost_ids


def test_compact_resolution_uses_marker_and_exact_terminal_process(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = SessionManager(temp_db)
    canonical_id = _register(
        manager,
        project_id=sample_project["id"],
        external_id="canonical-provider-id",
    )
    _mark_compact(temp_db, canonical_id, message_count=20)
    _register(
        manager,
        project_id=sample_project["id"],
        external_id="different-process-id",
        terminal_context={**TERMINAL_CONTEXT, "parent_create_time": 1785221538.0},
    )

    resolution = resolve_compact_continuation(
        temp_db,
        source="claude",
        terminal_context=dict(TERMINAL_CONTEXT),
    )

    assert not resolution.ambiguous
    assert resolution.session is not None
    assert resolution.session.id == canonical_id


def test_ambiguous_marked_terminal_process_matches_return_no_session(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = SessionManager(temp_db)
    first_id = _register(
        manager,
        project_id=sample_project["id"],
        external_id="first-canonical-id",
    )
    second_id = _register(
        manager,
        project_id=sample_project["id"],
        external_id="second-canonical-id",
    )
    _mark_compact(temp_db, first_id, message_count=1)
    _mark_compact(temp_db, second_id, message_count=1)

    resolution = resolve_compact_continuation(
        temp_db,
        source="claude",
        terminal_context=dict(TERMINAL_CONTEXT),
    )

    assert resolution.session is None
    assert set(resolution.conflicting_session_ids) == {first_id, second_id}


def test_compact_resolution_bounds_newest_candidates_and_preserves_ambiguity() -> None:
    db = MagicMock()
    db.fetchall.return_value = [
        {"id": "newer", "compact_marker": "compact"},
        {"id": "older", "compact_marker": "compact"},
    ]
    candidates = [
        SimpleNamespace(id="newer", status="expired", terminal_context={"pid": 1}),
        SimpleNamespace(id="older", status="expired", terminal_context={"pid": 1}),
    ]

    with (
        patch(
            "gobby.sessions.compact_identity.Session.from_row",
            side_effect=candidates,
        ),
        patch(
            "gobby.sessions.compact_identity.terminal_process_contexts_match",
            return_value=True,
        ),
    ):
        resolution = resolve_compact_continuation(
            db,
            source="codex",
            terminal_context={"pid": 1},
        )

    query, params = db.fetchall.call_args.args
    assert "ORDER BY s.created_at DESC, s.id DESC" in query
    assert "LIMIT %s" in query
    assert params == ("codex", MAX_COMPACT_CONTINUATION_CANDIDATES)
    assert resolution.conflicting_session_ids == ("newer", "older")
