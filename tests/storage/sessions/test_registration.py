"""Focused tests for session storage behavior."""

import inspect
from collections.abc import Sequence

import pytest

from gobby.storage.session_models import Session
from gobby.storage.sessions import SYSTEM_SESSION_ID, SessionManager
from gobby.storage.sessions import _crud as session_crud
from gobby.storage.sessions import _field_update as session_field_update
from gobby.storage.sessions import _upsert as session_upsert

pytestmark = pytest.mark.unit


def test_session_registration_boolean_case_is_postgres_safe() -> None:
    source = inspect.getsource(session_crud)
    upsert_source = inspect.getsource(session_upsert)

    assert "CASE WHEN ? THEN 1 ELSE is_local END" not in source
    assert "CASE WHEN ? THEN TRUE ELSE is_local END" not in source
    assert "is_local = %s" in source
    assert "WHEN ? = -1 THEN is_local" not in upsert_source
    assert "WHEN ? THEN TRUE" not in upsert_source
    assert "WHEN %s THEN %s" in upsert_source
    assert "%s, 0, 0, 0, 0, NULL" not in source
    assert "%s, FALSE, 0, 0, 0, NULL" in source


def test_session_had_edits_updates_use_boolean_literals() -> None:
    source = inspect.getsource(session_field_update)

    assert "had_edits = 1" not in source
    assert "had_edits = 0" not in source
    assert "had_edits = TRUE" in source
    assert "had_edits = FALSE" in source


def test_session_unique_conflict_detection_uses_integrity_error_args() -> None:
    """Session unique-conflict matching must use exception args, not masked str()."""

    class MaskedIntegrityError(Exception):
        def __str__(self) -> str:
            return "masked"

    assert session_upsert.is_session_unique_conflict(
        MaskedIntegrityError('duplicate key value violates unique constraint "idx_sessions_unique"')
    )
    assert not session_upsert.is_session_unique_conflict(
        MaskedIntegrityError(
            'duplicate key value violates unique constraint "idx_sessions_seq_num"'
        )
    )
    assert not session_upsert.is_session_unique_conflict(
        MaskedIntegrityError("UNIQUE constraint failed: other_table.external_id")
    )


def test_update_existing_session_can_set_clear_or_preserve_is_local(
    session_manager: SessionManager,
    sample_project: dict,
) -> None:
    session = session_manager.register(
        external_id="local-flag",
        machine_id="machine",
        source="codex",
        project_id=sample_project["id"],
        is_local=True,
    )

    with session_manager.db.transaction() as conn:
        cleared = session_upsert.update_existing_session(
            session_manager,
            conn,
            session,
            title=None,
            transcript_path=None,
            git_branch=None,
            parent_session_id=None,
            terminal_context_json=None,
            workflow_name=None,
            is_local=False,
            sandbox_enabled=None,
            sandbox_policy_hash=None,
            now="2026-05-22T00:00:00+00:00",
        )

    assert cleared.is_local is False

    with session_manager.db.transaction() as conn:
        preserved = session_upsert.update_existing_session(
            session_manager,
            conn,
            cleared,
            title=None,
            transcript_path=None,
            git_branch=None,
            parent_session_id=None,
            terminal_context_json=None,
            workflow_name=None,
            is_local=None,
            sandbox_enabled=None,
            sandbox_policy_hash=None,
            now="2026-05-22T00:00:01+00:00",
        )

    assert preserved.is_local is False


class _CaptureConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, sql: str, params: Sequence[object] = ()) -> object:
        self.calls.append((sql, tuple(params)))
        return object()


class _StaticSessionGetter:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, session_id: str) -> Session | None:
        return self.session if session_id == self.session.id else None


def _session_stub() -> Session:
    return Session(
        id="session-1",
        external_id="external-1",
        machine_id="machine-1",
        source="codex",
        project_id="project-1",
        title=None,
        status="active",
        transcript_path=None,
        summary_path=None,
        summary_markdown=None,
        git_branch=None,
        parent_session_id=None,
        created_at="2026-05-22T00:00:00+00:00",
        updated_at="2026-05-22T00:00:00+00:00",
    )


