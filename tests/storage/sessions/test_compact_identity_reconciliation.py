"""Storage coverage for compact identity ownership reconciliation."""

from __future__ import annotations

import json
from typing import Any

from gobby.sessions.compact_identity import resolve_compact_continuation
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_activity import reconcile_compact_session_activity
from gobby.storage.sessions import SessionManager

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
        machine_id="machine-compact-test",
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
        "machine-compact-test",
        sample_project["id"],
        "claude",
    )
    assert restarted is not None
    assert restarted.id == canonical_id
    assert restarted.ref == resolution.session.ref


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
        machine_id="machine-compact-test",
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
        machine_id="machine-compact-test",
        source="claude",
        terminal_context=dict(TERMINAL_CONTEXT),
    )

    assert resolution.session is None
    assert resolution.conflicting_session_ids == (first_id, second_id)
