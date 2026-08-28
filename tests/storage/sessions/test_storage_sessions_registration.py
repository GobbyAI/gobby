"""Focused tests for session storage behavior."""

import inspect
import json
import logging
import uuid
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from typing import Any
from unittest.mock import patch

import pytest

from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager, system_session_external_id, system_session_id
from gobby.storage.sessions import _crud as session_crud
from gobby.storage.sessions import _session_metadata_update as session_metadata_update
from gobby.storage.sessions import _upsert as session_upsert
from gobby.storage.sessions import _web_chat_crud as session_web_chat_crud
from gobby.storage.sessions._title_defaults import (
    MANUAL_TITLE_SOURCE,
    PROVISIONAL_TITLE_SOURCE,
    manual_title_source,
)
from gobby.storage.sessions._update_sentinel import UNSET
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError

LOCAL_MACHINE_ID = "20000000-0000-4000-8000-000000000001"
FOREIGN_MACHINE_ID = "20000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id.get_machine_id", return_value=LOCAL_MACHINE_ID):
        yield


def test_registration_requires_local_machine_ownership(
    session_manager: SessionManager,
) -> None:
    with patch("gobby.utils.machine_id.get_machine_id", return_value=LOCAL_MACHINE_ID):
        local = session_manager.register(
            external_id="local-session",
            machine_id=None,
            source="codex",
            project_id=None,
        )
        assert local.machine_id == LOCAL_MACHINE_ID

        with pytest.raises(MachineOwnershipMismatchError):
            session_manager.register(
                external_id="explicit-foreign-session",
                machine_id=FOREIGN_MACHINE_ID,
                source="codex",
                project_id=None,
            )

        with pytest.raises(MachineOwnershipMismatchError):
            session_manager.register(
                external_id="synthetic-machine-session",
                machine_id="web",
                source="web",
                project_id=None,
            )

    with patch("gobby.utils.machine_id.get_machine_id", return_value=FOREIGN_MACHINE_ID):
        foreign = session_manager.register(
            external_id="owned-by-foreign-machine",
            machine_id=None,
            source="codex",
            project_id=None,
        )

    with patch("gobby.utils.machine_id.get_machine_id", return_value=LOCAL_MACHINE_ID):
        with pytest.raises(MachineOwnershipMismatchError) as exc_info:
            session_manager.register(
                external_id=foreign.external_id,
                machine_id=None,
                source=foreign.source,
                project_id=None,
            )

    assert exc_info.value.resource_id == foreign.id
    assert session_manager.get(foreign.id) == foreign


def test_registration_blocks_foreign_owner_across_projects(
    session_manager: SessionManager,
    sample_project: dict,
) -> None:
    """The ownership scan is project-agnostic: register may recover a
    same-identity session across projects, so a foreign owner in another
    project must block before that reuse path can run."""
    other_project_id = (
        LocalProjectManager(session_manager.db)
        .create(
            name="foreign-owner-project",
            repo_path="/tmp/foreign-owner-project",
        )
        .id
    )

    with patch("gobby.utils.machine_id.get_machine_id", return_value=FOREIGN_MACHINE_ID):
        foreign = session_manager.register(
            external_id="cross-project-session",
            machine_id=None,
            source="codex",
            project_id=other_project_id,
        )

    with patch("gobby.utils.machine_id.get_machine_id", return_value=LOCAL_MACHINE_ID):
        with pytest.raises(MachineOwnershipMismatchError) as exc_info:
            session_manager.register(
                external_id="cross-project-session",
                machine_id=None,
                source="codex",
                project_id=sample_project["id"],
            )

    assert exc_info.value.resource_id == foreign.id
    assert exc_info.value.owner_machine_id == FOREIGN_MACHINE_ID
    assert session_manager.get(foreign.id) == foreign
    assert foreign.project_id == other_project_id


pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Session title", MANUAL_TITLE_SOURCE),
        ("  Session title  ", MANUAL_TITLE_SOURCE),
        ("", None),
        ("   ", None),
        (None, None),
        (42, None),
    ],
)
def test_manual_title_source_only_marks_non_blank_strings(
    title: object,
    expected: str | None,
) -> None:
    assert manual_title_source(title) == expected