def test_update_existing_session_binds_is_local_as_booleans_for_postgres() -> None:
    session = _session_stub()
    conn = _CaptureConnection()

    session_upsert.update_existing_session(
        _StaticSessionGetter(session),
        conn,
        session,
        title=None,
        transcript_path=None,
        git_branch=None,
        parent_session_id=None,
        terminal_context_json=None,
        workflow_name=None,
        is_local=True,
        sandbox_enabled=True,
        sandbox_policy_hash=None,
        now="2026-05-22T00:00:01+00:00",
    )

    params = conn.calls[0][1]

    assert params[6:9] == (True, True, True)
    assert all(type(value) is bool for value in params[6:9])


def test_update_existing_session_preserve_is_local_uses_boolean_guard_param() -> None:
    session = _session_stub()
    conn = _CaptureConnection()

    session_upsert.update_existing_session(
        _StaticSessionGetter(session),
        conn,
        session,
        title=None,
        transcript_path=None,
        git_branch=None,
        parent_session_id=None,
        terminal_context_json=None,
        workflow_name=None,
        is_local=None,
        sandbox_enabled=None,
        sandbox_policy_hash=None,
        now="2026-05-22T00:00:01+00:00",
    )

    params = conn.calls[0][1]

    assert params[6:9] == (False, False, None)
    assert type(params[6]) is bool
    assert type(params[7]) is bool


