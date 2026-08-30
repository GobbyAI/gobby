"""Tests for hooks/event_handlers/_session.py — targeting uncovered lines."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from gobby.hooks.event_handlers._session import SessionEventHandlerMixin
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.storage.sessions._update_sentinel import UNSET
from gobby.tasks.state_semantics import ACTIVE_STAGE_STATES

from ._event_handler_helpers import empty_database_mock

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    event_type: HookEventType = HookEventType.SESSION_START,
    session_id: str = "ext-123",
    source: SessionSource = SessionSource.CLAUDE,
    data: dict | None = None,
    metadata: dict | None = None,
    task_id: str | None = None,
) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        session_id=session_id,
        source=source,
        timestamp=datetime.now(),
        data=data or {},
        metadata=metadata or {},
        task_id=task_id,
    )


def _make_session(
    *,
    session_id: str = "sess-uuid-1",
    status: str = "active",
    summary_markdown: str | None = None,
    parent_session_id: str | None = None,
    seq_num: int | None = 10,
    project_id: str | None = "proj-1",
    agent_run_id: str | None = None,
    agent_depth: int = 0,
    workflow_name: str | None = None,
    created_at: str = "2024-01-01T00:00:00Z",
) -> MagicMock:
    session = MagicMock()
    session.id = session_id
    session.status = status
    session.summary_markdown = summary_markdown
    session.parent_session_id = parent_session_id
    session.seq_num = seq_num
    session.project_id = project_id
    session.agent_run_id = agent_run_id
    session.agent_depth = agent_depth
    session.workflow_name = workflow_name
    session.created_at = created_at
    return session


@pytest.mark.parametrize(
    "binding",
    ["compact", "resume", "clear", "web_chat", "pre_created"],
)
def test_session_start_binding_matrix(binding: str) -> None:
    from gobby.hooks.event_handlers._session_start.handoff import SessionStartResolution

    handler = _TestHandler()
    external_id = f"binding-{binding}"
    source = binding if binding in {"compact", "resume", "clear"} else "startup"
    event = _make_event(
        event_type=HookEventType.SESSION_START,
        session_id=external_id,
        data={"source": source, "cwd": "/tmp"},
    )
    row = SimpleNamespace(
        id=f"canonical-{binding}",
        external_id=external_id,
        status="active",
        source="claude",
        project_id="proj-1",
        machine_id="21000000-0000-4000-8000-000000000001",
        transcript_path="/tmp/transcript.jsonl",
        terminal_context={"cwd": "/tmp"},
        parent_session_id=None,
        session_type="web_chat" if binding == "web_chat" else "terminal",
        seq_num=42,
    )
    handler._session_manager.get.return_value = row if binding == "pre_created" else None
    handler._session_manager.find_by_external_id.return_value = (
        row if binding == "web_chat" else None
    )
    handler._session_manager.find_by_external_id_any_project.return_value = None
    handler._session_manager.register_session.return_value = row.id

    def bind_pre_created(**kwargs: Any) -> HookResponse:
        kwargs["event"].metadata["_platform_session_id"] = row.id
        return HookResponse(decision="allow")

    def activate(
        activation_event: HookEvent,
        session_id: str,
        **_kwargs: Any,
    ) -> list[str]:
        activation_event.metadata["_platform_session_id"] = session_id
        return []

    resolved = SessionStartResolution(
        session=row if binding in {"compact", "resume"} else None,
        session_source=source,
    )
    reconcile_result = SimpleNamespace(success=True)

    with (
        patch.object(handler, "_handle_pre_created_session", side_effect=bind_pre_created),
        patch.object(handler, "_activate_materialized_session", side_effect=activate),
        patch.object(
            handler,
            "_compose_session_response",
            return_value=HookResponse(decision="allow"),
        ),
        patch.object(handler, "_derive_transcript_path", return_value=row.transcript_path),
        patch(
            "gobby.hooks.event_handlers._session_start.flow.resolve_session_start_identity",
            return_value=resolved,
        ),
        patch(
            "gobby.hooks.event_handlers._session_start.flow.rebind_resumed_session_start",
            return_value=(row, row.transcript_path),
        ),
        patch(
            "gobby.hooks.event_handlers._session_start.flow.reconcile_compact_session_activity",
            return_value=reconcile_result,
        ),
    ):
        response = handler.handle_session_start(event)

    assert response.decision == "allow"
    assert event.metadata["_platform_session_id"] == row.id
    if binding == "clear":
        handler._session_manager.register_session.assert_called_once()
    else:
        handler._session_manager.register_session.assert_not_called()


class _TestHandler(SessionEventHandlerMixin):
    """Concrete implementation with required attributes for testing."""

    def __init__(self) -> None:
        self.logger = MagicMock()
        self._session_manager = MagicMock()
        self._session_manager.db = empty_database_mock()
        self._session_manager.update.return_value = None
        self._session_coordinator = MagicMock()
        self._session_end_auto_link_worker = None
        message_processor = MagicMock()
        self._message_processor_resolver = lambda: message_processor
        self._task_manager = MagicMock()
        self._workflow_handler = MagicMock()
        self._workflow_config = None
        self._workflow_config_resolver = lambda: None
        self._message_manager = None
        self._skill_manager = None
        self._skills_config = None
        self._session_task_manager = None
        self._session_message_processors: dict[str, Any] = {}
        self._dispatch_session_summaries_fn = None
        self._get_machine_id = MagicMock(return_value="21000000-0000-4000-8000-000000000001")
        self._resolve_project_id = MagicMock(return_value="proj-1")
        self._handler_map = {}


# ---------------------------------------------------------------------------
# _derive_transcript_path tests
# ---------------------------------------------------------------------------


class TestDeriveTranscriptPath:
    """Tests for _derive_transcript_path."""

    def test_qwen_source(self, tmp_path: Path) -> None:
        handler = _TestHandler()
        transcript = tmp_path / "q.json"
        transcript.write_text("{}\n", encoding="utf-8")
        result = handler._derive_transcript_path(
            "qwen",
            {"transcript_path": str(transcript)},
            "ext-1",
            owner_machine_id="local-machine",
            local_machine_id="local-machine",
        )
        assert result == str(transcript)

    def test_unknown_source(self) -> None:
        handler = _TestHandler()
        result = handler._derive_transcript_path(
            "codex",
            {},
            "ext-1",
            owner_machine_id="local-machine",
            local_machine_id="local-machine",
        )
        assert result is None


# ---------------------------------------------------------------------------
# _find_qwen_transcript tests
# ---------------------------------------------------------------------------


class TestFindQwenTranscript:
    """Tests for _find_qwen_transcript."""

    def test_no_cwd(self) -> None:
        handler = _TestHandler()
        result = handler._find_qwen_transcript({}, "ext-1")
        assert result is None

    def test_chats_dir_not_exists(self, tmp_path: Path) -> None:
        handler = _TestHandler()
        result = handler._find_qwen_transcript({"cwd": str(tmp_path)}, "ext-1")
        assert result is None

    def test_match_by_prefix(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import hashlib

        handler = _TestHandler()
        cwd = str(tmp_path / "project")
        project_hash = hashlib.sha256(cwd.encode()).hexdigest()
        chats_dir = tmp_path / ".qwen" / "tmp" / project_hash / "chats"
        chats_dir.mkdir(parents=True)
        (chats_dir / "session-2024-01-01T10-00-abcdefgh.json").touch()

        import gobby.hooks.event_handlers._session_start as session_mod

        monkeypatch.setattr(session_mod.Path, "home", staticmethod(lambda: tmp_path))

        result = handler._find_qwen_transcript({"cwd": cwd}, "abcdefgh-1234")
        assert result is not None
        assert "abcdefgh" in result

    def test_missing_session_id_does_not_fallback_to_most_recent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A missing session ID must not bind an unrelated transcript."""
        import hashlib

        handler = _TestHandler()
        cwd = str(tmp_path / "project")
        project_hash = hashlib.sha256(cwd.encode()).hexdigest()
        chats_dir = tmp_path / ".qwen" / "tmp" / project_hash / "chats"
        chats_dir.mkdir(parents=True)
        (chats_dir / "session-2024-01-02T10-00-old.json").touch()
        (chats_dir / "session-2024-01-01T10-00-recent.json").touch()

        import gobby.hooks.event_handlers._session_start as session_mod

        monkeypatch.setattr(session_mod.Path, "home", staticmethod(lambda: tmp_path))

        result = handler._find_qwen_transcript({"cwd": cwd}, "")
        assert result is None


