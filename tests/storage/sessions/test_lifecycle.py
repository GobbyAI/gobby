"""Focused tests for session storage behavior."""

import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.memories import LocalMemoryManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.terminal_ownership import terminal_session_identity
from gobby.workflows.definitions import WorkflowInstance
from gobby.workflows.state_manager import WorkflowInstanceManager

pytestmark = pytest.mark.unit


class TestSessionManagerLifecycle:
    """Tests split from the SessionManager storage monolith."""

    def test_compact_reregistration_reactivates_row_in_place(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """A compact restart reuses the same row: same id, state untouched."""
        session = session_manager.register(
            external_id="compact-in-place",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        run_manager = LocalAgentRunManager(session_manager.db)
        run = run_manager.create(
            parent_session_id=session.id,
            child_session_id=session.id,
            provider="claude",
            prompt="Continue compacted work",
        )
        session_manager.db.execute(
            "UPDATE sessions SET agent_run_id = %s WHERE id = %s",
            (run.id, session.id),
        )
        workflow_manager = WorkflowInstanceManager(session_manager.db)
        workflow_manager.save_instance(
            WorkflowInstance(
                id=str(uuid.uuid4()),
                session_id=session.id,
                workflow_name="developer",
                current_step="implement",
            )
        )
        session_manager.update_status(session.id, "handoff_ready")

        reactivated = session_manager.register(
            external_id="compact-in-place",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        assert reactivated.id == session.id
        assert reactivated.status == "active"
        assert reactivated.agent_run_id == run.id
        instances = workflow_manager.get_active_instances(session.id)
        assert [instance.workflow_name for instance in instances] == ["developer"]
        preserved_run = run_manager.get(run.id)
        assert preserved_run is not None
        assert preserved_run.child_session_id == session.id

    def test_reregistration_preserves_parent_linkage_by_default(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Re-registering without a parent argument must not clear stored linkage."""
        parent = session_manager.register(
            external_id="spawn-parent",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        child = session_manager.register(
            external_id="spawn-child",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            parent_session_id=parent.id,
        )
        assert child.parent_session_id == parent.id

        reregistered = session_manager.register(
            external_id="spawn-child",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        assert reregistered.id == child.id
        assert reregistered.parent_session_id == parent.id

    def test_update_status(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Test updating session status."""
        session = session_manager.register(
            external_id="status-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        assert session.status == "active"

        updated = session_manager.update_status(session.id, "paused")
        assert updated is not None
        assert updated.status == "paused"

    def test_expire_if_active_does_not_overwrite_handoff_ready(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        session = session_manager.register(
            external_id="conditional-expiry-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.update_status(session.id, "handoff_ready")

        assert session_manager.expire_if_active(session.id) is None
        preserved = session_manager.get(session.id)
        assert preserved is not None
        assert preserved.status == "handoff_ready"

    def test_expire_if_active_updates_active_session(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        session = session_manager.register(
            external_id="active-expiry-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        expired = session_manager.expire_if_active(session.id)

        assert expired is not None
        assert expired.status == "expired"

    @pytest.mark.parametrize("bulk", [False, True])
    def test_status_updates_reject_unknown_values(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
        *,
        bulk: bool,
    ) -> None:
        session = session_manager.register(
            external_id=f"invalid-status-{bulk}",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        with pytest.raises(ValueError, match="Invalid session status 'immortal'"):
            if bulk:
                session_manager.update(session.id, status="immortal")
            else:
                session_manager.update_status(session.id, "immortal")

    @pytest.mark.parametrize("terminal_status", ["expired", "deleted"])
    @pytest.mark.parametrize("bulk", [False, True])
    @pytest.mark.parametrize("new_status", ["active", "paused"])
    def test_status_updates_reject_transitions_out_of_terminal_states(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
        terminal_status: str,
        *,
        bulk: bool,
        new_status: str,
    ) -> None:
        session = session_manager.register(
            external_id=f"terminal-transition-{terminal_status}-{bulk}",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.update_status(session.id, terminal_status)

        with pytest.raises(ValueError, match="Cannot transition terminal session status"):
            if bulk:
                session_manager.update(session.id, status=new_status)
            else:
                session_manager.update_status(session.id, new_status)

    @pytest.mark.parametrize("activity_status", ["active", "paused"])
    def test_activity_status_revives_expired_session(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
        activity_status: str,
    ) -> None:
        session = session_manager.register(
            external_id=f"activity-revival-{activity_status}",
            machine_id="machine",
            source="codex",
            project_id=sample_project["id"],
        )
        session_manager.update_status(session.id, "expired")
        session_manager.mark_transcript_processed(session.id)
        session_manager.db.execute(
            "UPDATE sessions SET updated_at = NOW() - INTERVAL '1 hour' WHERE id = %s",
            (session.id,),
        )
        before = session_manager.get(session.id)
        assert before is not None
        events: list[tuple[str, str]] = []
        session_manager.register_session_change_listener(
            lambda event, session_id: events.append((event, session_id))
        )

        updated = session_manager.update_status_from_activity(session.id, activity_status)

        assert updated is not None
        assert updated.status == activity_status
        assert updated.updated_at != before.updated_at
        row = session_manager.db.fetchone(
            "SELECT transcript_processed FROM sessions WHERE id = %s",
            (session.id,),
        )
        assert row is not None
        assert row["transcript_processed"] is False
        assert events == [("session_updated", session.id)]

    @pytest.mark.parametrize("activity_status", ["active", "paused"])
    def test_activity_status_does_not_revive_deleted_session(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
        activity_status: str,
    ) -> None:
        session = session_manager.register(
            external_id=f"deleted-activity-{activity_status}",
            machine_id="machine",
            source="codex",
            project_id=sample_project["id"],
        )
        session_manager.update_status(session.id, "deleted")
        events: list[tuple[str, str]] = []
        session_manager.register_session_change_listener(
            lambda event, session_id: events.append((event, session_id))
        )

        updated = session_manager.update_status_from_activity(session.id, activity_status)

        assert updated is None
        persisted = session_manager.get(session.id)
        assert persisted is not None
        assert persisted.status == "deleted"
        assert events == []

    @pytest.mark.parametrize("invalid_status", ["expired", "completed"])
    def test_activity_status_rejects_non_activity_targets(
        self,
        session_manager: SessionManager,
        invalid_status: str,
    ) -> None:
        with pytest.raises(ValueError, match="Confirmed activity status"):
            session_manager.update_status_from_activity("missing-session", invalid_status)

    def test_activity_status_returns_none_for_missing_session(
        self,
        session_manager: SessionManager,
    ) -> None:
        events: list[tuple[str, str]] = []
        session_manager.register_session_change_listener(
            lambda event, session_id: events.append((event, session_id))
        )

        assert session_manager.update_status_from_activity(str(uuid.uuid4()), "active") is None
        assert events == []

    @pytest.mark.parametrize("activity_status", ["active", "paused"])
    def test_activity_status_idempotently_refreshes_live_session(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
        activity_status: str,
    ) -> None:
        session = session_manager.register(
            external_id=f"idempotent-activity-{activity_status}",
            machine_id="machine",
            source="codex",
            project_id=sample_project["id"],
        )
        if activity_status == "paused":
            session_manager.update_status(session.id, "paused")
        session_manager.mark_transcript_processed(session.id)
        session_manager.db.execute(
            "UPDATE sessions SET updated_at = NOW() - INTERVAL '1 hour' WHERE id = %s",
            (session.id,),
        )
        before = session_manager.get(session.id)
        assert before is not None
        events: list[tuple[str, str]] = []
        session_manager.register_session_change_listener(
            lambda event, session_id: events.append((event, session_id))
        )

        updated = session_manager.update_status_from_activity(session.id, activity_status)

        assert updated is not None
        assert updated.status == activity_status
        assert updated.updated_at != before.updated_at
        row = session_manager.db.fetchone(
            "SELECT transcript_processed FROM sessions WHERE id = %s",
            (session.id,),
        )
        assert row is not None
        assert row["transcript_processed"] is False
        assert events == [("session_updated", session.id)]

    def test_list_sessions(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Test listing sessions."""
        session_manager.register(
            external_id="list-1",
            machine_id="m1",
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.register(
            external_id="list-2",
            machine_id="m2",
            source="qwen",
            project_id=sample_project["id"],
        )

        sessions = session_manager.list(project_id=sample_project["id"])
        assert len(sessions) == 2

    def test_list_with_filters(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Test listing sessions with filters."""
        s1 = session_manager.register(
            external_id="filter-1",
            machine_id="m1",
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.register(
            external_id="filter-2",
            machine_id="m2",
            source="qwen",
            project_id=sample_project["id"],
        )
        session_manager.update_status(s1.id, "paused")

        # Filter by source
        claude_sessions = session_manager.list(source="claude")
        assert len(claude_sessions) == 1
        assert claude_sessions[0].source == "claude"

        # Filter by status
        paused_sessions = session_manager.list(status="paused")
        assert len(paused_sessions) == 1
        assert paused_sessions[0].status == "paused"

    def test_list_with_limit(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Test listing sessions with limit."""
        for i in range(5):
            session_manager.register(
                external_id=f"limit-{i}",
                machine_id=f"m{i}",
                source="claude",
                project_id=sample_project["id"],
            )

        sessions = session_manager.list(limit=3)
        assert len(sessions) == 3

    def test_delete_session(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Test deleting a session."""
        session = session_manager.register(
            external_id="delete-me",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        result = session_manager.delete(session.id)
        assert result is True
        assert session_manager.get(session.id) is None

    def test_delete_session_preserves_sourced_memory(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        session = session_manager.register(
            external_id="delete-memory-source",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        memory_manager = LocalMemoryManager(session_manager.db)
        memory = memory_manager.create_memory(
            content="Provenance can be cleared without deleting this content",
            project_id=sample_project["id"],
            source_session_id=session.id,
        )

        assert session_manager.delete(session.id) is True

        surviving_memory = memory_manager.get_memory(memory.id)
        assert surviving_memory.content == memory.content
        assert surviving_memory.source_session_id is None

    def test_delete_nonexistent(self, session_manager: SessionManager) -> None:
        """Test deleting nonexistent session returns False."""
        result = session_manager.delete(str(uuid.uuid4()))
        assert result is False

    def test_transcript_processing_lifecycle(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Test transcript processing lifecycle methods."""
        # Create expired session with transcript_path
        session = session_manager.register(
            external_id="transcript-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            transcript_path="/tmp/test.jsonl",
        )
        session_manager.update_status(session.id, "expired")

        # Should be pending
        pending = session_manager.get_pending_transcript_sessions()
        assert len(pending) == 1
        assert pending[0].id == session.id

        # Mark processed
        updated = session_manager.mark_transcript_processed(session.id)
        assert updated is not None
        # Verify it's no longer pending
        pending = session_manager.get_pending_transcript_sessions()
        assert len(pending) == 0

        # Reset processed
        reset = session_manager.reset_transcript_processed(session.id)
        assert reset is not None

        # Should be pending again
        pending = session_manager.get_pending_transcript_sessions()
        assert len(pending) == 1

    def test_revive_expired_terminal_session(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Fresh activity revives expired terminal sessions and reopens transcript processing."""
        session = session_manager.register(
            external_id="revive-test",
            machine_id="machine",
            source="codex",
            project_id=sample_project["id"],
            transcript_path="/tmp/test.jsonl",
        )
        session_manager.update_status(session.id, "expired")
        session_manager.mark_transcript_processed(session.id)

        revived = session_manager.revive_expired_terminal_session(session.id)

        assert revived is not None
        assert revived.status == "active"
        row = session_manager.db.fetchone(
            "SELECT transcript_processed FROM sessions WHERE id = %s",
            (session.id,),
        )
        assert row is not None
        assert row["transcript_processed"] == 0
        pending = session_manager.get_pending_transcript_sessions()
        assert pending == []

    def test_revive_superseded_terminal_session_keeps_newest_owner_active(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Delayed activity stays attributed to the expired historical session."""
        terminal_context = {
            "tmux_pane": "%154",
            "tmux_socket_path": "/tmp/tmux-501/default",
        }
        older = session_manager.register(
            external_id="stale-hook-session",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            terminal_context=terminal_context,
        )
        session_manager.update_status(older.id, "expired")
        session_manager.mark_transcript_processed(older.id)
        newer = session_manager.register(
            external_id="live-hook-session",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            terminal_context=terminal_context,
        )
        caplog.set_level("DEBUG", logger="gobby.storage.sessions")

        revived = session_manager.revive_expired_terminal_session(older.id)

        assert revived is not None
        assert revived.status == "expired"
        live_owner = session_manager.get(newer.id)
        assert live_owner is not None
        assert live_owner.status == "active"
        transcript_row = session_manager.db.fetchone(
            "SELECT transcript_processed FROM sessions WHERE id = %s",
            (older.id,),
        )
        assert transcript_row is not None
        assert transcript_row["transcript_processed"] == 1
        suppressed = [
            record
            for record in caplog.records
            if getattr(record, "event", None) == "terminal_session_revival_suppressed"
        ]
        assert len(suppressed) == 1
        assert suppressed[0].levelname == "DEBUG"
        assert getattr(suppressed[0], "session_id", None) == older.id
        assert getattr(suppressed[0], "terminal_owner_session_id", None) == newer.id

    def test_newest_terminal_activity_demotes_reactivated_older_owner(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A false-expired newest session reclaims its terminal from an older row."""
        terminal_context = {
            "tmux_pane": "%154",
            "tmux_socket_path": "/tmp/tmux-501/default",
        }
        older = session_manager.register(
            external_id="reactivated-old-session",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            terminal_context=terminal_context,
        )
        newer = session_manager.register(
            external_id="false-expired-new-session",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            terminal_context=terminal_context,
        )
        session_manager.update_status(newer.id, "expired")
        caplog.set_level("INFO", logger="gobby.storage.sessions")
        notifications: list[tuple[str, str]] = []
        monkeypatch.setattr(
            session_manager,
            "_notify_session_change",
            lambda event, session_id: notifications.append((event, session_id)),
        )

        revived = session_manager.revive_expired_terminal_session(newer.id)

        assert revived is not None
        assert revived.status == "active"
        superseded = session_manager.get(older.id)
        assert superseded is not None
        assert superseded.status == "expired"
        ownership_changes = [
            record
            for record in caplog.records
            if getattr(record, "event", None) == "terminal_session_owner_superseded"
        ]
        assert len(ownership_changes) == 1
        assert ownership_changes[0].levelname == "INFO"
        assert getattr(ownership_changes[0], "session_id", None) == older.id
        assert getattr(ownership_changes[0], "terminal_owner_session_id", None) == newer.id
        assert ("session_expired", older.id) in notifications

    def test_terminal_revival_uses_locked_status_for_transcript_reset(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        current = session_manager.register(
            external_id="active-after-stale-snapshot",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            terminal_context={
                "tmux_pane": "%155",
                "tmux_socket_path": "/tmp/tmux-501/default",
            },
        )
        session_manager.db.execute(
            "UPDATE sessions SET transcript_processed = TRUE WHERE id = %s",
            (current.id,),
        )
        original_get = session_manager.get
        get_calls = 0

        def stale_first_get(session_id: str) -> Session | None:
            nonlocal get_calls
            get_calls += 1
            session = original_get(session_id)
            if get_calls == 1 and session is not None:
                return replace(session, status="expired")
            return session

        monkeypatch.setattr(session_manager, "get", stale_first_get)

        revived = session_manager.revive_expired_terminal_session(current.id)

        assert revived is not None
        assert revived.status == "active"
        transcript_row = session_manager.db.fetchone(
            "SELECT transcript_processed FROM sessions WHERE id = %s",
            (current.id,),
        )
        assert transcript_row is not None
        assert transcript_row["transcript_processed"] == 1

    @pytest.mark.parametrize(
        ("newer_machine_id", "newer_socket_path"),
        [
            ("other-machine", "/tmp/tmux-501/default"),
            ("machine", "/tmp/tmux-501/other"),
        ],
    )
    def test_terminal_ownership_does_not_cross_machine_or_socket(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
        newer_machine_id: str,
        newer_socket_path: str,
    ) -> None:
        """Identical pane IDs on distinct machines or sockets remain independent."""
        older = session_manager.register(
            external_id=f"older-{newer_machine_id}-{newer_socket_path}",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            terminal_context={
                "tmux_pane": "%154",
                "tmux_socket_path": "/tmp/tmux-501/default",
            },
        )
        session_manager.update_status(older.id, "expired")
        newer = session_manager.register(
            external_id=f"newer-{newer_machine_id}-{newer_socket_path}",
            machine_id=newer_machine_id,
            source="claude",
            project_id=sample_project["id"],
            terminal_context={
                "tmux_pane": "%154",
                "tmux_socket_path": newer_socket_path,
            },
        )

        revived = session_manager.revive_expired_terminal_session(older.id)

        assert revived is not None
        assert revived.status == "active"
        independent = session_manager.get(newer.id)
        assert independent is not None
        assert independent.status == "active"

    def test_find_by_terminal_identity_is_global_across_projects_providers_and_statuses(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
        tmp_path: Path,
    ) -> None:
        project_manager = LocalProjectManager(session_manager.db)
        other_project = project_manager.create("other-terminal-project", str(tmp_path))
        terminal_context = {
            "tmux_pane": "%226",
            "tmux_socket_path": "/tmp/tmux-501/gobby",
        }
        codex = session_manager.register(
            external_id="terminal-owner-codex",
            machine_id="machine",
            source="codex",
            project_id=sample_project["id"],
            terminal_context=terminal_context,
        )
        grok = session_manager.register(
            external_id="terminal-owner-grok",
            machine_id="machine",
            source="grok",
            project_id=other_project.id,
            terminal_context=terminal_context,
        )
        session_manager.update_status(grok.id, "expired")
        session_manager.register(
            external_id="other-terminal-socket",
            machine_id="machine",
            source="qwen",
            project_id=sample_project["id"],
            terminal_context={
                "tmux_pane": "%226",
                "tmux_socket_path": "/tmp/tmux-501/default",
            },
        )

        identity = terminal_session_identity(codex)
        assert identity is not None
        matches = session_manager.find_by_terminal_identity(identity)

        assert {session.id for session in matches} == {codex.id, grok.id}

    @pytest.mark.parametrize("resumable_status", ["paused", "expired"])
    def test_activate_web_chat_session_activates_resumable_row(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
        resumable_status: str,
    ) -> None:
        session = session_manager.register(
            external_id=f"web-chat-activate-{resumable_status}",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            session_type="web_chat",
        )
        session_manager.update_status(session.id, resumable_status)

        activated = session_manager.activate_web_chat_session(session.id)

        assert activated is not None
        assert activated.status == "active"

    @pytest.mark.parametrize(
        ("session_type", "terminal_status"),
        [("terminal", "expired"), ("web_chat", "deleted")],
    )
    def test_activate_web_chat_session_preserves_ineligible_row(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
        session_type: str,
        terminal_status: str,
    ) -> None:
        session = session_manager.register(
            external_id=f"web-chat-ineligible-{session_type}-{terminal_status}",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            session_type=session_type,
        )
        session_manager.update_status(session.id, terminal_status)

        unchanged = session_manager.activate_web_chat_session(session.id)

        assert unchanged is not None
        assert unchanged.status == terminal_status
        assert unchanged.session_type == session_type

    def test_mark_transcript_processed_does_not_clobber_revival(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """A stale finalizer cannot mark a concurrently revived session processed."""
        session = session_manager.register(
            external_id="revival-race-test",
            machine_id="machine",
            source="codex",
            project_id=sample_project["id"],
            transcript_path="/tmp/test.jsonl",
        )
        session_manager.update_status(session.id, "expired")

        revived = session_manager.revive_expired_terminal_session(session.id)
        updated = session_manager.mark_transcript_processed(session.id)

        assert revived is not None
        assert revived.status == "active"
        assert updated is not None
        assert updated.status == "active"
        row = session_manager.db.fetchone(
            "SELECT transcript_processed FROM sessions WHERE id = %s",
            (session.id,),
        )
        assert row is not None
        assert row["transcript_processed"] == 0

    def test_update_parent_session_id(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Test updating parent session ID."""
        session = session_manager.register(
            external_id="child",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        parent = session_manager.register(
            external_id="parent",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        updated = session_manager.update_parent_session_id(session.id, parent.id)
        assert updated is not None
        assert updated.parent_session_id == parent.id

        cleared = session_manager.update_parent_session_id(session.id, None)
        assert cleared is not None
        assert cleared.parent_session_id is None

    def test_update_parent_session_id_ignores_self_parent(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Storage ignores direct updates that would self-parent a session."""
        session = session_manager.register(
            external_id="compact-session",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.update_status(session.id, "handoff_ready")

        parent = session_manager.find_parent(
            machine_id="machine",
            project_id=sample_project["id"],
            source="claude",
            status="handoff_ready",
        )
        assert parent is not None
        assert parent.id == session.id

        updated = session_manager.update_parent_session_id(session.id, session.id)
        assert updated is not None
        assert updated.parent_session_id is None

    def test_find_children(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Test finding child sessions of a parent."""
        parent = session_manager.register(
            external_id="parent-session",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        # Create child sessions
        child1 = session_manager.register(
            external_id="child-1",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            parent_session_id=parent.id,
        )
        child2 = session_manager.register(
            external_id="child-2",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            parent_session_id=parent.id,
        )

        children = session_manager.find_children(parent.id)

        assert len(children) == 2
        child_ids = [c.id for c in children]
        assert child1.id in child_ids
        assert child2.id in child_ids

    def test_find_children_no_children(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Test find_children returns empty list when no children."""
        session = session_manager.register(
            external_id="no-children",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        children = session_manager.find_children(session.id)
        assert children == []

    def test_update_multiple_fields(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Test updating multiple session fields at once."""
        session = session_manager.register(
            external_id="multi-update",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            title="Original Title",
        )

        updated = session_manager.update(
            session.id,
            external_id="new-ext-id",
            transcript_path="/new/path.jsonl",
            status="paused",
            title="New Title",
            git_branch="feature/branch",
        )

        assert updated is not None
        assert updated.external_id == "new-ext-id"
        assert updated.transcript_path == "/new/path.jsonl"
        assert updated.status == "paused"
        assert updated.title == "New Title"
        assert updated.git_branch == "feature/branch"

    def test_update_clears_nullable_metadata(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        session = session_manager.register(
            external_id="clear-metadata",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            title="Temporary title",
            transcript_path="/tmp/transcript.jsonl",
            git_branch="feature/temporary",
            title_source="manual",
        )

        updated = session_manager.update(
            session.id,
            title=None,
            title_source=None,
            transcript_path=None,
            git_branch=None,
        )

        assert updated is not None
        assert updated.title == "Temporary title"
        assert updated.title_source == "manual"
        assert updated.transcript_path is None
        assert updated.git_branch is None

    def test_update_single_field(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Test updating a single field."""
        session = session_manager.register(
            external_id="single-update",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        updated = session_manager.update(session.id, status="completed")

        assert updated is not None
        assert updated.status == "completed"

    def test_update_no_fields(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Test update with no fields returns session unchanged."""
        session = session_manager.register(
            external_id="no-update",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        result = session_manager.update(session.id)

        assert result is not None
        assert result.id == session.id

    def test_update_external_id_only(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Test updating just external_id."""
        session = session_manager.register(
            external_id="old-ext",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        updated = session_manager.update(session.id, external_id="new-ext")

        assert updated is not None
        assert updated.external_id == "new-ext"

    def test_update_transcript_path_only(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Test updating just transcript_path."""
        session = session_manager.register(
            external_id="jsonl-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        updated = session_manager.update(session.id, transcript_path="/updated/path.jsonl")

        assert updated is not None
        assert updated.transcript_path == "/updated/path.jsonl"

    def test_update_git_branch_only(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Test updating just git_branch."""
        session = session_manager.register(
            external_id="branch-test",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        updated = session_manager.update(session.id, git_branch="main")

        assert updated is not None
        assert updated.git_branch == "main"

    def test_count_sessions(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Test counting sessions."""
        session_manager.register(
            external_id="count-1",
            machine_id="m1",
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.register(
            external_id="count-2",
            machine_id="m2",
            source="qwen",
            project_id=sample_project["id"],
        )

        count = session_manager.count(project_id=sample_project["id"])
        assert count == 2

    def test_count_with_filters(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Test counting sessions with filters."""
        s1 = session_manager.register(
            external_id="count-filter-1",
            machine_id="m1",
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.register(
            external_id="count-filter-2",
            machine_id="m2",
            source="qwen",
            project_id=sample_project["id"],
        )
        session_manager.update_status(s1.id, "paused")

        # Count by source
        claude_count = session_manager.count(source="claude")
        assert claude_count == 1

        # Count by status
        paused_count = session_manager.count(status="paused")
        assert paused_count == 1

    def test_count_no_results(self, session_manager: SessionManager) -> None:
        """Test count returns 0 when no sessions match."""
        count = session_manager.count(project_id=str(uuid.uuid4()))
        assert count == 0

    def test_count_by_status(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Test counting sessions grouped by status."""
        s1 = session_manager.register(
            external_id="status-count-1",
            machine_id="m1",
            source="claude",
            project_id=sample_project["id"],
        )
        s2 = session_manager.register(
            external_id="status-count-2",
            machine_id="m2",
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.register(
            external_id="status-count-3",
            machine_id="m3",
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.update_status(s1.id, "paused")
        session_manager.update_status(s2.id, "paused")
        session_manager.update_status(s2.id, "deleted")

        counts = session_manager.count_by_status()

        # +1 active for the bootstrapped system session
        assert counts.get("active") == 2
        assert counts.get("paused") == 1
        assert "deleted" not in counts

        project_counts = session_manager.count_by_status(project_id=sample_project["id"])
        assert project_counts.get("active") == 1
        assert project_counts.get("paused") == 1
        assert "deleted" not in project_counts

    def test_count_by_status_empty(self, session_manager: SessionManager) -> None:
        """Test count_by_status with no user sessions (only bootstrapped system session)."""
        counts = session_manager.count_by_status()
        # The bootstrapped system session is always present
        assert counts == {"active": 1}

    def test_renumber_project_sessions_dry_run_leaves_rows_unchanged(
        self,
        session_manager: SessionManager,
        temp_db: HubDatabase,
    ) -> None:
        """Dry-run reports dense refs without mutating rows."""
        project = LocalProjectManager(temp_db).create(name="renumber-dry", repo_path="/tmp/dry")
        first = session_manager.register(
            external_id="dry-1",
            machine_id="m1",
            source="claude",
            project_id=project.id,
            title="First",
        )
        second = session_manager.register(
            external_id="dry-2",
            machine_id="m1",
            source="claude",
            project_id=project.id,
            title="Second",
        )
        with temp_db.transaction() as conn:
            conn.execute(
                "UPDATE sessions SET seq_num = %s, created_at = %s WHERE id = %s",
                (10, "2026-01-01T00:00:00+00:00", first.id),
            )
            conn.execute(
                "UPDATE sessions SET seq_num = %s, created_at = %s WHERE id = %s",
                (30, "2026-01-02T00:00:00+00:00", second.id),
            )

        mapping = session_manager.renumber_project_sessions(project.id, dry_run=True)

        assert [(item["old_seq_num"], item["new_seq_num"]) for item in mapping] == [
            (10, 1),
            (30, 2),
        ]
        refreshed_first = session_manager.get(first.id)
        refreshed_second = session_manager.get(second.id)
        assert refreshed_first is not None
        assert refreshed_second is not None
        assert refreshed_first.seq_num == 10
        assert refreshed_second.seq_num == 30

    def test_renumber_project_sessions_apply_is_project_scoped_and_tails_deleted(
        self,
        session_manager: SessionManager,
        temp_db: HubDatabase,
    ) -> None:
        """Apply compacts one project and moves retained deleted rows after visible rows."""
        project_manager = LocalProjectManager(temp_db)
        project = project_manager.create(name="renumber-apply", repo_path="/tmp/apply")
        other_project = project_manager.create(name="renumber-other", repo_path="/tmp/other")

        visible_first = session_manager.register(
            external_id="apply-1",
            machine_id="m1",
            source="claude",
            project_id=project.id,
            title="Visible first",
        )
        deleted = session_manager.register(
            external_id="apply-deleted",
            machine_id="m1",
            source="claude",
            project_id=project.id,
            title="Deleted",
        )
        visible_second = session_manager.register(
            external_id="apply-2",
            machine_id="m1",
            source="claude",
            project_id=project.id,
            title="Visible second",
        )
        other = session_manager.register(
            external_id="other-1",
            machine_id="m1",
            source="claude",
            project_id=other_project.id,
            title="Other",
        )

        with temp_db.transaction() as conn:
            conn.execute(
                "UPDATE sessions SET seq_num = %s, created_at = %s WHERE id = %s",
                (10, "2026-01-01T00:00:00+00:00", visible_first.id),
            )
            conn.execute(
                """
                UPDATE sessions
                SET seq_num = %s, created_at = %s, status = %s
                WHERE id = %s
                """,
                (20, "2026-01-02T00:00:00+00:00", "deleted", deleted.id),
            )
            conn.execute(
                "UPDATE sessions SET seq_num = %s, created_at = %s WHERE id = %s",
                (30, "2026-01-03T00:00:00+00:00", visible_second.id),
            )
            conn.execute("UPDATE sessions SET seq_num = %s WHERE id = %s", (50, other.id))

        mapping = session_manager.renumber_project_sessions(project.id, dry_run=False)

        assert [
            (item["session_id"], item["old_seq_num"], item["new_seq_num"], item["status"])
            for item in mapping
        ] == [
            (visible_first.id, 10, 1, "active"),
            (visible_second.id, 30, 2, "active"),
            (deleted.id, 20, 3, "deleted"),
        ]
        refreshed_visible_first = session_manager.get(visible_first.id)
        refreshed_visible_second = session_manager.get(visible_second.id)
        refreshed_deleted = session_manager.get(deleted.id)
        refreshed_other = session_manager.get(other.id)
        assert refreshed_visible_first is not None
        assert refreshed_visible_second is not None
        assert refreshed_deleted is not None
        assert refreshed_other is not None
        assert refreshed_visible_first.seq_num == 1
        assert refreshed_visible_second.seq_num == 2
        assert refreshed_deleted.seq_num == 3
        assert refreshed_other.seq_num == 50

    def test_list_without_filters(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, str],
    ) -> None:
        """Test listing all sessions without filters."""
        session_manager.register(
            external_id="list-all-1",
            machine_id="m1",
            source="claude",
            project_id=sample_project["id"],
        )
        session_manager.register(
            external_id="list-all-2",
            machine_id="m2",
            source="qwen",
            project_id=sample_project["id"],
        )

        sessions = session_manager.list()  # No filters
        assert len(sessions) >= 2
