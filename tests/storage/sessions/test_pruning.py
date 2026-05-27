"""Focused tests for session storage behavior."""

from uuid import uuid4

import pytest

from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


class TestSessionManagerPruning:
    """Tests split from the SessionManager storage monolith."""

    def test_expire_stale_sessions(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test expiring stale sessions."""
        # Create a stale session (simulated by mocking updated_at in query or just rely on db time)
        # Since we use PostgreSQL CURRENT_TIMESTAMP in queries, we can't easily mock time without
        # deeper mocking. Instead, we'll verify the SQL generation and execution flow
        # or use a very short timeout.

        session = session_manager.register(
            external_id="stale-session",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        # Manually backdate the session in DB
        session_manager.db.execute(
            "UPDATE sessions SET updated_at = NOW() - INTERVAL '25 hours' WHERE id = %s",
            (session.id,),
        )

        count = session_manager.expire_stale_sessions(timeout_hours=24)
        assert count == 1

        expired = session_manager.get(session.id)
        assert expired.status == "expired"

    def test_pause_inactive_active_sessions(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test pausing inactive active sessions."""
        session = session_manager.register(
            external_id="active-idle",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        # Backdate
        session_manager.db.execute(
            "UPDATE sessions SET updated_at = NOW() - INTERVAL '31 minutes' WHERE id = %s",
            (session.id,),
        )

        count = session_manager.pause_inactive_active_sessions(timeout_minutes=30)
        assert count == 1

        paused = session_manager.get(session.id)
        assert paused.status == "paused"

    def test_pause_inactive_active_sessions_preserves_last_activity_time(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Auto-pausing should not make a stale session look freshly active."""
        session = session_manager.register(
            external_id="active-idle-preserve-time",
            machine_id="machine",
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
            machine_id="machine",
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

    def test_expire_empty_sessions(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Zero-message active and paused sessions should fast-expire."""
        active_session = session_manager.register(
            external_id="empty-active",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        paused_session = session_manager.register(
            external_id="empty-paused",
            machine_id="machine",
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
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        expired_empty = session_manager.register(
            external_id="already-expired-empty",
            machine_id="machine",
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
    ) -> None:
        """Prune should only hard-delete old expired zero-message sessions."""
        prune_me = session_manager.register(
            external_id="prune-me",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        recent_expired = session_manager.register(
            external_id="recent-expired",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        expired_nonempty = session_manager.register(
            external_id="expired-nonempty",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        paused_empty = session_manager.register(
            external_id="paused-empty",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        session_manager.update_stats(expired_nonempty.id, message_count=1)
        session_manager.update_status(prune_me.id, "expired")
        session_manager.update_status(recent_expired.id, "expired")
        session_manager.update_status(expired_nonempty.id, "expired")
        session_manager.update_status(paused_empty.id, "paused")
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

        count = session_manager.prune_empty_sessions(min_age_hours=1)
        assert count == 1

        assert session_manager.get(prune_me.id) is None
        recent_after = session_manager.get(recent_expired.id)
        nonempty_after = session_manager.get(expired_nonempty.id)
        paused_after = session_manager.get(paused_empty.id)
        assert recent_after is not None
        assert nonempty_after is not None
        assert paused_after is not None
        assert recent_after.status == "expired"
        assert nonempty_after.status == "expired"
        assert paused_after.status == "paused"

    def test_prune_empty_sessions_skips_retained_references(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Prune should skip empty expired sessions still referenced by retained history."""
        child_parent = session_manager.register(
            external_id="child-parent",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        task_ref = session_manager.register(
            external_id="task-ref",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        memory_ref = session_manager.register(
            external_id="memory-ref",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        agent_run_ref = session_manager.register(
            external_id="agent-run-ref",
            machine_id="machine",
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
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            parent_session_id=child_parent.id,
        )
        session_manager.db.execute(
            """
            INSERT INTO tasks (
                id, project_id, title, created_in_session_id, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (str(uuid4()), sample_project["id"], "Retained task history", task_ref.id),
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
                "note",
                "Retained memory history",
                memory_ref.id,
            ),
        )
        session_manager.db.execute(
            """
            INSERT INTO agent_runs (
                id, parent_session_id, provider, prompt, status, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (str(uuid4()), agent_run_ref.id, "claude", "retained prompt", "success"),
        )

        count = session_manager.prune_empty_sessions(min_age_hours=1)
        assert count == 0

        assert session_manager.get(child_parent.id) is not None
        assert session_manager.get(task_ref.id) is not None
        assert session_manager.get(memory_ref.id) is not None
        assert session_manager.get(agent_run_ref.id) is not None

    def test_prune_empty_sessions_large_batch_preserves_retained_refs(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Large prune batches should delete stale empties while keeping referenced rows."""
        retained_parent = session_manager.register(
            external_id="retained-parent",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        retained_memory = session_manager.register(
            external_id="retained-memory",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        stale_sessions = [
            session_manager.register(
                external_id=f"bulk-prune-{index}",
                machine_id="machine",
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
            machine_id="machine",
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
                "note",
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
            machine_id="machine",
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
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        count = session_manager.pause_inactive_active_sessions(timeout_minutes=30)
        assert count == 0

    def test_get_pending_transcript_sessions_with_limit(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test get_pending_transcript_sessions respects limit."""
        # Create multiple expired sessions with transcript_path
        for i in range(5):
            session = session_manager.register(
                external_id=f"pending-{i}",
                machine_id="machine",
                source="claude",
                project_id=sample_project["id"],
                transcript_path=f"/tmp/transcript-{i}.jsonl",
            )
            session_manager.update_status(session.id, "expired")

        pending = session_manager.get_pending_transcript_sessions(limit=3)
        assert len(pending) == 3

    def test_get_pending_transcript_sessions_excludes_processed(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test that get_pending_transcript_sessions excludes processed sessions."""
        session = session_manager.register(
            external_id="processed-session",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            transcript_path="/tmp/transcript.jsonl",
        )
        session_manager.update_status(session.id, "expired")
        session_manager.mark_transcript_processed(session.id)

        pending = session_manager.get_pending_transcript_sessions()
        assert len(pending) == 0

    def test_get_pending_transcript_sessions_excludes_no_jsonl(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test that get_pending_transcript_sessions excludes sessions without transcript_path."""
        session = session_manager.register(
            external_id="no-jsonl-session",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            transcript_path=None,  # No transcript path
        )
        session_manager.update_status(session.id, "expired")

        pending = session_manager.get_pending_transcript_sessions()
        assert len(pending) == 0