def test_session_registration_boolean_case_is_postgres_safe() -> None:
    source = inspect.getsource(session_crud)
    upsert_source = inspect.getsource(session_upsert)
    web_chat_source = inspect.getsource(session_web_chat_crud)

    assert "CASE WHEN ? THEN 1 ELSE is_local END" not in source
    assert "CASE WHEN ? THEN TRUE ELSE is_local END" not in source
    assert "is_local = %s" in web_chat_source
    assert "WHEN ? = -1 THEN is_local" not in upsert_source
    assert "WHEN ? THEN TRUE" not in upsert_source
    assert "WHEN %s THEN %s" in upsert_source
    assert "%s, 0, 0, 0, 0, NULL" not in source
    assert "%s, FALSE, 0, 0, 0, NULL" in source


def test_session_had_edits_updates_use_boolean_literals() -> None:
    source = inspect.getsource(session_metadata_update)

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
        machine_id="20000000-0000-4000-8000-000000000001",
        source="codex",
        project_id=sample_project["id"],
        is_local=True,
    )

    with session_manager.db.transaction() as conn:
        cleared = session_upsert.update_existing_session(
            session_manager,
            conn,
            session,
            machine_id=session.machine_id,
            title=None,
            title_source=None,
            transcript_path=None,
            git_branch=None,
            parent_session_id=None,
            terminal_context_json=None,
            workflow_name=None,
            is_local=False,
            sandbox_enabled=None,
            sandbox_policy_hash=None,
            now=datetime.fromisoformat("2026-05-22T00:00:00+00:00"),
        )

    assert cleared.is_local is False

    with session_manager.db.transaction() as conn:
        preserved = session_upsert.update_existing_session(
            session_manager,
            conn,
            cleared,
            machine_id=cleared.machine_id,
            title=None,
            title_source=None,
            transcript_path=None,
            git_branch=None,
            parent_session_id=None,
            terminal_context_json=None,
            workflow_name=None,
            is_local=None,
            sandbox_enabled=None,
            sandbox_policy_hash=None,
            now=datetime.fromisoformat("2026-05-22T00:00:01+00:00"),
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
        machine_id=LOCAL_MACHINE_ID,
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


def _session_update_bound_values(conn: _CaptureConnection) -> dict[str, object]:
    sql, params = conn.calls[0]
    names = [
        "machine_id",
        "transcript_path_set",
        "transcript_path",
        "git_branch_set",
        "git_branch",
        "parent_session_id_set",
        "parent_session_id",
        "terminal_context_guard",
        "terminal_context",
        "workflow_name",
        "is_local_provided",
        "is_local",
        "sandbox_enabled",
        "sandbox_policy_hash",
        "updated_at",
        "last_activity",
        "id",
    ]
    bound = dict(zip(names, params, strict=True))
    assert "%s" in sql
    return bound


def test_update_existing_session_binds_is_local_as_booleans_for_postgres() -> None:
    session = _session_stub()
    conn = _CaptureConnection()

    session_upsert.update_existing_session(
        _StaticSessionGetter(session),
        conn,
        session,
        machine_id=session.machine_id,
        title=UNSET,
        title_source=UNSET,
        transcript_path=None,
        git_branch=None,
        parent_session_id=None,
        terminal_context_json=None,
        workflow_name=None,
        is_local=True,
        sandbox_enabled=True,
        sandbox_policy_hash=None,
        now=datetime.fromisoformat("2026-05-22T00:00:01+00:00"),
    )

    bound = _session_update_bound_values(conn)

    assert bound["is_local_provided"] is True
    assert bound["is_local"] is True
    assert bound["sandbox_enabled"] is True
    assert type(bound["is_local_provided"]) is bool
    assert type(bound["is_local"]) is bool
    assert type(bound["sandbox_enabled"]) is bool


def test_update_existing_session_preserve_is_local_uses_boolean_guard_param() -> None:
    session = _session_stub()
    conn = _CaptureConnection()

    session_upsert.update_existing_session(
        _StaticSessionGetter(session),
        conn,
        session,
        machine_id=session.machine_id,
        title=UNSET,
        title_source=UNSET,
        transcript_path=None,
        git_branch=None,
        parent_session_id=None,
        terminal_context_json=None,
        workflow_name=None,
        is_local=None,
        sandbox_enabled=None,
        sandbox_policy_hash=None,
        now=datetime.fromisoformat("2026-05-22T00:00:01+00:00"),
    )

    bound = _session_update_bound_values(conn)

    assert bound["is_local_provided"] is False
    assert bound["is_local"] is False
    assert bound["sandbox_enabled"] is None
    assert type(bound["is_local_provided"]) is bool
    assert type(bound["is_local"]) is bool


def test_update_existing_session_ignores_invalid_terminal_context_json() -> None:
    session = _session_stub()
    session.terminal_context = {"tmux_pane": "%1"}
    conn = _CaptureConnection()

    session_upsert.update_existing_session(
        _StaticSessionGetter(session),
        conn,
        session,
        machine_id=session.machine_id,
        title=UNSET,
        title_source=UNSET,
        transcript_path=None,
        git_branch=None,
        parent_session_id=None,
        terminal_context_json="{not-json",
        workflow_name=None,
        is_local=None,
        sandbox_enabled=None,
        sandbox_policy_hash=None,
        now=datetime.fromisoformat("2026-05-22T00:00:01+00:00"),
    )

    params = conn.calls[0][1]

    assert params[7:9] == (None, None)


def test_update_existing_session_merges_terminal_context_in_sql() -> None:
    session = _session_stub()
    session.terminal_context = {"tmux_pane": "%1"}
    conn = _CaptureConnection()

    session_upsert.update_existing_session(
        _StaticSessionGetter(session),
        conn,
        session,
        machine_id=session.machine_id,
        title=UNSET,
        title_source=UNSET,
        transcript_path=None,
        git_branch=None,
        parent_session_id=None,
        terminal_context_json='{"cwd": "/work/gobby", "parent_pid": null}',
        workflow_name=None,
        is_local=None,
        sandbox_enabled=None,
        sandbox_policy_hash=None,
        now=datetime.fromisoformat("2026-05-22T00:00:01+00:00"),
    )

    sql, params = conn.calls[0]

    assert "COALESCE(terminal_context, '{}'::jsonb) || %s::jsonb" in sql
    assert json.loads(str(params[7])) == {"cwd": "/work/gobby"}
    assert params[7] == params[8]


class TestSessionManagerRegistration:
    """Tests split from the SessionManager storage monolith."""

    @pytest.mark.parametrize(
        "machine_id",
        ["20000000-0000-4000-8000-000000000001", None],
    )
    def test_register_is_idempotent_for_uuid_and_null_machine(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
        machine_id: str | None,
    ) -> None:
        first = session_manager.register(
            external_id="uuid-null-idempotency",
            machine_id=machine_id,
            source="codex",
            project_id=sample_project["id"],
        )
        second = session_manager.register(
            external_id="uuid-null-idempotency",
            machine_id=machine_id,
            source="codex",
            project_id=sample_project["id"],
        )

        assert second.id == first.id

    @pytest.mark.parametrize(
        ("first_machine_id", "second_machine_id"),
        [
            (None, "20000000-0000-4000-8000-000000000001"),
            ("20000000-0000-4000-8000-000000000001", None),
        ],
    )
    def test_register_unifies_machine_attribution_transitions(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
        first_machine_id: str | None,
        second_machine_id: str | None,
    ) -> None:
        first = session_manager.register(
            external_id="machine-attribution-transition",
            machine_id=first_machine_id,
            source="codex",
            project_id=sample_project["id"],
        )
        second = session_manager.register(
            external_id="machine-attribution-transition",
            machine_id=second_machine_id,
            source="codex",
            project_id=sample_project["id"],
        )

        row = session_manager.db.fetchone(
            """
            SELECT count(*) AS session_count, max(machine_id::text) AS machine_id
            FROM sessions
            WHERE external_id = %s AND source = %s AND project_id = %s
              AND session_type = 'terminal'
            """,
            ("machine-attribution-transition", "codex", sample_project["id"]),
        )
        assert second.id == first.id
        assert row["session_count"] == 1
        assert row["machine_id"] == "20000000-0000-4000-8000-000000000001"

    def test_concurrent_mixed_machine_registrations_create_one_row(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        machine_id = "20000000-0000-4000-8000-000000000001"

        def register(observed_machine_id: str | None) -> Session:
            return session_manager.register(
                external_id="concurrent-machine-attribution",
                machine_id=observed_machine_id,
                source="codex",
                project_id=sample_project["id"],
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            sessions = list(executor.map(register, (None, machine_id)))

        row = session_manager.db.fetchone(
            """
            SELECT count(*) AS session_count
            FROM sessions
            WHERE external_id = %s AND source = %s AND project_id = %s
              AND session_type = 'terminal'
            """,
            ("concurrent-machine-attribution", "codex", sample_project["id"]),
        )
        assert {session.id for session in sessions} == {sessions[0].id}
        assert row["session_count"] == 1

    def test_register_session(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test registering a new session."""
        session = session_manager.register(
            external_id="session-123",
            machine_id=LOCAL_MACHINE_ID,
            source="claude",
            project_id=sample_project["id"],
            title="My Session",
            transcript_path="/path/to/transcript.jsonl",
            git_branch="main",
        )

        assert session.id is not None
        assert session.external_id == "session-123"
        assert session.machine_id == LOCAL_MACHINE_ID
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

    @pytest.mark.parametrize(
        ("source", "provider_label"),
        [
            ("claude", "Claude"),
            ("claude_code", "Claude Code"),
            ("codex", "Codex"),
            ("grok", "Grok"),
            ("qwen", "Qwen"),
            ("droid", "Droid"),
            ("agy", "AGY"),
            ("pipeline", "Pipeline"),
            ("Custom Source!", "Custom Source!"),
        ],
    )
    def test_register_without_title_uses_provisional_title(
        self,
        session_manager: SessionManager,
        sample_project: dict,
        source: str,
        provider_label: str,
    ) -> None:
        session = session_manager.register(
            external_id=f"provisional-{source}",
            machine_id="20000000-0000-4000-8000-000000000001",
            source=source,
            project_id=sample_project["id"],
        )

        assert session.title == f"(test-project-S#{session.seq_num}): {provider_label}"
        assert session.title_source == PROVISIONAL_TITLE_SOURCE

    def test_register_with_explicit_title_does_not_mark_provisional(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        session = session_manager.register(
            external_id="explicit-title",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=sample_project["id"],
            title="Caller Supplied Title",
        )

        assert session.title == "Caller Supplied Title"
        assert session.title_source == "manual"

    def test_register_rejects_removed_native_title_source(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        with pytest.raises(ValueError, match="Invalid title_source"):
            session_manager.register(
                external_id="explicit-native-title",
                machine_id="20000000-0000-4000-8000-000000000001",
                source="codex",
                project_id=sample_project["id"],
                title="Caller Supplied Title",
                title_source="native",
            )

    def test_register_rejects_invalid_title_source(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        with pytest.raises(ValueError, match="Invalid title_source"):
            session_manager.register(
                external_id="invalid-title-source",
                machine_id="20000000-0000-4000-8000-000000000001",
                source="codex",
                project_id=sample_project["id"],
                title="Caller Supplied Title",
                title_source="provider",
            )

    def test_register_rejects_removed_llm_title_source(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        with pytest.raises(ValueError, match="Invalid title_source"):
            session_manager.register(
                external_id="provisional-ignores-caller-source",
                machine_id="20000000-0000-4000-8000-000000000001",
                source="codex",
                project_id=sample_project["id"],
                title_source="llm",
            )

    def test_register_existing_blank_title_backfills_provisional_title(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        session = session_manager.register(
            external_id="blank-title",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=sample_project["id"],
            title="",
        )

        updated = session_manager.register(
            external_id="blank-title",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=sample_project["id"],
        )

        assert updated.id == session.id
        assert updated.title == f"(test-project-S#{updated.seq_num}): Codex"
        assert updated.title_source == PROVISIONAL_TITLE_SOURCE

    def test_stale_registration_backfill_preserves_concurrent_task_title(
        self,
        session_manager: SessionManager,
        sample_project: dict,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session = session_manager.register(
            external_id="stale-registration-title",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        stale_session = replace(session, title=None, title_source=None)
        task_owned = session_manager.update_title(
            session.id,
            "(gobby): Task #42 - Claimed work",
            title_source="task",
        )
        assert task_owned is not None

        monkeypatch.setattr(
            session_manager,
            "find_by_external_id",
            lambda *_args, **_kwargs: stale_session,
        )

        updated = session_manager.register(
            external_id="stale-registration-title",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )

        assert updated.title == "(gobby): Task #42 - Claimed work"
        assert updated.title_source == "task"

    def test_create_web_chat_without_title_uses_provisional_title(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        session = session_manager.create_web_chat_session(
            machine_id="20000000-0000-4000-8000-000000000001",
            project_id=sample_project["id"],
            source="droid",
            model="gemini-3.5-flash",
            sandbox_enabled=True,
            sandbox_policy_hash="policy-hash-123",
        )

        assert session.session_type == "web_chat"
        assert session.title == f"(test-project-S#{session.seq_num}): Droid"
        assert session.title_source == PROVISIONAL_TITLE_SOURCE

    def test_create_web_chat_with_user_title_marks_it_manual(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        session = session_manager.create_web_chat_session(
            machine_id="20000000-0000-4000-8000-000000000001",
            project_id=sample_project["id"],
            source="codex",
            title="My Investigation",
            sandbox_enabled=True,
            sandbox_policy_hash="policy-hash-123",
        )

        assert session.title == "My Investigation"
        assert session.title_source == "manual"

    def test_register_recreates_missing_system_parent_session(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Register self-heals the system parent row before inserting children."""
        session_manager.db.execute("DELETE FROM sessions WHERE id = %s", (system_session_id(),))
        assert (
            session_manager.db.fetchone(
                "SELECT id FROM sessions WHERE id = %s", (system_session_id(),)
            )
            is None
        )

        session = session_manager.register(
            external_id="pipeline-child",
            machine_id=LOCAL_MACHINE_ID,
            source="pipeline",
            project_id=sample_project["id"],
            parent_session_id=system_session_id(),
        )

        repaired = session_manager.db.fetchone(
            "SELECT id, external_id, source FROM sessions WHERE id = %s",
            (system_session_id(),),
        )
        assert repaired is not None
        assert repaired["external_id"] == system_session_external_id()
        assert repaired["source"] == "system"
        assert session.parent_session_id == system_session_id()

    def test_register_session_has_stats_columns(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test that a newly registered session has the stats columns."""
        session = session_manager.register(
            external_id="stats-check",
            machine_id="20000000-0000-4000-8000-000000000001",
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
        assert row is not None
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
            machine_id="20000000-0000-4000-8000-000000000001",
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
            machine_id="20000000-0000-4000-8000-000000000001",
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
            machine_id=LOCAL_MACHINE_ID,
            source="claude",
            project_id=sample_project["id"],
            title="Original",
        )

        # Second registration with same key combo
        session2 = session_manager.register(
            external_id="unique-key",
            machine_id=LOCAL_MACHINE_ID,
            source="claude",
            project_id=sample_project["id"],
            title="Updated",
        )

        # Should be the same session with updated title
        assert session2.id == session1.id
        assert session2.title == "Updated"

    def test_register_preserves_expired_terminal_session_and_transcript_state(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        session = session_manager.register(
            external_id="expired-registration",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
            transcript_path="/tmp/expired-registration.jsonl",
        )
        session_manager.update_status(session.id, "expired")
        session_manager.mark_transcript_processed(session.id)

        registered = session_manager.register(
            external_id="expired-registration",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
        )

        assert registered.id == session.id
        assert registered.status == "expired"
        row = session_manager.db.fetchone(
            "SELECT transcript_processed FROM sessions WHERE id = %s",
            (session.id,),
        )
        assert row is not None
        assert row["transcript_processed"] is True

    def test_expired_cache_mapping_cannot_route_to_detached_duplicate(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        original = session_manager.register(
            external_id="stale-registration-cache",
            machine_id=None,
            source="codex",
            project_id=sample_project["id"],
        )
        session_manager.cache_session_mapping(
            external_id=original.external_id,
            source=original.source,
            session_id=original.id,
            project_id=original.project_id,
            session_type=original.session_type,
        )
        session_manager.update_status(original.id, "expired")

        assert (
            session_manager.lookup_session_id(
                original.external_id,
                original.source,
                project_id=original.project_id,
                session_type=original.session_type,
            )
            is None
        )

        session_manager.db.execute(
            "UPDATE sessions SET external_id = %s WHERE id = %s",
            ("detached-stale-registration-cache", original.id),
        )
        revived = session_manager.register(
            external_id="stale-registration-cache",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=sample_project["id"],
        )

        assert revived.id != original.id
        assert (
            session_manager.lookup_session_id(
                "stale-registration-cache",
                "codex",
                project_id=sample_project["id"],
            )
            == revived.id
        )
        assert (
            session_manager.get_session_id(
                "stale-registration-cache",
                "codex",
                project_id=sample_project["id"],
            )
            == revived.id
        )

    def test_register_does_not_revive_deleted_session(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        session = session_manager.register(
            external_id="deleted-registration",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
        )
        session_manager.update_status(session.id, "deleted")

        registered = session_manager.register(
            external_id="deleted-registration",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
        )

        assert registered.id == session.id
        assert registered.status == "deleted"

    def test_register_existing_session_ignores_self_parent(
        self,
        caplog: pytest.LogCaptureFixture,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Existing session re-registration must not persist itself as parent."""
        session = session_manager.register(
            external_id="self-parent-update",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
        )

        caplog.set_level(logging.WARNING)
        updated = session_manager.register(
            external_id="self-parent-update",
            machine_id=LOCAL_MACHINE_ID,
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
        assert row is not None
        assert row["parent_session_id"] is None
        assert not any("session cannot be its own parent" in message for message in caplog.messages)

    def test_register_repairs_existing_self_parent_row(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Legacy corrupt self-parent rows are repaired during registration."""
        session = session_manager.register(
            external_id="corrupt-self-parent",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
        )
        session_manager.db.execute(
            "ALTER TABLE sessions DROP CONSTRAINT IF EXISTS sessions_parent_session_not_self"
        )
        try:
            session_manager.db.execute(
                "UPDATE sessions SET parent_session_id = id WHERE id = %s",
                (session.id,),
            )

            repaired = session_manager.register(
                external_id="corrupt-self-parent",
                machine_id=LOCAL_MACHINE_ID,
                source="codex",
                project_id=sample_project["id"],
            )
        finally:
            session_manager.db.execute(
                """
                ALTER TABLE sessions
                    ADD CONSTRAINT sessions_parent_session_not_self
                    CHECK (parent_session_id IS NULL OR parent_session_id <> id)
                """
            )

        assert repaired.id == session.id
        assert repaired.parent_session_id is None
        row = session_manager.db.fetchone(
            "SELECT parent_session_id FROM sessions WHERE id = %s",
            (session.id,),
        )
        assert row is not None
        assert row["parent_session_id"] is None

    def test_register_existing_session_persists_valid_parent_update(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Valid parent updates still persist on existing sessions."""
        child = session_manager.register(
            external_id="valid-parent-child",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
        )
        parent = session_manager.register(
            external_id="valid-parent",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
        )

        updated = session_manager.register(
            external_id="valid-parent-child",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
            parent_session_id=parent.id,
        )

        assert updated.id == child.id
        assert updated.parent_session_id == parent.id

    def test_register_existing_session_clears_parent_rejected_as_cycle(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """A rejected cyclic parent must clear stale parent attribution."""
        ancestor = session_manager.register(
            external_id="cycle-ancestor",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
        )
        root = session_manager.register(
            external_id="cycle-root",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
            parent_session_id=ancestor.id,
        )
        child = session_manager.register(
            external_id="cycle-child",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
            parent_session_id=root.id,
        )

        updated = session_manager.register(
            external_id="cycle-root",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
            parent_session_id=child.id,
        )

        assert updated.id == root.id
        assert updated.parent_session_id is None

    def test_register_isolates_session_types(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        web_chat = session_manager.register(
            external_id="runtime-key",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
            title="Web chat",
            session_type="web_chat",
        )
        terminal = session_manager.register(
            external_id="runtime-key",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
            title="Terminal",
            session_type="terminal",
        )

        assert terminal.id != web_chat.id
        assert terminal.session_type == "terminal"
        assert web_chat.session_type == "web_chat"

    def test_register_cross_project_recovery_allocates_destination_seq_num(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        destination = LocalProjectManager(session_manager.db).create(
            name="registration-recovery-destination",
            repo_path="/tmp/registration-recovery-destination",
        )
        original = session_manager.register(
            external_id="cross-project-recovery",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=sample_project["id"],
        )
        for index in range(2):
            session_manager.register(
                external_id=f"destination-session-{index}",
                machine_id=LOCAL_MACHINE_ID,
                source="codex",
                project_id=destination.id,
            )

        recovered = session_manager.register(
            external_id="cross-project-recovery",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=destination.id,
        )

        assert recovered.id == original.id
        assert recovered.project_id == destination.id
        assert recovered.seq_num == 3

    def test_get_session(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test getting a session by ID."""
        created = session_manager.register(
            external_id="get-test",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="codex",
            project_id=sample_project["id"],
        )

        retrieved = session_manager.get(created.id)
        assert retrieved is not None
        assert retrieved.id == created.id
        assert retrieved.external_id == "get-test"

    def test_get_nonexistent(self, session_manager: SessionManager) -> None:
        """Test getting nonexistent session returns None."""
        result = session_manager.get(str(uuid.uuid4()))
        assert result is None

    def test_find_by_external_id(
        self,
        session_manager: SessionManager,
        sample_project: dict,
    ) -> None:
        """Test finding session by canonical provider identity."""
        session = session_manager.register(
            external_id="findable",
            machine_id=LOCAL_MACHINE_ID,
            source="claude",
            project_id=sample_project["id"],
        )

        found = session_manager.find_by_external_id(
            external_id="findable",
            project_id=sample_project["id"],
            source="claude",
        )

        assert found is not None
        assert found.id == session.id

    def test_find_by_external_id_not_found(self, session_manager: SessionManager) -> None:
        """Test find_by_external_id returns None when not found."""
        result = session_manager.find_by_external_id(
            external_id="nonexistent",
            project_id=str(uuid.uuid4()),
            source="claude",
        )
        assert result is None

    @pytest.mark.unit
    def test_create_web_chat_session_sets_model_and_chat_mode(
        self,
        session_manager: SessionManager,
        sample_project: dict[str, Any],
    ) -> None:
        session = session_manager.create_web_chat_session(
            machine_id="20000000-0000-4000-8000-000000000001",
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
        sample_project: dict[str, str],
    ) -> None:
        """Test registering session with agent depth and spawned_by_agent_id."""
        session = session_manager.register(
            external_id="agent-session",
            machine_id="20000000-0000-4000-8000-000000000001",
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
        sample_project: dict[str, str],
    ) -> None:
        """Test that register updates metadata when session exists."""
        # Create a parent session first for the foreign key
        parent = session_manager.register(
            external_id="parent-meta",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )

        # First registration without transcript_path or git_branch
        session1 = session_manager.register(
            external_id="update-meta",
            machine_id="20000000-0000-4000-8000-000000000001",
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
            machine_id="20000000-0000-4000-8000-000000000001",
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

        preserved = session_manager.register(
            external_id="update-meta",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
        )
        assert preserved.title == "Updated Title"
        assert preserved.transcript_path == "/new/path.jsonl"
        assert preserved.git_branch == "feature/new"
        assert preserved.parent_session_id == parent.id

        cleared = session_manager.register(
            external_id="update-meta",
            machine_id="20000000-0000-4000-8000-000000000001",
            source="claude",
            project_id=sample_project["id"],
            title=None,
            transcript_path=None,
            git_branch=None,
            parent_session_id=None,
        )
        assert cleared.title == "Updated Title"
        assert cleared.title_source == "manual"
        assert cleared.transcript_path is None
        assert cleared.git_branch is None
        assert cleared.parent_session_id is None