# ---------------------------------------------------------------------------
# _find_cursor_transcript tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# handle_session_end tests
# ---------------------------------------------------------------------------


class TestHandleSessionEnd:
    """Tests for handle_session_end."""

    def test_handle_session_end_basic(self) -> None:
        handler = _TestHandler()
        event = _make_event(event_type=HookEventType.SESSION_END, data={"cwd": "/tmp"})
        event.metadata["_platform_session_id"] = "sess-1"
        processor = MagicMock()
        handler._session_message_processors["sess-1"] = processor

        # Test basic execution
        resp = handler.handle_session_end(event)
        assert resp.decision == "allow"

        # Should call auto_link_commits and complete_agent_run
        handler._task_manager = MagicMock()
        handler._session_coordinator.complete_agent_run.assert_called_once()
        processor.unregister_session.assert_called_with("sess-1")
        assert "sess-1" not in handler._session_message_processors
        handler._session_manager.update_status_if_non_terminal.assert_called_with(
            "sess-1", "expired"
        )

    def test_handle_session_end_clear_expires(self) -> None:
        handler = _TestHandler()
        event = _make_event(event_type=HookEventType.SESSION_END, data={"reason": "clear"})
        event.metadata["_platform_session_id"] = "sess-1"

        mock_session = MagicMock()
        mock_session.status = "expired"
        handler._session_manager.get.return_value = mock_session

        resp = handler.handle_session_end(event)
        assert resp.decision == "allow"
        handler._session_manager.update_status_if_non_terminal.assert_called_with(
            "sess-1", "expired"
        )

    def test_handle_session_end_missing_platform_session_id(self) -> None:
        handler = _TestHandler()
        event = _make_event(event_type=HookEventType.SESSION_END, session_id="ext-1")

        handler._session_manager.lookup_session_id.return_value = "db-sess-1"
        processor = MagicMock()
        handler._session_message_processors["db-sess-1"] = processor
        handler.handle_session_end(event)

        assert event.metadata.get("_platform_session_id") == "db-sess-1"
        processor.unregister_session.assert_called_with("db-sess-1")
        assert "db-sess-1" not in handler._session_message_processors

    def test_handle_session_end_exceptions(self) -> None:
        handler = _TestHandler()
        event = _make_event(event_type=HookEventType.SESSION_END)
        event.metadata["_platform_session_id"] = "sess-1"

        # Setting up exceptions
        handler._session_coordinator.complete_agent_run.side_effect = Exception("test run")
        handler._message_processor_resolver().unregister_session.side_effect = Exception(
            "test proc"
        )
        handler._session_manager.update_status_if_non_terminal.side_effect = Exception(
            "test storage"
        )

        with patch("gobby.tasks.commits.auto_link_commits", side_effect=Exception("test link")):
            resp = handler.handle_session_end(event)
            assert resp.decision == "allow"  # Exceptions are swallowed

    def test_handle_session_end_releases_interactive_lock_labels(self) -> None:
        """When a session ends, remove its interactive-plan lock labels across
        all tasks. Skill's own terminal cleanup handles the common case; this
        sweep is the safety net for sessions that die before reaching cleanup.
        """
        handler = _TestHandler()
        event = _make_event(event_type=HookEventType.SESSION_END, data={"cwd": "/tmp"})
        event.metadata["_platform_session_id"] = "sess-1"

        t1 = MagicMock()
        t1.id = "uuid-task-1"
        t2 = MagicMock()
        t2.id = "uuid-task-2"
        handler._task_manager.list_tasks.return_value = [t1, t2]

        handler.handle_session_end(event)

        # Every matching task has the session-specific label removed
        expected_label = "interactive:planning-in-progress:sess-1"
        handler._task_manager.list_tasks.assert_any_call(label=expected_label, limit=200)
        handler._task_manager.remove_label.assert_any_call("uuid-task-1", expected_label)
        handler._task_manager.remove_label.assert_any_call("uuid-task-2", expected_label)
        assert handler._task_manager.remove_label.call_count == 2

    def test_handle_session_end_lock_sweep_failure_does_not_fail_session_end(self) -> None:
        """If list_tasks / remove_label raises, session-end still returns allow."""
        handler = _TestHandler()
        event = _make_event(event_type=HookEventType.SESSION_END)
        event.metadata["_platform_session_id"] = "sess-1"

        handler._task_manager.list_tasks.side_effect = Exception("db down")

        resp = handler.handle_session_end(event)
        assert resp.decision == "allow"

    def test_handle_session_end_sweep_is_scoped_to_current_session(self) -> None:
        """The sweep must only remove THIS session's lock label, never another
        session's. We verify this at the list_tasks boundary: the handler
        queries with label=interactive:planning-in-progress:<self>, so tasks
        carrying another session's lock are never returned and never touched.
        """
        handler = _TestHandler()
        event = _make_event(event_type=HookEventType.SESSION_END)
        event.metadata["_platform_session_id"] = "sess-1"

        # Simulate a task that belongs to a *different* session and should
        # not surface under list_tasks(label=...for sess-1...).
        def fake_list_tasks(label: str, limit: int = 200) -> list:
            # Only sess-1's label returns anything
            if label == "interactive:planning-in-progress:sess-1":
                return []
            return [MagicMock(id="other-session-task")]

        handler._task_manager.list_tasks.side_effect = fake_list_tasks

        handler.handle_session_end(event)

        # Only sess-1's label was queried; never another session's
        called_labels = [
            call.kwargs.get("label") for call in handler._task_manager.list_tasks.call_args_list
        ]
        assert "interactive:planning-in-progress:sess-1" in called_labels
        assert not any(
            lbl
            and lbl.startswith("interactive:planning-in-progress:")
            and lbl != "interactive:planning-in-progress:sess-1"
            for lbl in called_labels
        )
        # Nothing to remove, so remove_label is never called
        handler._task_manager.remove_label.assert_not_called()

    def test_handle_session_end_per_task_remove_failure_is_isolated(self) -> None:
        """A failure removing a lock on one task must not prevent removing it
        from the next task in the sweep."""
        handler = _TestHandler()
        event = _make_event(event_type=HookEventType.SESSION_END)
        event.metadata["_platform_session_id"] = "sess-1"

        t1 = MagicMock()
        t1.id = "uuid-a"
        t2 = MagicMock()
        t2.id = "uuid-b"
        handler._task_manager.list_tasks.return_value = [t1, t2]

        # First remove fails, second succeeds
        handler._task_manager.remove_label.side_effect = [Exception("lost row"), None]

        resp = handler.handle_session_end(event)
        assert resp.decision == "allow"
        # Both tasks were attempted
        assert handler._task_manager.remove_label.call_count == 2


