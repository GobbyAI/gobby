"""Focused tests for session storage behavior."""

import logging
from collections.abc import Iterator
from unittest.mock import patch
from uuid import uuid4

import pytest

from gobby.sessions.status_events import SessionStatusTransition
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager
from gobby.workflows.step_instances import AgentStepInstanceManager
from tests.workflows.step_instance_fixtures import make_step_instance

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "20000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity(temp_db: HubDatabase) -> Iterator[None]:
    from gobby.storage.machines import LocalMachineManager
    from tests.fixtures.postgres import TEST_USER_ID

    LocalMachineManager(temp_db).upsert_seen(LOCAL_MACHINE_ID, TEST_USER_ID)
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


class TestSessionManagerPruning:
    """Tests split from the SessionManager storage monolith."""

    def test_expire_orphaned_handoff_preserves_workflow_instances(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Orphan sweep only expires: the handoff_ready row is the live session."""
        session = session_manager.register(
            external_id="orphaned-compact-session",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.update_status(session.id, "handoff_ready")
        workflow_manager = AgentStepInstanceManager(session_manager.db)
        workflow_manager.save(
            make_step_instance(
                session.id,
                agent_name="developer",
                current_step="implement",
            )
        )
        session_manager.db.execute(
            "UPDATE sessions SET updated_at = NOW() - INTERVAL '31 minutes' WHERE id = %s",
            (session.id,),
        )

        expired = session_manager.expire_orphaned_handoff_sessions(timeout_minutes=30)

        assert expired == 1
        updated = session_manager.get(session.id)
        assert updated is not None
        assert updated.status == "expired"
        instance = workflow_manager.get_for_session(session.id)
        assert instance is not None
        assert instance.agent_name == "developer"

    def test_prune_stale_compact_workflow_instances_reclaims_marked_sessions(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Only long-expired sessions with an unconsumed compact marker are reclaimed."""
        session = session_manager.register(
            external_id="unresumed-compact-session",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        workflow_manager = AgentStepInstanceManager(session_manager.db)
        workflow_manager.save(
            make_step_instance(
                session.id,
                agent_name="developer",
                current_step="implement",
            )
        )
        SessionVariableManager(session_manager.db).merge_variables(
            session.id, {"handoff_source": "compact"}
        )
        session_manager.db.execute(
            "UPDATE sessions SET status = 'expired', "
            "updated_at = NOW() - INTERVAL '25 hours' WHERE id = %s",
            (session.id,),
        )

        pruned = session_manager.prune_stale_compact_workflow_instances(retention_hours=24)

        assert pruned == 1
        assert workflow_manager.get_for_session(session.id) is None

    def test_prune_stale_compact_workflow_instances_skips_unmarked_and_fresh(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Expired daemon-resume sessions (no marker) and fresh markers are untouched."""
        workflow_manager = AgentStepInstanceManager(session_manager.db)
        sv_manager = SessionVariableManager(session_manager.db)

        unmarked = session_manager.register(
            external_id="expired-daemon-resume-session",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        fresh = session_manager.register(
            external_id="freshly-expired-compact-session",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        for session_id in (unmarked.id, fresh.id):
            workflow_manager.save(
                make_step_instance(
                    session_id,
                    agent_name="developer",
                    current_step="implement",
                )
            )
        sv_manager.merge_variables(fresh.id, {"handoff_source": "compact"})
        session_manager.db.execute(
            "UPDATE sessions SET status = 'expired', "
            "updated_at = NOW() - INTERVAL '25 hours' WHERE id = %s",
            (unmarked.id,),
        )
        session_manager.db.execute(
            "UPDATE sessions SET status = 'expired', "
            "updated_at = NOW() - INTERVAL '1 hour' WHERE id = %s",
            (fresh.id,),
        )

        pruned = session_manager.prune_stale_compact_workflow_instances(retention_hours=24)

        assert pruned == 0
        assert workflow_manager.get_for_session(unmarked.id) is not None
        assert workflow_manager.get_for_session(fresh.id) is not None

    def test_cleanup_expired_session_state_uses_revival_horizon_and_terminalizes_pending(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        variable_manager = SessionVariableManager(session_manager.db)
        old_expired = session_manager.register(
            external_id="old-expired-state",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=sample_project["id"],
        )
        old_deleted = session_manager.register(
            external_id="old-deleted-state",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=sample_project["id"],
        )
        recent_expired = session_manager.register(
            external_id="recent-expired-state",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=sample_project["id"],
        )
        for session in (old_expired, old_deleted, recent_expired):
            variable_manager.merge_variables(session.id, {"payload": session.external_id})

        session_manager.db.execute(
            "UPDATE sessions SET status = 'expired', "
            "updated_at = NOW() - INTERVAL '25 hours' WHERE id = %s",
            (old_expired.id,),
        )
        session_manager.db.execute(
            "UPDATE sessions SET status = 'deleted', "
            "updated_at = NOW() - INTERVAL '25 hours' WHERE id = %s",
            (old_deleted.id,),
        )
        session_manager.db.execute(
            "UPDATE sessions SET status = 'expired', "
            "updated_at = NOW() - INTERVAL '23 hours' WHERE id = %s",
            (recent_expired.id,),
        )
        interaction_id = str(uuid4())
        session_manager.db.execute(
            """
            INSERT INTO pending_interactions (
                id, session_id, kind, provider, payload_json, status, timeout_seconds
            ) VALUES (%s, %s, 'approval', 'codex', '{}', 'pending', 300)
            """,
            (interaction_id, old_expired.id),
        )

        result = session_manager.cleanup_expired_session_state()

        assert result.session_variables == 2
        assert result.pending_interactions == 1
        remaining_variables = session_manager.db.fetchall(
            "SELECT session_id FROM session_variables WHERE session_id IN (%s, %s, %s)",
            (old_expired.id, old_deleted.id, recent_expired.id),
        )
        assert [row["session_id"] for row in remaining_variables] == [recent_expired.id]
        pending = session_manager.db.fetchone(
            "SELECT status, decision, resolved_at FROM pending_interactions WHERE id = %s",
            (interaction_id,),
        )
        assert pending is not None
        assert pending["status"] == "expired"
        assert pending["decision"] == "timeout"
        assert pending["resolved_at"] is not None

    def test_expire_stale_sessions(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Test expiring stale sessions."""
        # Create a stale session (simulated by mocking updated_at in query or just rely on db time)
        # Since we use PostgreSQL CURRENT_TIMESTAMP in queries, we can't easily mock time without
        # deeper mocking. Instead, we'll verify the SQL generation and execution flow
        # or use a very short timeout.

        session = session_manager.register(
            external_id="stale-session",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )

        # Manually backdate the session in DB
        session_manager.db.execute(
            "UPDATE sessions SET updated_at = NOW() - INTERVAL '25 hours' WHERE id = %s",
            (session.id,),
        )
        transitions: list[SessionStatusTransition] = []
        session_manager.register_status_transition_listener(transitions.append)

        count = session_manager.expire_stale_sessions(timeout_hours=24)
        assert count == 1

        expired = session_manager.get(session.id)
        assert expired.status == "expired"
        assert [(event.session_id, event.status) for event in transitions] == [
            (session.id, "expired")
        ]

    def test_pause_inactive_active_sessions(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Test pausing inactive active sessions."""
        session = session_manager.register(
            external_id="active-idle",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )

        # Backdate
        session_manager.db.execute(
            "UPDATE sessions SET updated_at = NOW() - INTERVAL '31 minutes' WHERE id = %s",
            (session.id,),
        )
        transitions: list[SessionStatusTransition] = []
        session_manager.register_status_transition_listener(transitions.append)

        count = session_manager.pause_inactive_active_sessions(timeout_minutes=30)
        assert count == 1

        paused = session_manager.get(session.id)
        assert paused.status == "paused"
        assert [(event.session_id, event.status) for event in transitions] == [
            (session.id, "paused")
        ]

    def test_pause_inactive_active_sessions_preserves_last_activity_time(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Auto-pausing should not make a stale session look freshly active."""
        session = session_manager.register(
            external_id="active-idle-preserve-time",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )

        session_manager.db.execute(
            "UPDATE sessions SET updated_at = NOW() - INTERVAL '31 minutes' WHERE id = %s",
            (session.id,),
        )
        before = session_manager.db.fetchone(
            "SELECT updated_at FROM sessions WHERE id = %s",
            (session.id,),
        )

        count = session_manager.pause_inactive_active_sessions(timeout_minutes=30)
        assert count == 1

        after = session_manager.db.fetchone(
            "SELECT updated_at FROM sessions WHERE id = %s",
            (session.id,),
        )
        assert before is not None
        assert after is not None
        assert after["updated_at"] == before["updated_at"]

    def test_pause_then_expire_stale_session_uses_last_activity_time(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """A very old session should expire in the same cleanup sweep after pause."""
        session = session_manager.register(
            external_id="ancient-active-session",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )

        session_manager.db.execute(
            "UPDATE sessions SET updated_at = NOW() - INTERVAL '25 hours' WHERE id = %s",
            (session.id,),
        )

        paused = session_manager.pause_inactive_active_sessions(timeout_minutes=30)
        expired = session_manager.expire_stale_sessions(timeout_hours=24)

        assert paused == 1
        assert expired == 1
        stale = session_manager.get(session.id)
        assert stale is not None
        assert stale.status == "expired"

    @pytest.mark.parametrize("target_field", ["tmux_pane", "tmux_window_id"])
    def test_tmux_backed_session_pauses_without_refresh_and_survives_stale_expiry(
        self,
        session_manager: SessionManager,
        sample_project: dict,
        target_field: str,
    ) -> None:
        session = session_manager.register(
            external_id=f"idle-tmux-{target_field}",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
            terminal_context={target_field: "%304" if target_field == "tmux_pane" else "@42"},
        )
        session_manager.db.execute(
            "UPDATE sessions SET updated_at = NOW() - INTERVAL '25 hours' WHERE id = %s",
            (session.id,),
        )
        before = session_manager.get(session.id)

        paused = session_manager.pause_inactive_active_sessions(timeout_minutes=30)
        expired = session_manager.expire_stale_sessions(timeout_hours=24)

        after = session_manager.get(session.id)
        assert before is not None
        assert after is not None
        assert paused == 1
        assert expired == 0
        assert after.status == "paused"
        assert after.updated_at == before.updated_at

    @pytest.mark.parametrize("target_field", ["tmux_pane", "tmux_window_id"])
    @pytest.mark.parametrize("status", ["active", "paused", "handoff_ready"])
    def test_stale_expiry_protects_recorded_tmux_owner_status(
        self,
        session_manager: SessionManager,
        sample_project: dict,
        target_field: str,
        status: str,
    ) -> None:
        session = session_manager.register(
            external_id=f"protected-tmux-{target_field}-{status}",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
            terminal_context={target_field: "%305" if target_field == "tmux_pane" else "@43"},
        )
        if status != "active":
            session_manager.update_status(session.id, status)
        session_manager.db.execute(
            "UPDATE sessions SET updated_at = NOW() - INTERVAL '25 hours' WHERE id = %s",
            (session.id,),
        )

        expired = session_manager.expire_stale_sessions(timeout_hours=24)

        protected = session_manager.get(session.id)
        assert expired == 0
        assert protected is not None
        assert protected.status == status

    @pytest.mark.parametrize("target_field", ["tmux_pane", "tmux_window_id"])
    def test_whitespace_tmux_target_uses_ordinary_pause_and_expiry(
        self,
        session_manager: SessionManager,
        sample_project: dict,
        target_field: str,
    ) -> None:
        session = session_manager.register(
            external_id=f"blank-tmux-{target_field}",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
            terminal_context={target_field: "   "},
        )
        session_manager.db.execute(
            "UPDATE sessions SET updated_at = NOW() - INTERVAL '25 hours' WHERE id = %s",
            (session.id,),
        )

        paused = session_manager.pause_inactive_active_sessions(timeout_minutes=30)
        expired = session_manager.expire_stale_sessions(timeout_hours=24)

        stale = session_manager.get(session.id)
        assert paused == 1
        assert expired == 1
        assert stale is not None
        assert stale.status == "expired"

    def test_expire_empty_sessions(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Zero-message active and paused sessions should fast-expire."""
        active_session = session_manager.register(
            external_id="empty-active",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        paused_session = session_manager.register(
            external_id="empty-paused",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )

        session_manager.update_status(paused_session.id, "paused")
        session_manager.db.execute(
            "UPDATE sessions SET updated_at = NOW() - INTERVAL '3 hours' WHERE id IN (%s, %s)",
            (active_session.id, paused_session.id),
        )

        count = session_manager.expire_empty_sessions(timeout_hours=2)
        assert count == 2

        active_after = session_manager.get(active_session.id)
        paused_after = session_manager.get(paused_session.id)
        assert active_after is not None
        assert paused_after is not None
        assert active_after.status == "expired"
        assert paused_after.status == "expired"

    def test_expire_empty_sessions_ignores_non_empty_and_non_active_statuses(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Fast-expire should skip sessions that are non-empty or already expired."""
        nonempty_active = session_manager.register(
            external_id="nonempty-active",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        expired_empty = session_manager.register(
            external_id="already-expired-empty",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )

        session_manager.update_stats(nonempty_active.id, message_count=1)
        session_manager.update_status(expired_empty.id, "expired")
        session_manager.db.execute(
            "UPDATE sessions SET updated_at = NOW() - INTERVAL '3 hours' WHERE id IN (%s, %s)",
            (nonempty_active.id, expired_empty.id),
        )

        count = session_manager.expire_empty_sessions(timeout_hours=2)
        assert count == 0

        nonempty_after = session_manager.get(nonempty_active.id)
        expired_after = session_manager.get(expired_empty.id)
        assert nonempty_after is not None
        assert expired_after is not None
        assert nonempty_after.status == "active"
        assert expired_after.status == "expired"

    def test_prune_empty_sessions(
        self,
        session_manager: SessionManager,
        sample_project: dict,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Prune should only hard-delete old expired zero-message sessions."""
        prune_me = session_manager.register(
            external_id="prune-me",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        recent_expired = session_manager.register(
            external_id="recent-expired",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        expired_nonempty = session_manager.register(
            external_id="expired-nonempty",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        paused_empty = session_manager.register(
            external_id="paused-empty",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )

        session_manager.update_stats(expired_nonempty.id, message_count=1)
        session_manager.update_status(prune_me.id, "expired")
        session_manager.update_status(recent_expired.id, "expired")
        session_manager.update_status(expired_nonempty.id, "expired")
        session_manager.update_status(paused_empty.id, "paused")
        session_manager.db.execute(
            "INSERT INTO session_variables (session_id) VALUES (%s)",
            (prune_me.id,),
        )
        session_manager.db.execute(
            """
            UPDATE sessions
            SET updated_at = CASE
                WHEN id = %s THEN NOW() - INTERVAL '2 hours'
                WHEN id = %s THEN NOW() - INTERVAL '30 minutes'
                WHEN id = %s THEN NOW() - INTERVAL '2 hours'
                WHEN id = %s THEN NOW() - INTERVAL '2 hours'
            END
            WHERE id IN (%s, %s, %s, %s)
            """,
            (
                prune_me.id,
                recent_expired.id,
                expired_nonempty.id,
                paused_empty.id,
                prune_me.id,
                recent_expired.id,
                expired_nonempty.id,
                paused_empty.id,
            ),
        )

        with caplog.at_level(logging.INFO, logger="gobby.storage.session_lifecycle"):
            count = session_manager.prune_empty_sessions(min_age_hours=1)
        assert count == 1
        records = [
            record
            for record in caplog.records
            if record.getMessage().startswith("Pruned 1 empty ghost session")
        ]
        assert len(records) == 1
        assert records[0].levelno == logging.INFO

        assert session_manager.get(prune_me.id) is None
        assert (
            session_manager.db.fetchone(
                "SELECT session_id FROM session_variables WHERE session_id = %s",
                (prune_me.id,),
            )
            is None
        )
        recent_after = session_manager.get(recent_expired.id)
        nonempty_after = session_manager.get(expired_nonempty.id)
        paused_after = session_manager.get(paused_empty.id)
        assert recent_after is not None
        assert nonempty_after is not None
        assert paused_after is not None
        assert recent_after.status == "expired"
        assert nonempty_after.status == "expired"
        assert paused_after.status == "paused"

    def test_prune_empty_sessions_preserves_transcript_backed_session(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        session = session_manager.register(
            external_id="transcript-backed",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
            transcript_path="/tmp/transcript-backed.jsonl",
        )
        session_manager.update_status(session.id, "expired")
        session_manager.db.execute(
            "UPDATE sessions SET updated_at = NOW() - INTERVAL '2 hours' WHERE id = %s",
            (session.id,),
        )

        count = session_manager.prune_empty_sessions(min_age_hours=1)

        assert count == 0
        assert session_manager.get(session.id) is not None

    def test_prune_empty_sessions_skips_retained_references(
        self,
        session_manager: SessionManager,
        sample_project: dict,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Prune should skip empty expired sessions still referenced by retained history."""
        child_parent = session_manager.register(
            external_id="child-parent",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        task_ref = session_manager.register(
            external_id="task-ref",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        memory_ref = session_manager.register(
            external_id="memory-ref",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        agent_run_ref = session_manager.register(
            external_id="agent-run-ref",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )

        for session_id in (child_parent.id, task_ref.id, memory_ref.id, agent_run_ref.id):
            session_manager.update_status(session_id, "expired")

        session_manager.db.execute(
            """
            UPDATE sessions
            SET updated_at = NOW() - INTERVAL '2 hours'
            WHERE id IN (%s, %s, %s, %s)
            """,
            (child_parent.id, task_ref.id, memory_ref.id, agent_run_ref.id),
        )

        session_manager.register(
            external_id="child-session",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
            parent_session_id=child_parent.id,
        )
        session_manager.db.execute(
            """
            INSERT INTO tasks (
                id, project_id, title, validation_criteria,
                created_in_session_id, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                str(uuid4()),
                sample_project["id"],
                "Retained task history",
                "Retained task remains referenced.",
                task_ref.id,
            ),
        )
        session_manager.db.execute(
            """
            INSERT INTO memories (
                id, project_id, memory_type, content, source_session_id, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                str(uuid4()),
                sample_project["id"],
                "context",
                "Retained memory history",
                memory_ref.id,
            ),
        )
        session_manager.db.execute(
            """
            INSERT INTO agent_runs (
                id, machine_id, parent_session_id, provider, prompt, status,
                created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                str(uuid4()),
                LOCAL_MACHINE_ID,
                agent_run_ref.id,
                "claude",
                "retained prompt",
                "success",
            ),
        )

        logger_name = "gobby.storage.session_lifecycle"
        with caplog.at_level(logging.DEBUG, logger=logger_name):
            count = session_manager.prune_empty_sessions(min_age_hours=1)
        assert count == 0
        records = [
            record
            for record in caplog.records
            if record.getMessage().startswith("Skipped pruning 4 empty ghost sessions")
        ]
        assert len(records) == 1
        assert records[0].levelno == logging.DEBUG

        caplog.clear()
        with caplog.at_level(logging.INFO, logger=logger_name):
            assert session_manager.prune_empty_sessions(min_age_hours=1) == 0
        assert not any(
            record.getMessage().startswith("Skipped pruning 4 empty ghost sessions")
            for record in caplog.records
        )

        assert session_manager.get(child_parent.id) is not None
        assert session_manager.get(task_ref.id) is not None
        assert session_manager.get(memory_ref.id) is not None
        assert session_manager.get(agent_run_ref.id) is not None

    @pytest.mark.asyncio
    async def test_workflow_audit_maintenance_unblocks_empty_session_pruning(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        from gobby.runner_maintenance_audit import workflow_audit_cleanup_loop

        session = session_manager.register(
            external_id="old-audit-ref",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="qwen",
            project_id=sample_project["id"],
        )
        session_manager.update_status(session.id, "expired")
        session_manager.db.execute(
            "UPDATE sessions SET updated_at = NOW() - INTERVAL '2 hours' WHERE id = %s",
            (session.id,),
        )
        session_manager.db.execute(
            """
            INSERT INTO workflow_audit_log (session_id, timestamp, step, event_type, result)
            VALUES (%s, NOW() - INTERVAL '8 days', %s, %s, %s)
            """,
            (session.id, "old-step", "transition", "success"),
        )

        assert session_manager.prune_empty_sessions(min_age_hours=1) == 0

        shutdown_requested = False

        async def stop_after_cycle(_seconds: float) -> None:
            nonlocal shutdown_requested
            shutdown_requested = True

        from types import SimpleNamespace
        from typing import Any, cast

        bundle = SimpleNamespace(
            snapshot=SimpleNamespace(
                active=SimpleNamespace(
                    session_lifecycle=SimpleNamespace(workflow_audit_retention_days=7)
                )
            )
        )
        await workflow_audit_cleanup_loop(
            session_manager.db,
            lambda: shutdown_requested,
            capture_bundle=cast(Any, lambda: bundle),
            interval_seconds=0,
            sleep=stop_after_cycle,
        )

        audit_count = session_manager.db.fetchone(
            "SELECT COUNT(*) AS count FROM workflow_audit_log WHERE session_id = %s",
            (session.id,),
        )
        assert audit_count is not None
        assert audit_count["count"] == 0
        assert session_manager.prune_empty_sessions(min_age_hours=1) == 1
        assert session_manager.get(session.id) is None

    def test_prune_empty_sessions_large_batch_preserves_retained_refs(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Large prune batches should delete stale empties while keeping referenced rows."""
        retained_parent = session_manager.register(
            external_id="retained-parent",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        retained_memory = session_manager.register(
            external_id="retained-memory",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )

        stale_sessions = [
            session_manager.register(
                external_id=f"bulk-prune-{index}",
                machine_id="20000000-0000-4000-8000-000000000001",
                source="claude",
                project_id=sample_project["id"],
            )
            for index in range(200)
        ]

        stale_ids = [retained_parent.id, retained_memory.id, *(s.id for s in stale_sessions)]
        for session_id in stale_ids:
            session_manager.update_status(session_id, "expired")

        placeholders = ", ".join("%s" for _ in stale_ids)
        session_manager.db.execute(
            f"""
            UPDATE sessions
            SET updated_at = NOW() - INTERVAL '3 hours'
            WHERE id IN ({placeholders})
            """,
            tuple(stale_ids),
        )

        session_manager.register(
            external_id="retained-child",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
            parent_session_id=retained_parent.id,
        )
        session_manager.db.execute(
            """
            INSERT INTO memories (
                id, project_id, memory_type, content, source_session_id, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (
                str(uuid4()),
                sample_project["id"],
                "context",
                "Retained memory history",
                retained_memory.id,
            ),
        )

        count = session_manager.prune_empty_sessions(min_age_hours=1)

        assert count == len(stale_sessions)
        assert session_manager.get(retained_parent.id) is not None
        assert session_manager.get(retained_memory.id) is not None
        assert all(session_manager.get(session.id) is None for session in stale_sessions)

    def test_expire_stale_sessions_no_stale(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test expire_stale_sessions returns 0 when no stale sessions."""
        session_manager.register(
            external_id="fresh-session",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )

        count = session_manager.expire_stale_sessions(timeout_hours=24)
        assert count == 0

    def test_pause_inactive_active_sessions_no_inactive(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test pause_inactive_active_sessions returns 0 when no inactive sessions."""
        session_manager.register(
            external_id="active-session",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )

        count = session_manager.pause_inactive_active_sessions(timeout_minutes=30)
        assert count == 0

    def test_get_pending_transcript_sessions_with_limit(
        self,
        session_manager: SessionManager,
        sample_project: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test get_pending_transcript_sessions respects limit."""
        # Create multiple expired sessions with transcript_path
        for i in range(5):
            session = session_manager.register(
                external_id=f"pending-{i}",
                machine_id="20000000-0000-4000-8000-000000000001",
                source="claude",
                project_id=sample_project["id"],
                transcript_path=f"/tmp/transcript-{i}.jsonl",
            )
            session_manager.update_status(session.id, "expired")

        monkeypatch.setattr(
            "gobby.storage.sessions._transcript.get_machine_id",
            lambda: "20000000-0000-4000-8000-000000000001",
        )
        pending = session_manager.get_pending_transcript_sessions(limit=3)
        assert len(pending) == 3

    def test_get_pending_transcript_sessions_excludes_processed(
        self,
        session_manager: SessionManager,
        sample_project: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test that get_pending_transcript_sessions excludes processed sessions."""
        session = session_manager.register(
            external_id="processed-session",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
            transcript_path="/tmp/transcript.jsonl",
        )
        session_manager.update_status(session.id, "expired")
        session_manager.mark_transcript_processed(session.id)

        monkeypatch.setattr(
            "gobby.storage.sessions._transcript.get_machine_id",
            lambda: "20000000-0000-4000-8000-000000000001",
        )
        pending = session_manager.get_pending_transcript_sessions()
        assert len(pending) == 0

    def test_get_pending_transcript_sessions_excludes_no_jsonl(
        self,
        session_manager: SessionManager,
        sample_project: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test that get_pending_transcript_sessions excludes sessions without transcript_path."""
        session = session_manager.register(
            external_id="no-jsonl-session",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
            transcript_path=None,  # No transcript path
        )
        session_manager.update_status(session.id, "expired")

        monkeypatch.setattr(
            "gobby.storage.sessions._transcript.get_machine_id",
            lambda: "20000000-0000-4000-8000-000000000001",
        )
        pending = session_manager.get_pending_transcript_sessions()
        assert len(pending) == 0