class TestSessionManagerRegistration:
    """Tests split from the SessionManager storage monolith."""

    def test_register_session(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test registering a new session."""
        session = session_manager.register(
            external_id="session-123",
            machine_id="machine-abc",
            source="claude",
            project_id=sample_project["id"],
            title="My Session",
            transcript_path="/path/to/transcript.jsonl",
            git_branch="main",
        )

        assert session.id is not None
        assert session.external_id == "session-123"
        assert session.machine_id == "machine-abc"
        assert session.source == "claude"
        assert session.project_id == sample_project["id"]
        assert session.title == "My Session"
        assert session.status == "active"
        assert session.transcript_path == "/path/to/transcript.jsonl"
        assert session.git_branch == "main"

        # Verify stats columns
        assert session.message_count == 0
        assert session.turn_count == 0
        assert session.tool_call_count == 0
        assert session.last_assistant_content is None

    def test_register_recreates_missing_system_parent_session(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Register self-heals the system parent row before inserting children."""
        session_manager.db.execute("DELETE FROM sessions WHERE id = %s", (SYSTEM_SESSION_ID,))
        assert (
            session_manager.db.fetchone(
                "SELECT id FROM sessions WHERE id = %s", (SYSTEM_SESSION_ID,)
            )
            is None
        )

        session = session_manager.register(
            external_id="pipeline-child",
            machine_id="machine-abc",
            source="pipeline",
            project_id=sample_project["id"],
            parent_session_id=SYSTEM_SESSION_ID,
        )

        repaired = session_manager.db.fetchone(
            "SELECT id, external_id, source FROM sessions WHERE id = %s",
            (SYSTEM_SESSION_ID,),
        )
        assert repaired is not None
        assert repaired["external_id"] == "system"
        assert repaired["source"] == "system"
        assert session.parent_session_id == SYSTEM_SESSION_ID

    def test_register_session_has_stats_columns(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test that a newly registered session has the stats columns."""
        session = session_manager.register(
            external_id="stats-check",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        # Verify Session object has fields
        assert hasattr(session, "message_count")
        assert hasattr(session, "turn_count")
        assert hasattr(session, "tool_call_count")
        assert hasattr(session, "last_assistant_content")

        # Verify values from DB
        row = session_manager.db.fetchone("SELECT * FROM sessions WHERE id = %s", (session.id,))
        assert "message_count" in row.keys()
        assert "turn_count" in row.keys()
        assert "tool_call_count" in row.keys()
        assert "last_assistant_content" in row.keys()

        assert row["message_count"] == 0
        assert row["turn_count"] == 0
        assert row["tool_call_count"] == 0
        assert row["last_assistant_content"] is None

    def test_register_persists_sandbox_metadata(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        session = session_manager.register(
            external_id="sandboxed-session",
            machine_id="machine",
            source="codex",
            project_id=sample_project["id"],
            sandbox_enabled=True,
            sandbox_policy_hash="policy-abc",
        )

        assert session.sandbox_enabled is True
        assert session.sandbox_policy_hash == "policy-abc"

        reloaded = session_manager.get(session.id)
        assert reloaded is not None
        assert reloaded.sandbox_enabled is True
        assert reloaded.sandbox_policy_hash == "policy-abc"

    def test_register_preserves_unknown_sandbox_metadata_as_null(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        session = session_manager.register(
            external_id="unknown-sandbox-session",
            machine_id="machine",
            source="codex",
            project_id=sample_project["id"],
            sandbox_enabled=None,
            sandbox_policy_hash=None,
        )

        row = session_manager.db.fetchone(
            "SELECT sandbox_enabled, sandbox_policy_hash FROM sessions WHERE id = %s",
            (session.id,),
        )
        assert row is not None
        assert row["sandbox_enabled"] is None
        assert row["sandbox_policy_hash"] is None

        reloaded = session_manager.get(session.id)
        assert reloaded is not None
        assert reloaded.sandbox_enabled is None
        assert reloaded.sandbox_policy_hash is None

    def test_register_upserts_on_conflict(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test that register updates existing session on conflict."""
        # First registration
        session1 = session_manager.register(
            external_id="unique-key",
            machine_id="machine-1",
            source="claude",
            project_id=sample_project["id"],
            title="Original",
        )

        # Second registration with same key combo
        session2 = session_manager.register(
            external_id="unique-key",
            machine_id="machine-1",
            source="claude",
            project_id=sample_project["id"],
            title="Updated",
        )

        # Should be the same session with updated title
        assert session2.id == session1.id
        assert session2.title == "Updated"

    def test_register_existing_session_ignores_self_parent(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Existing session re-registration must not persist itself as parent."""
        session = session_manager.register(
            external_id="self-parent-update",
            machine_id="machine-1",
            source="codex",
            project_id=sample_project["id"],
        )

        updated = session_manager.register(
            external_id="self-parent-update",
            machine_id="machine-1",
            source="codex",
            project_id=sample_project["id"],
            parent_session_id=session.id,
        )

        assert updated.id == session.id
        assert updated.parent_session_id is None
        row = session_manager.db.fetchone(
            "SELECT parent_session_id FROM sessions WHERE id = %s",
            (session.id,),
        )
        assert row["parent_session_id"] is None

    def test_register_repairs_existing_self_parent_row(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Legacy corrupt self-parent rows are repaired during registration."""
        session = session_manager.register(
            external_id="corrupt-self-parent",
            machine_id="machine-1",
            source="codex",
            project_id=sample_project["id"],
        )
        session_manager.db.execute(
            "ALTER TABLE sessions DROP CONSTRAINT IF EXISTS sessions_parent_session_not_self"
        )
        session_manager.db.execute(
            "UPDATE sessions SET parent_session_id = id WHERE id = %s",
            (session.id,),
        )

        repaired = session_manager.register(
            external_id="corrupt-self-parent",
            machine_id="machine-1",
            source="codex",
            project_id=sample_project["id"],
        )

        assert repaired.id == session.id
        assert repaired.parent_session_id is None
        row = session_manager.db.fetchone(
            "SELECT parent_session_id FROM sessions WHERE id = %s",
            (session.id,),
        )
        assert row["parent_session_id"] is None

    def test_register_existing_session_persists_valid_parent_update(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Valid parent updates still persist on existing sessions."""
        child = session_manager.register(
            external_id="valid-parent-child",
            machine_id="machine-1",
            source="codex",
            project_id=sample_project["id"],
        )
        parent = session_manager.register(
            external_id="valid-parent",
            machine_id="machine-1",
            source="codex",
            project_id=sample_project["id"],
        )

        updated = session_manager.register(
            external_id="valid-parent-child",
            machine_id="machine-1",
            source="codex",
            project_id=sample_project["id"],
            parent_session_id=parent.id,
        )

        assert updated.id == child.id
        assert updated.parent_session_id == parent.id

    def test_register_existing_session_ignores_parent_chain_cycle(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Re-registration must ignore a parent update that would create a cycle."""
        root = session_manager.register(
            external_id="cycle-root",
            machine_id="machine-1",
            source="codex",
            project_id=sample_project["id"],
        )
        child = session_manager.register(
            external_id="cycle-child",
            machine_id="machine-1",
            source="codex",
            project_id=sample_project["id"],
            parent_session_id=root.id,
        )

        updated = session_manager.register(
            external_id="cycle-root",
            machine_id="machine-1",
            source="codex",
            project_id=sample_project["id"],
            parent_session_id=child.id,
        )

        assert updated.id == root.id
        assert updated.parent_session_id is None

    def test_register_recovers_legacy_unique_conflict_across_session_types(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Older DBs had uniqueness without session_type; reuse that row on conflict."""
        created = session_manager.register(
            external_id="runtime-key",
            machine_id="machine-1",
            source="codex",
            project_id=sample_project["id"],
            title="Web chat",
            session_type="web_chat",
        )
        session_manager.db.execute(
            """
            CREATE UNIQUE INDEX idx_sessions_unique_legacy_test
            ON sessions(external_id, machine_id, source, project_id)
            """
        )

        recovered = session_manager.register(
            external_id="runtime-key",
            machine_id="machine-1",
            source="codex",
            project_id=sample_project["id"],
            title="Recovered",
            session_type="terminal",
        )

        assert recovered.id == created.id
        assert recovered.session_type == "web_chat"
        assert recovered.title == "Recovered"

    def test_get_session(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test getting a session by ID."""
        created = session_manager.register(
            external_id="get-test",
            machine_id="machine",
            source="codex",
            project_id=sample_project["id"],
        )

        retrieved = session_manager.get(created.id)
        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.external_id == "get-test"

    def test_get_nonexistent(self, session_manager: SessionManager) -> None:
        """Test getting nonexistent session returns None."""
        result = session_manager.get("nonexistent-id")
        assert result is None

    def test_find_by_external_id(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test finding session by external_id, machine_id, project_id, source."""
        session = session_manager.register(
            external_id="findable",
            machine_id="my-machine",
            source="claude",
            project_id=sample_project["id"],
        )

        found = session_manager.find_by_external_id(
            external_id="findable",
            machine_id="my-machine",
            project_id=sample_project["id"],
            source="claude",
        )

        assert found is not None
        assert found.id == session.id

    def test_find_by_external_id_not_found(self, session_manager: SessionManager) -> None:
        """Test find_by_external_id returns None when not found."""
        result = session_manager.find_by_external_id(
            external_id="nonexistent",
            machine_id="machine",
            project_id="nonexistent-project",
            source="claude",
        )
        assert result is None

    @pytest.mark.unit
    def test_create_web_chat_session_sets_model_and_chat_mode(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        session = session_manager.create_web_chat_session(
            machine_id="machine",
            project_id=sample_project["id"],
            source="claude",
            title="Web Chat",
            model="claude-opus-4-5-20251101",
            chat_mode="accept_edits",
            sandbox_enabled=True,
            sandbox_policy_hash="policy-hash-123",
        )

        assert session.model == "claude-opus-4-5-20251101"
        assert session.chat_mode == "accept_edits"
        assert session.sandbox_enabled is True
        assert session.sandbox_policy_hash == "policy-hash-123"

        reloaded = session_manager.get(session.id)
        assert reloaded is not None
        assert reloaded.model == "claude-opus-4-5-20251101"
        assert reloaded.chat_mode == "accept_edits"
        assert reloaded.sandbox_enabled is True
        assert reloaded.sandbox_policy_hash == "policy-hash-123"

    def test_register_with_agent_depth_and_spawned_by(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test registering session with agent depth and spawned_by_agent_id."""
        session = session_manager.register(
            external_id="agent-session",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            agent_depth=2,
            spawned_by_agent_id="agent-abc",
        )

        assert session.agent_depth == 2
        assert session.spawned_by_agent_id == "agent-abc"

    def test_register_updates_metadata_on_existing_session(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test that register updates metadata when session exists."""
        # Create a parent session first for the foreign key
        parent = session_manager.register(
            external_id="parent-meta",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
        )

        # First registration without transcript_path or git_branch
        session1 = session_manager.register(
            external_id="update-meta",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            title=None,
            transcript_path=None,
            git_branch=None,
        )
        assert session1.transcript_path is None

        # Second registration with additional metadata
        session2 = session_manager.register(
            external_id="update-meta",
            machine_id="machine",
            source="claude",
            project_id=sample_project["id"],
            title="Updated Title",
            transcript_path="/new/path.jsonl",
            git_branch="feature/new",
            parent_session_id=parent.id,  # Use real parent session
        )

        # Same session, updated metadata
        assert session2.id == session1.id
        assert session2.title == "Updated Title"
        assert session2.transcript_path == "/new/path.jsonl"
        assert session2.git_branch == "feature/new"
        assert session2.parent_session_id == parent.id
        assert session2.status == "active"  # Status reset to active