class TestSessionStartAndHelpers:
    """Tests for handle_session_start and its internal helpers."""

    def test_handle_session_start_basic(self) -> None:
        handler = _TestHandler()
        event = _make_event(
            event_type=HookEventType.SESSION_START, session_id="ext-1", data={"cwd": "/tmp"}
        )
        handler._session_manager.get.return_value = None
        handler._session_manager.find_by_external_id.return_value = None
        handler._session_manager.find_by_external_id_any_project.return_value = None

        resp = handler.handle_session_start(event)

        handler._session_manager.register_session.assert_not_called()
        handler._session_coordinator.register_session.assert_not_called()
        handler._message_processor_resolver().register_session.assert_not_called()
        assert "_platform_session_id" not in event.metadata
        assert resp.decision == "allow"

    def test_handle_session_start_skips_acp_child(self) -> None:
        """Sessions spawned by daemon-owned qwen --acp must not
        register — the envelope carries gobby_acp_child='1' in terminal_context.
        """
        handler = _TestHandler()
        event = _make_event(
            event_type=HookEventType.SESSION_START,
            session_id="acp-child-external-id",
            source=SessionSource.QWEN,
            data={
                "cwd": "/tmp",
                "terminal_context": {"gobby_acp_child": "1"},
            },
        )

        resp = handler.handle_session_start(event)

        assert resp.decision == "allow"
        handler._session_manager.register_session.assert_not_called()
        handler._session_manager.get.assert_not_called()

    def test_handle_session_start_pre_created(self) -> None:
        handler = _TestHandler()
        event = _make_event(event_type=HookEventType.SESSION_START, session_id="ext-1", data={})

        mock_session = MagicMock()
        mock_session.id = "ext-1"
        handler._session_manager.get.return_value = mock_session

        with patch.object(handler, "_handle_pre_created_session") as mock_pre_created:
            mock_pre_created.return_value = HookResponse(decision="allow")
            resp = handler.handle_session_start(event)
            mock_pre_created.assert_called_once()
            assert resp.decision == "allow"

    def test_materialized_session_sets_code_index_available(self) -> None:
        """When project has indexed symbols, code_index_available is set to True."""
        handler = _TestHandler()
        event = _make_event(
            event_type=HookEventType.SESSION_START, session_id="ext-1", data={"cwd": "/tmp"}
        )
        session = _make_session(session_id="new-sess-1", project_id="proj-1")

        mock_stats = MagicMock()
        mock_stats.total_symbols = 42

        with (
            patch.object(handler, "_activate_default_agent", return_value=None),
            patch("gobby.code_index.storage.CodeIndexStorage") as mock_cis_cls,
            patch("gobby.workflows.state_manager.SessionVariableManager") as mock_sv_cls,
        ):
            mock_cis_cls.return_value.get_project_stats.return_value = mock_stats
            mock_sv_mgr = mock_sv_cls.return_value

            handler._activate_materialized_session(
                event,
                "new-sess-1",
                session_obj=session,
                project_id="proj-1",
                transcript_path="/tmp/t.json",
            )

            mock_cis_cls.return_value.get_project_stats.assert_called_once_with("proj-1")
            assert mock_cis_cls.return_value.get_project_stats.call_count == 1
            assert mock_cis_cls.return_value.get_project_stats.call_args is not None
            mock_sv_mgr.set_variable.assert_any_call("new-sess-1", "code_index_available", True)
            assert mock_sv_mgr.set_variable.call_count >= 1
            assert mock_sv_mgr.set_variable.call_args is not None

    def test_materialized_session_no_index_skips_variable(self) -> None:
        """When project has no indexed symbols, code_index_available is NOT set."""
        handler = _TestHandler()
        event = _make_event(
            event_type=HookEventType.SESSION_START, session_id="ext-1", data={"cwd": "/tmp"}
        )
        session = _make_session(session_id="new-sess-1", project_id="proj-1")

        with (
            patch.object(handler, "_activate_default_agent", return_value=None),
            patch("gobby.code_index.storage.CodeIndexStorage") as mock_cis_cls,
            patch("gobby.workflows.state_manager.SessionVariableManager") as mock_sv_cls,
        ):
            mock_cis_cls.return_value.get_project_stats.return_value = None
            mock_sv_mgr = mock_sv_cls.return_value

            handler._activate_materialized_session(
                event,
                "new-sess-1",
                session_obj=session,
                project_id="proj-1",
                transcript_path="/tmp/t.json",
            )

            mock_cis_cls.return_value.get_project_stats.assert_called_once_with("proj-1")
            assert mock_cis_cls.return_value.get_project_stats.call_count == 1
            assert mock_cis_cls.return_value.get_project_stats.call_args is not None
            # set_variable should NOT be called for code_index_available
            for call in mock_sv_mgr.set_variable.call_args_list:
                assert call[0][1] != "code_index_available"

    def test_handle_pre_created_session_logic(
        self, mock_empty_session_variable_manager: MagicMock
    ) -> None:
        handler = _TestHandler()
        event = _make_event(event_type=HookEventType.SESSION_START)
        mock_session = MagicMock()
        mock_session.id = "sess-1"
        mock_session.project_id = "proj-1"
        mock_session.agent_run_id = "run-1"

        with (
            patch.object(handler, "_activate_default_agent", return_value=None),
            patch.object(
                handler, "_compose_session_response", return_value=HookResponse(decision="allow")
            ),
        ):
            resp = handler._handle_pre_created_session(
                existing_session=mock_session,
                external_id="ext-1",
                transcript_path="/tmp/t.json",
                cli_source="claude",
                event=event,
                cwd="/tmp",
            )

            handler._session_manager.update.assert_called_with(
                session_id="sess-1", transcript_path=UNSET, status="active"
            )
            handler._session_manager.cache_session_mapping.assert_called_once()
            handler._session_coordinator.start_agent_run.assert_called_with("run-1")
            assert resp.decision == "allow"

    def test_resolve_agent_name(self) -> None:
        handler = _TestHandler()

        # Override provided
        assert handler._resolve_agent_name("sess-1", "override-agent") == "override-agent"

        # Session already has agent_type
        with patch("gobby.workflows.state_manager.SessionVariableManager") as mock_sv_mgr:
            mock_sv_mgr.return_value.get_variables.return_value = {"_agent_type": "spawned-agent"}
            assert handler._resolve_agent_name("sess-1", None) == "spawned-agent"

        # Global default
        with (
            patch("gobby.workflows.state_manager.SessionVariableManager") as mock_sv_mgr,
            patch("gobby.storage.config_repository.ConfigRepository") as mock_repo,
        ):
            mock_sv_mgr.return_value.get_variables.return_value = {}
            mock_repo.return_value.read.return_value.values = {"default_agent": "global-agent"}
            assert handler._resolve_agent_name("sess-1", None) == "global-agent"

    def test_build_agent_changes(self) -> None:
        handler = _TestHandler()

        mock_agent_body = MagicMock()
        mock_agent_body.name = "test-agent"
        mock_agent_body.workflows.skill_format = "content"
        mock_agent_body.workflows.variables = {"good_var": "val", "_bad_var": "skip"}
        mock_agent_body.steps = None
        mock_agent_body.step_variables = {}
        mock_agent_body.step_workflow = None

        mock_rule = MagicMock()
        mock_rule.name = "rule1"
        mock_rule.enabled = True

        with (
            patch("gobby.workflows.selectors.resolve_rules_for_agent", return_value={"rule1"}),
            patch("gobby.workflows.selectors.resolve_skills_for_agent", return_value={"skill1"}),
            patch("gobby.workflows.selectors.resolve_variables_for_agent", return_value=None),
        ):
            changes, rules, skills = handler._build_agent_changes(
                agent_body=mock_agent_body,
                session_id="sess-1",
                enabled_rules=[mock_rule],
                all_skills=[],
                enabled_variables=[],
            )

            assert changes["_agent_type"] == "test-agent"
            assert changes["_active_rule_names"] == ["rule1"]
            assert changes["good_var"] == "val"
            assert "_bad_var" not in changes
            assert rules == {"rule1"}
            assert skills == {"skill1"}

    @pytest.mark.parametrize(
        "session_kwargs",
        [{"agent_depth": 1}, {"parent_session_id": "parent-session"}],
        ids=["agent-depth", "parent-session"],
    )
    def test_build_agent_changes_marks_child_as_spawned(
        self,
        session_kwargs: dict[str, Any],
    ) -> None:
        handler = _TestHandler()
        handler._session_manager.get.return_value = _make_session(**session_kwargs)

        mock_agent_body = MagicMock()
        mock_agent_body.name = "test-agent"
        mock_agent_body.workflows.skill_format = "content"
        mock_agent_body.workflows.variables = {}
        mock_agent_body.steps = None
        mock_agent_body.step_variables = {}
        mock_agent_body.step_workflow = None

        with (
            patch("gobby.workflows.selectors.resolve_rules_for_agent", return_value=set()),
            patch("gobby.workflows.selectors.resolve_skills_for_agent", return_value=set()),
            patch("gobby.workflows.selectors.resolve_variables_for_agent", return_value=None),
        ):
            changes, _, _ = handler._build_agent_changes(
                agent_body=mock_agent_body,
                session_id="sess-1",
                enabled_rules=[],
                all_skills=[],
                enabled_variables=[],
            )

        assert changes["is_spawned_agent"] is True
        assert changes["_agent_type"] == "test-agent"


class TestSessionMoreCoverage:
    """Extra tests for hitting the rest of the lines in _session.py."""

    @pytest.mark.parametrize(
        ("listed_skills", "active_skills", "expected_skills"),
        [
            ([], {"skill1"}, ["skill1"]),
            ([SimpleNamespace(name="all-skill")], None, ["all-skill"]),
        ],
    )
    def test_activate_default_agent(
        self,
        listed_skills: list[SimpleNamespace],
        active_skills: set[str] | None,
        expected_skills: list[str],
    ) -> None:
        handler = _TestHandler()

        with (
            patch.object(handler, "_resolve_agent_name", return_value="test-agent"),
            patch("gobby.workflows.agent_resolver.resolve_agent") as mock_resolve,
            patch(
                "gobby.storage.definitions.rules.RuleDefinitionManager.list_all",
                return_value=[],
            ),
            patch(
                "gobby.skills.manager.SkillManager.list_skills",
                return_value=listed_skills,
            ),
            patch.object(handler, "_build_agent_changes") as mock_build,
            patch(
                "gobby.workflows.state_manager.SessionVariableManager.get_variables",
                return_value={},
            ),
            patch("gobby.workflows.state_manager.SessionVariableManager.merge_variables"),
        ):
            mock_agent = MagicMock()
            mock_agent.name = "test-agent"
            mock_agent.description = "Test"
            mock_resolve.return_value = mock_agent

            mock_build.return_value = (
                {"_agent_type": "test-agent", "var1": "val1"},
                {"rule1"},
                active_skills,
            )

            result = handler._activate_default_agent("sess-1", "claude", "proj-1")

            assert result is not None
            assert result.agent_name == "test-agent"
            assert result.rules_count == 1
            assert result.skills_count == len(expected_skills)
            assert result.injected_skill_names == expected_skills
            assert result.variables_count == 1
            # Persona/identity is injected only at first before_agent; the
            # activation result carries metadata, not prompt text.
            assert not hasattr(result, "context")

    def test_handle_session_start_qwen_terminal(self) -> None:
        handler = _TestHandler()
        event = _make_event(
            event_type=HookEventType.SESSION_START,
            session_id="ext-2",
            data={"terminal_context": {"gobby_session_id": "qwen-123"}},
        )

        # Existing session check fails, but gobby_session_id check succeeds
        handler._session_manager.get.side_effect = [None, MagicMock()]

        with patch.object(
            handler, "_handle_pre_created_session", return_value=HookResponse(decision="allow")
        ) as mock_pre:
            handler.handle_session_start(event)
            handler._session_manager.update.assert_called_once()
            assert handler._session_manager.update.call_count == 1
            assert handler._session_manager.update.call_args is not None
            mock_pre.assert_called_once()
            assert mock_pre.call_count == 1
            assert mock_pre.call_args is not None

    def test_handle_session_start_codex_terminal(self) -> None:
        """Codex now joins the late-link path: SessionStart fires with the native
        Codex session_id; the handler resolves the pre-created child via
        terminal_context.gobby_session_id and rewrites external_id.

        This locks in the contract Codex depends on after the preflight removal
        (otherwise Codex sessions would never be linked back to their Gobby
        parent and MCP tool calls scoped by external_id would silently miss).
        """
        handler = _TestHandler()
        codex_native_id = "019dadc3-07e9-7740-97f2-400c3906247e"
        event = _make_event(
            event_type=HookEventType.SESSION_START,
            session_id=codex_native_id,
            source=SessionSource.CODEX,
            data={"terminal_context": {"gobby_session_id": "gobby-codex-1"}},
        )

        # External-id lookup misses, gobby_session_id lookup hits the pre-created child.
        handler._session_manager.get.side_effect = [None, MagicMock()]

        with patch.object(
            handler, "_handle_pre_created_session", return_value=HookResponse(decision="allow")
        ) as mock_pre:
            handler.handle_session_start(event)
            # external_id is rewritten to the Codex-native session_id from the hook.
            handler._session_manager.update.assert_called_once()
            update_kwargs = handler._session_manager.update.call_args.kwargs
            assert update_kwargs.get("external_id") == codex_native_id
            mock_pre.assert_called_once()

    def test_handle_session_start_compact_in_place(self) -> None:
        handler = _TestHandler()
        event = _make_event(
            event_type=HookEventType.SESSION_START,
            session_id="ext-3",
            data={
                "source": "compact",
                "agent_depth": "2",
                "terminal_context": {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"},
            },
        )

        row = MagicMock()
        row.id = "sess-1"
        row.status = "handoff_ready"
        row.summary_markdown = "Pre-compaction summary"
        row.seq_num = 1
        row.parent_session_id = None
        row.terminal_context = {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"}
        row.external_id = "ext-3"
        row.machine_id = "21000000-0000-4000-8000-000000000001"
        row.project_id = "proj-1"

        handler._session_manager.get.side_effect = lambda sid: (row if sid == "sess-1" else None)
        handler._session_manager.find_by_external_id.return_value = row

        with (
            patch.object(handler, "_derive_transcript_path", return_value=None),
            patch.object(handler, "_activate_default_agent", return_value=None),
            patch(
                "gobby.workflows.state_manager.SessionVariableManager.get_variables",
                return_value={"handoff_source": "compact"},
            ),
            patch("gobby.workflows.state_manager.SessionVariableManager.merge_variables"),
            patch(
                "gobby.hooks.event_handlers._session_start.handoff.consume_compact_handoff_marker"
            ),
            patch(
                "gobby.hooks.event_handlers._session_start.flow.reconcile_compact_session_activity"
            ) as reconcile_activity,
        ):
            reconcile_activity.return_value.success = True

            handler.handle_session_start(event)

            reconcile_activity.assert_called_once_with(handler._session_manager, "sess-1")
            handler._session_manager.register_session.assert_not_called()
            assert event.data["source"] == "compact"
            assert event.metadata["_platform_session_id"] == "sess-1"
            # In-place handoff: nothing expires and claims never change owner
            handler._session_manager.mark_session_expired.assert_not_called()
            handler._task_manager.claim_task.assert_not_called()

    def test_compact_start_without_row_degrades_without_backoff(self) -> None:
        """A compact start with no persisted row degrades to startup with no polling."""
        handler = _TestHandler()
        event = _make_event(
            event_type=HookEventType.SESSION_START,
            session_id="ext-4",
            data={"source": "compact"},
        )
        handler._session_manager.get.return_value = None
        handler._session_manager.find_by_external_id.return_value = None
        handler._session_manager.find_by_external_id_any_project.return_value = None

        with (
            patch.object(handler, "_derive_transcript_path", return_value=None),
            patch.object(handler, "_activate_default_agent", return_value=None),
            patch("time.sleep") as mock_sleep,
            patch("gobby.workflows.state_manager.SessionVariableManager") as mock_svm_cls,
        ):
            mock_svm_cls.return_value.get_variables.return_value = {}
            handler._session_manager.register_session.return_value = "new-sess-1"

            handler.handle_session_start(event)

            mock_sleep.assert_not_called()
            assert event.data["source"] == "startup"
            assert "_platform_session_id" not in event.metadata
            handler._session_manager.register_session.assert_not_called()


# ---------------------------------------------------------------------------
# _compose_session_response tests
# ---------------------------------------------------------------------------


class TestComposeSessionResponse:
    """Tests for _compose_session_response."""

    def test_basic_response(self) -> None:
        handler = _TestHandler()
        session = _make_session(seq_num=42)

        result = handler._compose_session_response(
            session=session,
            session_id="sess-1",
            external_id="ext-1",
            parent_session_id=None,
            machine_id="21000000-0000-4000-8000-000000000006",
        )
        assert isinstance(result, HookResponse)
        assert result.decision == "allow"
        assert "#42" in result.system_message

    def test_with_parent_session(self) -> None:
        handler = _TestHandler()
        session = _make_session(seq_num=42)
        parent = _make_session(session_id="parent-1", seq_num=10, summary_markdown="# S")
        handler._session_manager.get.return_value = parent

        result = handler._compose_session_response(
            session=session,
            session_id="sess-1",
            external_id="ext-1",
            parent_session_id="parent-1",
            machine_id="21000000-0000-4000-8000-000000000006",
        )
        # system_message is now session ID banner only — parent info in metadata
        assert "#42" in result.system_message
        assert result.metadata["parent_session_id"] == "parent-1"

    def test_session_banner_omits_agent_tree(self) -> None:
        handler = _TestHandler()
        session = _make_session(seq_num=42)

        result = handler._compose_session_response(
            session=session,
            session_id="sess-1",
            external_id="ext-1",
            parent_session_id=None,
            machine_id="21000000-0000-4000-8000-000000000006",
        )
        # Agent tree removed from system_message — just session ID banner
        assert "#42" in result.system_message
        assert "Agent:" not in result.system_message

    def test_with_terminal_context(self) -> None:
        handler = _TestHandler()
        session = _make_session()

        result = handler._compose_session_response(
            session=session,
            session_id="sess-1",
            external_id="ext-1",
            parent_session_id=None,
            machine_id="21000000-0000-4000-8000-000000000006",
            is_pre_created=True,
            terminal_context={"parent_pid": "12345", "gobby_session_id": None},
        )
        assert result.metadata.get("is_pre_created") is True
        assert result.metadata.get("terminal_parent_pid") == "12345"
        # None values should not be included
        assert "terminal_gobby_session_id" not in result.metadata

    def test_no_seq_num_uses_session_id(self) -> None:
        handler = _TestHandler()
        session = _make_session(seq_num=None)

        result = handler._compose_session_response(
            session=session,
            session_id="sess-uuid-1",
            external_id="ext-1",
            parent_session_id=None,
            machine_id="21000000-0000-4000-8000-000000000006",
        )
        assert "sess-uuid-1" in result.system_message

    def test_no_session_id_omits_banner(self) -> None:
        handler = _TestHandler()

        result = handler._compose_session_response(
            session=None,
            session_id=None,
            external_id="ext-1",
            parent_session_id=None,
            machine_id="21000000-0000-4000-8000-000000000006",
        )

        assert result.system_message is None
        assert result.metadata["session_id"] is None

    def test_claimed_tasks_not_in_system_message(self) -> None:
        """Claimed tasks are in additional_context, not system_message."""
        handler = _TestHandler()
        session = _make_session()

        claimed_context = "## Claimed Tasks\n- #42 [in_progress] Fix auth bug"
        result = handler._compose_session_response(
            session=session,
            session_id="sess-uuid-1",
            external_id="ext-1",
            parent_session_id=None,
            machine_id="21000000-0000-4000-8000-000000000006",
            additional_context=[claimed_context],
        )
        # Claimed tasks removed from system_message (handled by build_claimed_task_context)
        assert "Claimed Tasks" not in result.system_message
        assert result.context == claimed_context


# ---------------------------------------------------------------------------
# _get_claimed_task_info / _build_claimed_task_context tests
# ---------------------------------------------------------------------------


class TestClaimedTaskHelpers:
    """Tests for _get_claimed_task_info and _build_claimed_task_context."""

    def test_no_session_id_returns_none(self) -> None:
        handler = _TestHandler()
        assert handler._get_claimed_task_info(None, "proj-1") is None

    def test_no_session_storage_returns_none(self) -> None:
        handler = _TestHandler()
        handler._session_manager = None
        assert handler._get_claimed_task_info("sess-1", "proj-1") is None

    def test_no_task_manager_returns_none(self) -> None:
        handler = _TestHandler()
        handler._task_manager = None
        assert handler._get_claimed_task_info("sess-1", "proj-1") is None

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_no_claimed_tasks_returns_none(self, mock_svm_cls: MagicMock) -> None:
        handler = _TestHandler()
        mock_svm_cls.return_value.get_variables.return_value = {}
        assert handler._get_claimed_task_info("sess-1", "proj-1") is None

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_task_claimed_false_returns_none(self, mock_svm_cls: MagicMock) -> None:
        handler = _TestHandler()
        mock_svm_cls.return_value.get_variables.return_value = {
            "task_claimed": False,
            "claimed_tasks": {"uuid-1": True},
        }
        assert handler._get_claimed_task_info("sess-1", "proj-1") is None

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_single_claimed_task(self, mock_svm_cls: MagicMock) -> None:
        handler = _TestHandler()
        mock_svm_cls.return_value.get_variables.return_value = {
            "task_claimed": True,
            "claimed_tasks": {"uuid-aaa": True},
        }
        task = MagicMock()
        task.seq_num = 42
        task.status = "in_progress"
        task.title = "Fix auth bug"
        task.claimed_by_session_id = "sess-1"
        handler._task_manager.get_task.return_value = task

        result = handler._get_claimed_task_info("sess-1", "proj-1")
        assert result == [("#42", "in_progress", "Fix auth bug")]

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_stale_claimed_task_owned_by_other_session_is_pruned(
        self, mock_svm_cls: MagicMock
    ) -> None:
        handler = _TestHandler()
        mock_svm = mock_svm_cls.return_value
        mock_svm.get_variables.return_value = {
            "task_claimed": True,
            "claimed_tasks": {"uuid-14997": "#14997"},
        }
        task = MagicMock()
        task.seq_num = 14997
        task.status = "ready"
        task.title = "Coordinate gobby build for #12746"
        task.claimed_by_session_id = "session-5815"
        handler._task_manager.get_task.return_value = task

        result = handler._get_claimed_task_info("session-5867", "proj-1")

        assert result is None
        mock_svm.set_variable.assert_any_call("session-5867", "task_claimed", False)
        mock_svm.set_variable.assert_any_call("session-5867", "claimed_tasks", {})

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_db_fallback_rebuilds_review_claims(self, mock_svm_cls: MagicMock) -> None:
        handler = _TestHandler()
        mock_svm = mock_svm_cls.return_value
        mock_svm.get_variables.return_value = {}

        task = MagicMock()
        task.id = "uuid-review"
        task.seq_num = 55
        task.status = "needs_review"
        task.title = "Review the patch"
        handler._task_manager.list_tasks.return_value = [task]

        result = handler._get_claimed_task_info("sess-1", "proj-1")

        assert result == [("#55", "needs_review", "Review the patch")]
        handler._task_manager.list_tasks.assert_called_once_with(
            claimed_by_session_id="sess-1",
            current_stage_state=list(ACTIVE_STAGE_STATES),
            project_id="proj-1",
        )
        mock_svm.set_variable.assert_any_call("sess-1", "task_claimed", True)
        mock_svm.set_variable.assert_any_call("sess-1", "claimed_tasks", {"uuid-review": "#55"})

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_db_fallback_returns_claims_when_reconcile_write_fails(
        self, mock_svm_cls: MagicMock
    ) -> None:
        handler = _TestHandler()
        mock_svm = mock_svm_cls.return_value
        mock_svm.get_variables.return_value = {}
        mock_svm.set_variable.side_effect = RuntimeError("write failed")

        task = MagicMock()
        task.id = "uuid-review"
        task.seq_num = 55
        task.status = "needs_review"
        task.title = "Review the patch"
        handler._task_manager.list_tasks.return_value = [task]

        result = handler._get_claimed_task_info("sess-1", "proj-1")

        assert result == [("#55", "needs_review", "Review the patch")]

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_multiple_claimed_tasks(self, mock_svm_cls: MagicMock) -> None:
        handler = _TestHandler()
        mock_svm_cls.return_value.get_variables.return_value = {
            "task_claimed": True,
            "claimed_tasks": {"uuid-aaa": True, "uuid-bbb": True},
        }

        task_a = MagicMock()
        task_a.seq_num = 42
        task_a.status = "in_progress"
        task_a.title = "Fix auth"
        task_a.claimed_by_session_id = "sess-1"

        task_b = MagicMock()
        task_b.seq_num = 43
        task_b.status = "open"
        task_b.title = "Write tests"
        task_b.claimed_by_session_id = "sess-1"

        handler._task_manager.get_task.side_effect = [task_a, task_b]

        result = handler._get_claimed_task_info("sess-1", "proj-1")
        assert result is not None
        assert len(result) == 2
        assert ("#42", "in_progress", "Fix auth") in result
        assert ("#43", "open", "Write tests") in result

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_deleted_task_graceful_fallback(self, mock_svm_cls: MagicMock) -> None:
        handler = _TestHandler()
        mock_svm = mock_svm_cls.return_value
        mock_svm.get_variables.return_value = {
            "task_claimed": True,
            "claimed_tasks": {"abcdef12-dead-0000-0000-000000000000": True},
        }
        handler._task_manager.get_task.side_effect = ValueError("Task not found")

        result = handler._get_claimed_task_info("sess-1", "proj-1")
        assert result is None
        mock_svm.set_variable.assert_any_call("sess-1", "task_claimed", False)
        mock_svm.set_variable.assert_any_call("sess-1", "claimed_tasks", {})

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_no_seq_num_uses_uuid_prefix(self, mock_svm_cls: MagicMock) -> None:
        handler = _TestHandler()
        mock_svm_cls.return_value.get_variables.return_value = {
            "task_claimed": True,
            "claimed_tasks": {"abcdef12-1234-5678-9abc-000000000000": True},
        }
        task = MagicMock()
        task.seq_num = None
        task.status = "open"
        task.title = "No seq task"
        task.claimed_by_session_id = "sess-1"
        handler._task_manager.get_task.return_value = task

        result = handler._get_claimed_task_info("sess-1", "proj-1")
        assert result == [("abcdef12", "open", "No seq task")]

    def test_session_variable_error_returns_none(self) -> None:
        """DB errors (e.g. mocked DB) are handled gracefully."""
        handler = _TestHandler()
        # _session_manager.db is a MagicMock, so SessionVariableManager
        # will fail — our try/except should catch it
        result = handler._get_claimed_task_info("sess-1", "proj-1")
        assert result is None

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_build_claimed_task_context_none(self, mock_svm_cls: MagicMock) -> None:
        handler = _TestHandler()
        mock_svm_cls.return_value.get_variables.return_value = {}
        assert handler._build_claimed_task_context("sess-1", "proj-1") is None

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_build_claimed_task_context_formatted(self, mock_svm_cls: MagicMock) -> None:
        handler = _TestHandler()
        mock_svm_cls.return_value.get_variables.return_value = {
            "task_claimed": True,
            "claimed_tasks": {"uuid-aaa": True},
        }
        task = MagicMock()
        task.seq_num = 42
        task.status = "in_progress"
        task.title = "Fix auth bug"
        task.claimed_by_session_id = "sess-1"
        handler._task_manager.get_task.return_value = task

        ctx = handler._build_claimed_task_context("sess-1", "proj-1")
        assert ctx is not None
        assert "## Claimed Tasks (Persisted)" in ctx
        assert "#42 [in_progress] Fix auth bug" in ctx
        assert "still assigned to you" in ctx
