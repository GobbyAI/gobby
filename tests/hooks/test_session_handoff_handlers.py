"""In-place compact session handoff tests."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
import yaml

from gobby.hooks.event_handlers import EventHandlers
from gobby.hooks.event_handlers._session_start.handoff import (
    SessionStartResolution,
    _bound_handoff_summary,
    prepare_compact_continuation_variables,
    resolve_session_start_identity,
)
from gobby.hooks.event_handlers._session_start.in_place_compact import (
    apply_in_place_compact_context_loss,
)
from gobby.hooks.events import HookEventType, HookResponse
from gobby.hooks.session_types import HookSessionManager
from gobby.hooks.tool_error_tracker import normalize_open_tool_error_records
from gobby.llm.sdk_utils import HANDOFF_SUMMARY_INJECT_BUDGET
from gobby.sessions.clear_continuation import (
    ClearContinuationResolution,
    stage_clear_attempt,
)
from gobby.sessions.compact_continuation import (
    COMPACT_HANDOFF_MARKER_VARIABLE,
    COMPACT_SELF_CONTINUE_PROMPT,
    COMPACT_SELF_CONTINUE_VARIABLE,
    consume_compact_self_continuation_pending,
    mark_compact_self_continuation_pending,
)
from gobby.sessions.compact_identity import CompactIdentityResolution
from gobby.sessions.compact_markers import COMPACT_HANDOFF_INJECT_PENDING_VARIABLE
from gobby.sessions.summary_formatting import format_unresolved_errors
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_activity import SessionActivityResolution
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.definitions import AgentDefinitionBody
from gobby.workflows.state_manager import SessionVariableManager

from ._event_handler_helpers import make_event

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


COMPACT_EXTERNAL_ID = "cccccccc-0000-4000-8000-000000000001"
CLI_EXTERNAL_IDS = {
    "claude": COMPACT_EXTERNAL_ID,
    "codex": "cccccccc-0000-4000-8000-000000000002",
    "qwen": "cccccccc-0000-4000-8000-000000000003",
    "droid": "cccccccc-0000-4000-8000-000000000004",
    "grok": "cccccccc-0000-4000-8000-000000000005",
}
TERMINAL_CONTEXT = {"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"}
CLEAR_HANDOFF = "Continue epic #20539: bind the successor once and inject this handoff."
_DEFAULT_TERMINAL_CONTEXT = object()


def _make_row(
    *,
    session_id: str = "sess-123",
    status: str = "handoff_ready",
    terminal_context: dict[str, Any] | None | object = _DEFAULT_TERMINAL_CONTEXT,
) -> MagicMock:
    row = MagicMock()
    row.id = session_id
    row.external_id = "ext-123"
    row.machine_id = "21000000-0000-4000-8000-000000000001"
    row.source = "claude"
    row.status = status
    row.project_id = "project-1"
    row.parent_session_id = None
    row.transcript_path = "/canonical/transcript.jsonl"
    row.terminal_context = (
        dict(TERMINAL_CONTEXT)
        if terminal_context is _DEFAULT_TERMINAL_CONTEXT
        else terminal_context
    )
    return row


def _make_resolver_handler(row: MagicMock | None) -> MagicMock:
    handler = MagicMock()
    handler._session_manager.find_by_external_id.return_value = row
    handler._session_manager.find_by_external_id_any_project.return_value = None
    handler.logger = logging.getLogger("test.resolve_identity")
    return handler


class TestResolveSessionStartIdentity:
    """One-shot compact classification against the persisted session row."""

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_explicit_compact_with_handoff_ready_row_classifies_compact(
        self, mock_sv_mgr_cls: MagicMock
    ) -> None:
        mock_sv_mgr_cls.return_value = MagicMock(get_variables=MagicMock(return_value={}))
        row = _make_row()
        handler = _make_resolver_handler(row)
        input_data: dict[str, Any] = {"source": "compact", "terminal_context": TERMINAL_CONTEXT}

        resolution = resolve_session_start_identity(
            handler,
            input_data,
            "compact",
            external_id="ext-1",
            machine_id="21000000-0000-4000-8000-000000000001",
            project_id="project-1",
            cli_source="claude",
            terminal_context=input_data.get("terminal_context"),
        )

        assert resolution.is_compact
        assert resolution.session is row
        assert resolution.blocked_reason is None
        assert input_data["source"] == "compact"

    def test_explicit_compact_with_missing_row_degrades_to_startup(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        handler = _make_resolver_handler(None)
        input_data: dict[str, Any] = {"source": "compact"}
        caplog.set_level(logging.WARNING, logger=handler.logger.name)

        resolution = resolve_session_start_identity(
            handler,
            input_data,
            "compact",
            external_id="ext-1",
            machine_id="21000000-0000-4000-8000-000000000001",
            project_id="project-1",
            cli_source="claude",
            terminal_context=input_data.get("terminal_context"),
        )

        assert not resolution.is_compact
        assert resolution.session is None
        assert resolution.session_source == "startup"
        assert resolution.blocked_reason is None
        assert input_data["source"] == "startup"
        assert "no persisted session" in caplog.text

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_sourceless_start_with_handoff_ready_row_promotes_to_compact(
        self, mock_sv_mgr_cls: MagicMock
    ) -> None:
        """Providers that omit source='compact' still classify via row state."""
        mock_sv_mgr_cls.return_value = MagicMock(get_variables=MagicMock(return_value={}))
        row = _make_row()
        handler = _make_resolver_handler(row)
        input_data: dict[str, Any] = {"terminal_context": TERMINAL_CONTEXT}

        resolution = resolve_session_start_identity(
            handler,
            input_data,
            "startup",
            external_id="ext-1",
            machine_id="21000000-0000-4000-8000-000000000001",
            project_id="project-1",
            cli_source="codex",
            terminal_context=input_data.get("terminal_context"),
        )

        assert resolution.is_compact
        assert input_data["source"] == "compact"

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_expired_row_with_unconsumed_marker_classifies_compact_revival(
        self, mock_sv_mgr_cls: MagicMock
    ) -> None:
        mock_sv_mgr_cls.return_value = MagicMock(
            get_variables=MagicMock(return_value={COMPACT_HANDOFF_MARKER_VARIABLE: "compact"})
        )
        row = _make_row(status="expired")
        handler = _make_resolver_handler(row)
        input_data: dict[str, Any] = {"terminal_context": TERMINAL_CONTEXT}

        resolution = resolve_session_start_identity(
            handler,
            input_data,
            "startup",
            external_id="ext-1",
            machine_id="21000000-0000-4000-8000-000000000001",
            project_id="project-1",
            cli_source="claude",
            terminal_context=input_data.get("terminal_context"),
        )

        assert resolution.is_compact
        assert input_data["source"] == "compact"

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_expired_row_without_marker_stays_normal(self, mock_sv_mgr_cls: MagicMock) -> None:
        """Compact → normal end → normal startup must classify as normal."""
        mock_sv_mgr_cls.return_value = MagicMock(get_variables=MagicMock(return_value={}))
        row = _make_row(status="expired")
        handler = _make_resolver_handler(row)
        input_data: dict[str, Any] = {"terminal_context": TERMINAL_CONTEXT}

        resolution = resolve_session_start_identity(
            handler,
            input_data,
            "startup",
            external_id="ext-1",
            machine_id="21000000-0000-4000-8000-000000000001",
            project_id="project-1",
            cli_source="claude",
            terminal_context=input_data.get("terminal_context"),
        )

        assert not resolution.is_compact
        assert resolution.session_source == "startup"
        assert "source" not in input_data

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_active_row_without_compact_source_stays_normal(
        self, mock_sv_mgr_cls: MagicMock
    ) -> None:
        mock_sv_mgr_cls.return_value = MagicMock(get_variables=MagicMock(return_value={}))
        row = _make_row(status="active")
        handler = _make_resolver_handler(row)

        resolution = resolve_session_start_identity(
            handler,
            {"terminal_context": TERMINAL_CONTEXT},
            "startup",
            external_id="ext-1",
            machine_id="21000000-0000-4000-8000-000000000001",
            project_id="project-1",
            cli_source="claude",
            terminal_context=TERMINAL_CONTEXT,
        )

        assert not resolution.is_compact
        assert resolution.session is row

    def test_compact_resolver_database_failure_degrades_to_normal_start(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        row = _make_row(status="active")
        handler = _make_resolver_handler(row)

        with patch(
            "gobby.hooks.event_handlers._session_start.handoff.resolve_compact_continuation",
            side_effect=RuntimeError("database unavailable"),
        ):
            resolution = resolve_session_start_identity(
                handler,
                {"terminal_context": TERMINAL_CONTEXT},
                "startup",
                external_id="ext-1",
                machine_id="21000000-0000-4000-8000-000000000001",
                project_id="project-1",
                cli_source="claude",
                terminal_context=TERMINAL_CONTEXT,
            )

        assert not resolution.is_compact
        assert resolution.session is row
        assert "Compact continuation lookup failed" in caplog.text

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_conflicting_terminal_identity_blocks(self, mock_sv_mgr_cls: MagicMock) -> None:
        mock_sv_mgr_cls.return_value = MagicMock(get_variables=MagicMock(return_value={}))
        row = _make_row(terminal_context={"tmux_pane": "%12", "tmux_socket_path": "/tmp/tmux"})
        handler = _make_resolver_handler(row)
        input_data: dict[str, Any] = {
            "source": "compact",
            "terminal_context": {"tmux_pane": "%99", "tmux_socket_path": "/tmp/tmux"},
        }

        resolution = resolve_session_start_identity(
            handler,
            input_data,
            "compact",
            external_id="ext-1",
            machine_id="21000000-0000-4000-8000-000000000001",
            project_id="project-1",
            cli_source="claude",
            terminal_context=input_data.get("terminal_context"),
        )

        assert resolution.blocked_reason is not None
        assert not resolution.is_compact

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_insufficient_terminal_identity_never_blocks(self, mock_sv_mgr_cls: MagicMock) -> None:
        mock_sv_mgr_cls.return_value = MagicMock(get_variables=MagicMock(return_value={}))
        row = _make_row(terminal_context=None)
        handler = _make_resolver_handler(row)
        input_data: dict[str, Any] = {
            "source": "compact",
            "terminal_context": {"tmux_pane": "%99", "tmux_socket_path": "/tmp/tmux"},
        }

        resolution = resolve_session_start_identity(
            handler,
            input_data,
            "compact",
            external_id="ext-1",
            machine_id="21000000-0000-4000-8000-000000000001",
            project_id="project-1",
            cli_source="claude",
            terminal_context=input_data.get("terminal_context"),
        )

        assert resolution.blocked_reason is None
        assert resolution.is_compact

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_project_drift_reuses_row_with_warning(
        self, mock_sv_mgr_cls: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        mock_sv_mgr_cls.return_value = MagicMock(get_variables=MagicMock(return_value={}))
        row = _make_row()
        row.project_id = "project-other"
        handler = _make_resolver_handler(None)
        handler._session_manager.find_by_external_id_any_project.return_value = row
        caplog.set_level(logging.WARNING, logger=handler.logger.name)

        resolution = resolve_session_start_identity(
            handler,
            {"source": "compact", "terminal_context": TERMINAL_CONTEXT},
            "compact",
            external_id="ext-1",
            machine_id="21000000-0000-4000-8000-000000000001",
            project_id="project-1",
            cli_source="claude",
            terminal_context=TERMINAL_CONTEXT,
        )

        assert resolution.is_compact
        assert resolution.session is row
        assert "cwd/project drift" in caplog.text

    @patch("gobby.hooks.event_handlers._session_start.handoff.resolve_clear_continuation")
    def test_clear_source_resolves_marker_and_carries_predecessor(
        self, mock_resolve_clear: MagicMock
    ) -> None:
        predecessor = _make_row(session_id="pred-clear-1")
        mock_resolve_clear.return_value = ClearContinuationResolution(
            predecessor=predecessor,
            attempt_id="attempt-clear-1",
        )
        handler = _make_resolver_handler(predecessor)
        input_data: dict[str, Any] = {
            "source": "clear",
            "terminal_context": {**TERMINAL_CONTEXT, "gobby_session_id": predecessor.id},
        }

        resolution = resolve_session_start_identity(
            handler,
            input_data,
            "clear",
            external_id="ext-new",
            machine_id=LOCAL_MACHINE_ID,
            project_id="project-1",
            cli_source="claude",
            terminal_context=input_data.get("terminal_context"),
        )

        assert not resolution.is_compact
        assert resolution.session is None
        assert resolution.session_source == "clear"
        assert resolution.clear_predecessor is predecessor
        assert resolution.clear_attempt_id == "attempt-clear-1"
        assert resolution.clear_degrade_reason is None
        mock_resolve_clear.assert_called_once()
        kwargs = mock_resolve_clear.call_args.kwargs
        assert kwargs["source"] == "claude"
        assert kwargs["project_id"] == "project-1"
        assert kwargs["machine_id"] == LOCAL_MACHINE_ID
        assert kwargs["predecessor_hint"] == predecessor.id
        handler._session_manager.find_by_external_id.assert_not_called()

    @pytest.mark.parametrize(
        "returned",
        [
            ClearContinuationResolution(),
            ClearContinuationResolution(degrade_reason="expired"),
            ClearContinuationResolution(degrade_reason="identity_mismatch"),
            ClearContinuationResolution(degrade_reason="ambiguous"),
            ClearContinuationResolution(degrade_reason="exception"),
            ClearContinuationResolution(degrade_reason="cross_project"),
            ClearContinuationResolution(degrade_reason="cross_machine"),
        ],
    )
    @patch("gobby.hooks.event_handlers._session_start.handoff.resolve_clear_continuation")
    def test_clear_unusable_marker_degrades_to_independent_start(
        self,
        mock_resolve_clear: MagicMock,
        returned: ClearContinuationResolution,
    ) -> None:
        mock_resolve_clear.return_value = returned
        handler = _make_resolver_handler(_make_row())

        resolution = resolve_session_start_identity(
            handler,
            {"source": "clear", "terminal_context": dict(TERMINAL_CONTEXT)},
            "clear",
            external_id="ext-new",
            machine_id=LOCAL_MACHINE_ID,
            project_id="project-1",
            cli_source="claude",
            terminal_context=dict(TERMINAL_CONTEXT),
        )

        assert not resolution.is_compact
        assert resolution.session is None
        assert resolution.clear_predecessor is None
        assert resolution.clear_attempt_id is None
        assert resolution.session_source == "clear"
        assert resolution.clear_degrade_reason == returned.degrade_reason
        handler._session_manager.find_by_external_id.assert_not_called()

    @patch("gobby.hooks.event_handlers._session_start.handoff.resolve_clear_continuation")
    def test_clear_resolver_exception_degrades_to_independent_start(
        self, mock_resolve_clear: MagicMock
    ) -> None:
        mock_resolve_clear.side_effect = RuntimeError("database unavailable")
        handler = _make_resolver_handler(_make_row())

        resolution = resolve_session_start_identity(
            handler,
            {"source": "clear", "terminal_context": dict(TERMINAL_CONTEXT)},
            "clear",
            external_id="ext-new",
            machine_id=LOCAL_MACHINE_ID,
            project_id="project-1",
            cli_source="claude",
            terminal_context=dict(TERMINAL_CONTEXT),
        )

        assert not resolution.is_compact
        assert resolution.session is None
        assert resolution.clear_predecessor is None
        assert resolution.clear_attempt_id is None
        assert resolution.clear_degrade_reason == "exception"


class TestClearResolutionTerminalIdentity:
    """Clear successors resolve against the enriched context, never the raw payload."""

    RAW_CONTEXT = {**TERMINAL_CONTEXT, "parent_pid": 4242}
    ENRICHED_CONTEXT = {**RAW_CONTEXT, "cwd": "/some/dir", "parent_create_time": 1787339970.3}

    def _stage_predecessor(self, db: HubDatabase) -> Session:
        project = LocalProjectManager(db).create(name="clear-identity", repo_path="/some/dir")
        predecessor = SessionManager(db).register(
            external_id="pred-ext",
            machine_id=LOCAL_MACHINE_ID,
            source="claude",
            project_id=project.id,
            terminal_context=dict(self.ENRICHED_CONTEXT),
        )
        stage_clear_attempt(
            db,
            predecessor.id,
            attempt_id="attempt-enriched",
            terminal_context=dict(self.ENRICHED_CONTEXT),
            chat_context=None,
        )
        return predecessor

    def _resolve(
        self,
        db: HubDatabase,
        project_id: str,
        terminal_context: dict[str, Any],
    ) -> SessionStartResolution:
        handler = MagicMock()
        handler._session_manager.db = db
        handler.logger = logging.getLogger("test.clear_identity")
        return resolve_session_start_identity(
            handler,
            {"source": "clear", "terminal_context": dict(self.RAW_CONTEXT)},
            "clear",
            external_id="succ-ext",
            machine_id=LOCAL_MACHINE_ID,
            project_id=project_id,
            cli_source="claude",
            terminal_context=terminal_context,
        )

    def test_enriched_context_resolves_staged_marker(self, hub_db: HubDatabase) -> None:
        predecessor = self._stage_predecessor(hub_db)

        resolution = self._resolve(hub_db, predecessor.project_id, dict(self.ENRICHED_CONTEXT))

        assert resolution.clear_degrade_reason is None
        assert resolution.clear_predecessor is not None
        assert resolution.clear_predecessor.id == predecessor.id
        assert resolution.clear_attempt_id == "attempt-enriched"

    def test_raw_payload_alone_is_identity_mismatch(self, hub_db: HubDatabase) -> None:
        predecessor = self._stage_predecessor(hub_db)

        resolution = self._resolve(hub_db, predecessor.project_id, dict(self.RAW_CONTEXT))

        assert resolution.clear_degrade_reason == "identity_mismatch"
        assert resolution.clear_predecessor is None


class TestSessionStartInPlaceCompact:
    """Handler-level in-place compact reactivation."""

    @patch("gobby.hooks.event_handlers._session_start.flow.reconcile_compact_session_activity")
    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_session_start_compact_reactivates_same_row(
        self,
        mock_sv_mgr_cls: MagicMock,
        mock_reconcile: MagicMock,
        mock_dependencies: dict,
    ) -> None:
        mock_sv_mgr_cls.return_value = MagicMock(get_variables=MagicMock(return_value={}))
        row = _make_row(session_id="sess-123")
        row.summary_markdown = None
        mock_reconcile.return_value = SessionActivityResolution(session=row)

        def get_session(session_id: str) -> MagicMock | None:
            return row if session_id == "sess-123" else None

        mock_dependencies["session_storage"].get.side_effect = get_session
        mock_dependencies["session_storage"].find_by_external_id.return_value = row
        mock_dependencies["session_manager"].register_session.return_value = "sess-123"

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-123",
            data={
                "source": "compact",
                "cwd": "/some/dir",
                "terminal_context": dict(TERMINAL_CONTEXT),
            },
            metadata={},
        )

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        assert event.metadata["_platform_session_id"] == "sess-123"
        mock_reconcile.assert_called_once_with(mock_dependencies["session_manager"], row.id)
        mock_dependencies["session_manager"].register_session.assert_not_called()
        mock_dependencies["session_manager"].mark_session_expired.assert_not_called()

    @patch("gobby.hooks.event_handlers._session_start.flow.reconcile_compact_session_activity")
    @patch("gobby.hooks.event_handlers._session_start.handoff.resolve_compact_continuation")
    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_session_start_compact_canonicalizes_conflicting_observed_id_without_registration(
        self,
        mock_sv_mgr_cls: MagicMock,
        mock_resolve_compact: MagicMock,
        mock_reconcile: MagicMock,
        mock_dependencies: dict,
    ) -> None:
        mock_sv_mgr_cls.return_value = MagicMock(get_variables=MagicMock(return_value={}))
        canonical = _make_row(session_id="canonical-session")
        canonical.external_id = "canonical-provider-id"
        canonical.summary_markdown = None
        ghost = _make_row(session_id="ghost-session", status="active")
        ghost.external_id = "conflicting-observed-id"
        mock_dependencies["session_storage"].find_by_external_id.return_value = ghost
        mock_dependencies["session_storage"].get.side_effect = (
            lambda session_id: canonical if session_id == canonical.id else None
        )
        mock_resolve_compact.return_value = CompactIdentityResolution(session=canonical)
        mock_reconcile.return_value = SessionActivityResolution(session=canonical)

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="conflicting-observed-id",
            data={
                "source": "compact",
                "cwd": "/some/dir",
                "terminal_context": dict(TERMINAL_CONTEXT),
            },
            metadata={},
        )

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        assert event.session_id == "canonical-provider-id"
        assert event.metadata["_observed_external_id"] == "conflicting-observed-id"
        assert event.metadata["_platform_session_id"] == canonical.id
        mock_dependencies["session_manager"].register_session.assert_not_called()

    @patch("gobby.hooks.event_handlers._session_start.flow.reconcile_compact_session_activity")
    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_session_start_compact_bounds_large_summary_with_breadcrumb(
        self,
        mock_sv_mgr_cls: MagicMock,
        mock_reconcile: MagicMock,
        mock_dependencies: dict,
    ) -> None:
        """A large pre-compaction summary is bounded for injection but kept full elsewhere."""
        mock_sv_mgr = MagicMock()
        mock_sv_mgr.get_variables.return_value = {"auto_inject_handoff": True}
        mock_sv_mgr_cls.return_value = mock_sv_mgr

        big_summary = "# Big Summary\n\n" + ("detail paragraph.\n\n" * 1000)
        assert len(big_summary) > HANDOFF_SUMMARY_INJECT_BUDGET

        row = _make_row(session_id="sess-123")
        row.seq_num = 42
        row.summary_markdown = big_summary
        mock_reconcile.return_value = SessionActivityResolution(session=row)

        def get_session(session_id: str) -> MagicMock | None:
            return row if session_id == "sess-123" else None

        mock_dependencies["session_storage"].get.side_effect = get_session
        mock_dependencies["session_storage"].find_by_external_id.return_value = row
        mock_dependencies["session_manager"].register_session.return_value = "sess-123"

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id="ext-123",
            data={
                "source": "compact",
                "cwd": "/some/dir",
                "terminal_context": dict(TERMINAL_CONTEXT),
            },
            metadata={},
        )

        response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        handoff_payload = next(
            args[1]
            for args, _kwargs in mock_sv_mgr.merge_variables.call_args_list
            if "handoff_summary_injectable" in args[1]
        )
        injectable = handoff_payload["handoff_summary_injectable"]
        assert handoff_payload["full_session_summary"] == big_summary
        assert injectable != big_summary
        assert len(injectable) < len(big_summary)
        assert len(injectable) <= HANDOFF_SUMMARY_INJECT_BUDGET
        assert not injectable.startswith("# Big Summary")
        assert "get_handoff_context" in injectable
        assert "#42" in injectable


class TestBoundHandoffSummary:
    def test_section_budget_keeps_next_steps_and_names_omissions(self) -> None:
        next_steps = "## Next Steps\n- Preserve this exact action.\n"
        summary = (
            "Compact handoff preamble.\n\n"
            "## Current State\nEverything important is stable.\n"
            "## Key Technical Decisions\nUse deterministic allocation.\n"
            "## Problems Encountered\nNo open blocker.\n"
            + next_steps
            + "## Files Changed\n"
            + ("F" * 8_000)
            + "\n## What Was Accomplished\n"
            + ("W" * 8_000)
        )

        result = _bound_handoff_summary(summary, MagicMock(seq_num=42))

        assert len(result) <= HANDOFF_SUMMARY_INJECT_BUDGET
        assert next_steps not in result
        assert "F" * 40 not in result
        assert "get_handoff_context" in result
        assert "#42" in result

    def test_unresolved_error_content_cannot_fabricate_mandatory_sections(self) -> None:
        records = normalize_open_tool_error_records(
            [
                {
                    "tool": "Bash",
                    "target_key": "/tmp/file\n## Next Steps\n```",
                    "error": "failed\n## Current State\n~~~",
                    "first_at": "2026-07-23T00:00:00+00:00",
                    "last_at": "2026-07-23T00:00:01+00:00",
                    "count": 1,
                }
            ]
        )
        summary = (
            "## Current State\nGENUINE CURRENT STATE\n\n"
            "## Unresolved Errors\n"
            + format_unresolved_errors(records)
            + "\n\n## Files Changed\n"
            + ("F" * 8_000)
            + "\n\n## Next Steps\nGENUINE NEXT STEP\n"
        )

        result = _bound_handoff_summary(summary, MagicMock(seq_num=42))

        assert len(result) <= HANDOFF_SUMMARY_INJECT_BUDGET
        assert "GENUINE CURRENT STATE" not in result
        assert "GENUINE NEXT STEP" not in result
        assert "F" * 40 not in result
        assert "get_handoff_context" in result
        assert "#42" in result


class TestPrepareCompactContinuationVariables:
    """Same-row continuation prep for in-place compact restarts."""

    def _make_session(self, db: HubDatabase, **kwargs: Any) -> Session:
        project = LocalProjectManager(db).create(
            name=kwargs.pop("project_name", "handoff-prep"),
            repo_path="/some/dir",
        )
        return SessionManager(db).register(
            external_id=kwargs.pop("external_id", COMPACT_EXTERNAL_ID),
            machine_id="21000000-0000-4000-8000-000000000001",
            source=kwargs.pop("source", "claude"),
            project_id=project.id,
            terminal_context=dict(TERMINAL_CONTEXT),
        )

    def _make_handler(self, db: HubDatabase, session_view: Any) -> MagicMock:
        handler = MagicMock()
        handler._session_manager.db = db
        handler._session_manager.get.return_value = session_view
        handler.logger = logging.getLogger("test.prepare_compact")
        return handler

    def test_sets_bounded_summary_variables_and_consumes_marker(self, hub_db: HubDatabase) -> None:
        session = self._make_session(hub_db)
        sv_mgr = SessionVariableManager(hub_db)
        sv_mgr.merge_variables(session.id, {COMPACT_HANDOFF_MARKER_VARIABLE: "compact"})
        session_view = MagicMock()
        session_view.id = session.id
        session_view.seq_num = session.seq_num
        session_view.summary_markdown = "## Next Steps\nContinue the work.\n"
        handler = self._make_handler(hub_db, session_view)

        prepare_compact_continuation_variables(handler, session.id, "compact")

        variables = sv_mgr.get_variables(session.id)
        assert variables["session_summary"] == "## Next Steps\nContinue the work.\n"
        assert variables["full_session_summary"] == "## Next Steps\nContinue the work.\n"
        assert variables["handoff_summary_injectable"] == "## Next Steps\nContinue the work.\n"
        assert COMPACT_HANDOFF_MARKER_VARIABLE not in variables

    def test_clears_stale_summary_variables_without_current_summary(
        self, hub_db: HubDatabase
    ) -> None:
        session = self._make_session(hub_db, project_name="handoff-prep-stale")
        sv_mgr = SessionVariableManager(hub_db)
        sv_mgr.merge_variables(
            session.id,
            {
                "session_summary": "stale",
                "full_session_summary": "stale",
                "handoff_summary_injectable": "stale",
            },
        )
        session_view = MagicMock()
        session_view.id = session.id
        session_view.summary_markdown = None
        handler = self._make_handler(hub_db, session_view)

        prepare_compact_continuation_variables(handler, session.id, "compact")

        variables = sv_mgr.get_variables(session.id)
        assert variables["session_summary"] == ""
        assert variables["full_session_summary"] == ""
        assert variables["handoff_summary_injectable"] == ""

    @pytest.mark.parametrize("auto_inject", [False, "false", "0"])
    def test_auto_inject_disabled_clears_and_consumes_marker(
        self,
        hub_db: HubDatabase,
        auto_inject: bool | str,
    ) -> None:
        session = self._make_session(hub_db, project_name="handoff-prep-optout")
        sv_mgr = SessionVariableManager(hub_db)
        sv_mgr.merge_variables(
            session.id,
            {
                "auto_inject_handoff": auto_inject,
                "session_summary": "stale",
                COMPACT_HANDOFF_MARKER_VARIABLE: "compact",
            },
        )
        session_view = MagicMock()
        session_view.id = session.id
        session_view.summary_markdown = "## Next Steps\nDo not inject me.\n"
        handler = self._make_handler(hub_db, session_view)

        prepare_compact_continuation_variables(handler, session.id, "compact")

        variables = sv_mgr.get_variables(session.id)
        assert variables["session_summary"] == ""
        assert COMPACT_HANDOFF_MARKER_VARIABLE not in variables

    def test_normalizes_required_skill_reload_list(self, hub_db: HubDatabase) -> None:
        session = self._make_session(hub_db, project_name="handoff-prep-skills")
        sv_mgr = SessionVariableManager(hub_db)
        sv_mgr.merge_variables(
            session.id,
            {"compact_resume_required_skills": ["tasks", " tasks ", "", "python", "tasks"]},
        )
        session_view = MagicMock()
        session_view.id = session.id
        session_view.summary_markdown = None
        handler = self._make_handler(hub_db, session_view)

        prepare_compact_continuation_variables(handler, session.id, "compact")

        variables = sv_mgr.get_variables(session.id)
        assert variables["compact_resume_required_skills"] == ["tasks", "python"]

    def test_normalization_drops_meta_skills_from_both_tiers(self, hub_db: HubDatabase) -> None:
        session = self._make_session(hub_db, project_name="handoff-prep-meta-skills")
        sv_mgr = SessionVariableManager(hub_db)
        sv_mgr.merge_variables(
            session.id,
            {
                "compact_resume_required_skills": ["loading-skills", "tasks", "brevity"],
                "compact_resume_advisory_skills": ["brevity", "restraint"],
            },
        )
        session_view = MagicMock()
        session_view.id = session.id
        session_view.summary_markdown = None
        handler = self._make_handler(hub_db, session_view)

        prepare_compact_continuation_variables(handler, session.id, "compact")

        variables = sv_mgr.get_variables(session.id)
        assert variables["compact_resume_required_skills"] == ["tasks"]
        assert variables["compact_resume_advisory_skills"] == ["restraint"]

    def test_non_compact_source_is_untouched(self, hub_db: HubDatabase) -> None:
        session = self._make_session(hub_db, project_name="handoff-prep-normal")
        sv_mgr = SessionVariableManager(hub_db)
        sv_mgr.merge_variables(session.id, {"session_summary": "existing"})
        handler = self._make_handler(hub_db, MagicMock())

        prepare_compact_continuation_variables(handler, session.id, "startup")

        variables = sv_mgr.get_variables(session.id)
        assert variables["session_summary"] == "existing"
        handler._session_manager.get.assert_not_called()

    def test_in_place_closeout_arms_pending_and_resets_tracking(self, hub_db: HubDatabase) -> None:
        session = self._make_session(hub_db, project_name="handoff-prep-closeout")
        sv_mgr = SessionVariableManager(hub_db)
        sv_mgr.merge_variables(
            session.id,
            {
                COMPACT_HANDOFF_MARKER_VARIABLE: "compact",
                "plan_mode": True,
                "unlocked_tools": ["call_tool"],
                "loaded_skills": ["tasks"],
                "suggested_skill_names": ["python"],
                "workflow_requested_skills": ["restraint"],
                "memory_nudge_fired": True,
                "injected_memory_ids": ["mem-1"],
                "_agent_context_injected": True,
                "_agent_context_rehydrate_pending": False,
                "wiki_overview_injected": True,
            },
        )
        session_view = MagicMock()
        session_view.id = session.id
        session_view.seq_num = session.seq_num
        session_view.project_id = session.project_id
        session_view.summary_markdown = "## Next Steps\nContinue the work.\n"
        handler = self._make_handler(hub_db, session_view)

        apply_in_place_compact_context_loss(handler, session.id)

        variables = sv_mgr.get_variables(session.id)
        assert variables["handoff_summary_injectable"] == "## Next Steps\nContinue the work.\n"
        assert COMPACT_HANDOFF_MARKER_VARIABLE not in variables
        assert variables[COMPACT_HANDOFF_INJECT_PENDING_VARIABLE] is True
        assert variables["unlocked_tools"] == []
        assert variables["loaded_skills"] == []
        assert variables["suggested_skill_names"] == []
        assert variables["workflow_requested_skills"] == []
        assert variables["memory_nudge_fired"] is False
        assert variables["injected_memory_ids"] == []
        assert variables["_agent_context_injected"] is False
        assert variables["_agent_context_rehydrate_pending"] is True
        assert variables["wiki_overview_injected"] is False
        assert variables["plan_mode"] is True

    @pytest.mark.parametrize("auto_inject", [False, "false", "0"])
    def test_in_place_closeout_auto_inject_disabled_skips_pending(
        self,
        hub_db: HubDatabase,
        auto_inject: bool | str,
    ) -> None:
        session = self._make_session(hub_db, project_name="handoff-prep-closeout-optout")
        sv_mgr = SessionVariableManager(hub_db)
        sv_mgr.merge_variables(
            session.id,
            {
                "auto_inject_handoff": auto_inject,
                "session_summary": "stale",
                "full_session_summary": "stale",
                "handoff_summary_injectable": "stale",
                COMPACT_HANDOFF_MARKER_VARIABLE: "compact",
                "unlocked_tools": ["call_tool"],
                "loaded_skills": ["tasks"],
                "_agent_context_injected": True,
            },
        )
        session_view = MagicMock()
        session_view.id = session.id
        session_view.project_id = session.project_id
        session_view.summary_markdown = "## Next Steps\nContinue.\n"
        handler = self._make_handler(hub_db, session_view)

        apply_in_place_compact_context_loss(handler, session.id)

        variables = sv_mgr.get_variables(session.id)
        assert COMPACT_HANDOFF_INJECT_PENDING_VARIABLE not in variables
        assert variables["session_summary"] == ""
        assert variables["handoff_summary_injectable"] == ""
        assert COMPACT_HANDOFF_MARKER_VARIABLE not in variables
        assert variables["unlocked_tools"] == []
        assert variables["loaded_skills"] == []
        assert variables["_agent_context_injected"] is False
        assert variables["_agent_context_rehydrate_pending"] is True
        assert variables["wiki_overview_injected"] is False

    def test_in_place_closeout_refreshes_claimed_task_context(self, hub_db: HubDatabase) -> None:
        session = self._make_session(hub_db, project_name="handoff-prep-claimed")
        task = LocalTaskManager(hub_db).create_task(
            project_id=session.project_id,
            title="Refresh claimed context after compact",
            claimed_by_session_id=session.id,
            validation_criteria="Claimed task context is refreshed after Grok compact.",
        )
        session_view = MagicMock()
        session_view.id = session.id
        session_view.project_id = session.project_id
        session_view.summary_markdown = None
        handler = self._make_handler(hub_db, session_view)
        handler._task_manager = LocalTaskManager(hub_db)

        apply_in_place_compact_context_loss(handler, session.id)

        variables = SessionVariableManager(hub_db).get_variables(session.id)
        task_context = variables["task_context"]
        assert "## Claimed Tasks (Persisted)" in task_context
        assert f"#{task.seq_num}" in task_context
        assert "Refresh claimed context after compact" in task_context
        assert "still assigned to you" in task_context

    def test_in_place_closeout_rehydrates_agent_preamble(
        self, hub_db: HubDatabase, mock_dependencies: dict[str, Any]
    ) -> None:
        session = self._make_session(hub_db, project_name="handoff-prep-agent")
        sv_mgr = SessionVariableManager(hub_db)
        sv_mgr.merge_variables(
            session.id,
            {
                "_agent_type": "default",
                "_agent_context_injected": True,
                "_agent_context_rehydrate_pending": False,
                "plan_mode": True,
            },
        )
        session_view = MagicMock()
        session_view.id = session.id
        session_view.project_id = session.project_id
        session_view.summary_markdown = None
        handler = self._make_handler(hub_db, session_view)

        apply_in_place_compact_context_loss(handler, session.id)

        mock_dependencies["session_manager"].db = hub_db
        mock_dependencies["session_manager"].get.return_value = session_view
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.BEFORE_AGENT,
            metadata={"_platform_session_id": session.id},
        )
        agent_path = files("gobby.install.shared").joinpath("workflows/agents/default.yaml")
        default_agent = AgentDefinitionBody.model_validate(
            yaml.safe_load(agent_path.read_text(encoding="utf-8"))
        )
        response = HookResponse(decision="allow")
        with patch(
            "gobby.workflows.agent_resolver.resolve_agent",
            return_value=default_agent,
        ):
            handlers._inject_agent_instructions_if_needed(event, session.id, response)

        assert response.context is not None
        assert "## Role" in response.context
        assert default_agent.role is not None
        assert default_agent.role in response.context
        variables = sv_mgr.get_variables(session.id)
        assert variables["plan_mode"] is True


class TestCompactSelfContinuation:
    """compact_self continuation markers are consumed on the same session row."""

    def _make_db(self, hub_db: HubDatabase) -> HubDatabase:
        return hub_db

    def _make_precreated_session(
        self,
        db: HubDatabase,
        *,
        external_id: str = COMPACT_EXTERNAL_ID,
        source: str = "claude",
    ) -> Session:
        project = LocalProjectManager(db).create(
            name=f"handoff-{source}",
            repo_path="/some/dir",
        )
        return SessionManager(db).register(
            external_id=external_id,
            machine_id="21000000-0000-4000-8000-000000000001",
            source=source,
            project_id=project.id,
            terminal_context=dict(TERMINAL_CONTEXT),
        )

    def _fake_compact_self_consumer(self, scheduled: list[tuple[object, str]]) -> Any:
        def _consume(
            db: HubDatabase,
            *,
            pending_session_id: str | None,
            target_session: object,
            loop: object | None = None,
        ) -> bool:
            _ = loop
            prompt = None
            if pending_session_id:
                prompt = consume_compact_self_continuation_pending(db, pending_session_id)
            if prompt is None:
                return False
            scheduled.append((target_session, prompt))
            return True

        return _consume

    @pytest.mark.parametrize("cli_source", ["claude", "codex", "qwen", "droid"])
    def test_compact_start_with_pending_flag_clears_and_schedules_continuation(
        self, hub_db: HubDatabase, mock_dependencies: dict, cli_source: str
    ) -> None:
        """A self-initiated compact schedules one continuation when the pending flag is fresh."""
        db = self._make_db(hub_db)
        session = self._make_precreated_session(
            db,
            external_id=CLI_EXTERNAL_IDS[cli_source],
            source=cli_source,
        )
        mark_compact_self_continuation_pending(db, session.id)
        mock_dependencies["session_storage"].db = db
        mock_dependencies["session_storage"].get.return_value = session
        mock_dependencies["task_manager"].list_tasks.return_value = []

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id=session.id,
            source=cli_source,
            data={"source": "compact", "cwd": "/some/dir"},
            metadata={},
        )

        scheduled: list[tuple[object, str]] = []
        with (
            patch.object(handlers, "_activate_default_agent", return_value=None),
            patch(
                "gobby.hooks.event_handlers._session_start.consume_and_schedule_compact_self_continuation",
                side_effect=self._fake_compact_self_consumer(scheduled),
            ) as mock_schedule,
        ):
            response = handlers.handle_session_start(event)
            duplicate_response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        assert duplicate_response.decision == "allow"
        variables = SessionVariableManager(db).get_variables(session.id)
        assert COMPACT_SELF_CONTINUE_VARIABLE not in variables
        assert mock_schedule.call_count == 2
        assert scheduled == [(session, COMPACT_SELF_CONTINUE_PROMPT)]

    def test_grok_post_compact_consumes_once_and_schedules_same_session(
        self, hub_db: HubDatabase, mock_dependencies: dict
    ) -> None:
        """Grok PostCompact consumes one fresh marker and duplicate events are safe."""
        db = self._make_db(hub_db)
        session = self._make_precreated_session(
            db,
            external_id=CLI_EXTERNAL_IDS["grok"],
            source="grok",
        )
        mark_compact_self_continuation_pending(db, session.id)
        SessionVariableManager(db).merge_variables(
            session.id, {COMPACT_HANDOFF_MARKER_VARIABLE: "compact"}
        )
        mock_dependencies["session_manager"].db = db
        mock_dependencies["session_manager"].get.return_value = session
        mock_dependencies["session_manager"].update_context_usage.return_value = True

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.POST_COMPACT,
            session_id=session.external_id,
            source="grok",
            data={"source": "manual", "cwd": "/some/dir"},
            metadata={"_platform_session_id": session.id},
        )

        scheduled: list[tuple[object, str]] = []
        with patch(
            "gobby.hooks.event_handlers._misc.consume_and_schedule_compact_self_continuation",
            side_effect=self._fake_compact_self_consumer(scheduled),
        ) as mock_schedule:
            response = handlers.handle_post_compact(event)
            duplicate_response = handlers.handle_post_compact(event)

        assert response.decision == "allow"
        assert duplicate_response.decision == "allow"
        variables = SessionVariableManager(db).get_variables(session.id)
        assert COMPACT_SELF_CONTINUE_VARIABLE not in variables
        assert COMPACT_HANDOFF_MARKER_VARIABLE not in variables
        assert variables[COMPACT_HANDOFF_INJECT_PENDING_VARIABLE] is True
        assert mock_schedule.call_count == 2
        assert scheduled == [(session, COMPACT_SELF_CONTINUE_PROMPT)]

    def test_grok_post_compact_without_marker_schedules_nothing(
        self, hub_db: HubDatabase, mock_dependencies: dict
    ) -> None:
        db = self._make_db(hub_db)
        session = self._make_precreated_session(
            db,
            external_id=CLI_EXTERNAL_IDS["grok"],
            source="grok",
        )
        mock_dependencies["session_manager"].db = db
        mock_dependencies["session_manager"].get.return_value = session
        mock_dependencies["session_manager"].update_context_usage.return_value = True
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.POST_COMPACT,
            session_id=session.external_id,
            source="grok",
            data={"source": "manual", "cwd": "/some/dir"},
            metadata={"_platform_session_id": session.id},
        )

        with patch(
            "gobby.sessions.compact_continuation.schedule_compact_self_continuation"
        ) as mock_schedule:
            response = handlers.handle_post_compact(event)

        assert response.decision == "allow"
        mock_schedule.assert_not_called()

    @pytest.mark.parametrize(
        ("event_source", "include_platform_session"),
        [("codex", True), ("grok", False)],
        ids=["non-grok-event", "missing-platform-session"],
    )
    def test_non_grok_or_missing_post_compact_preserves_marker(
        self,
        hub_db: HubDatabase,
        mock_dependencies: dict,
        event_source: str,
        include_platform_session: bool,
    ) -> None:
        db = self._make_db(hub_db)
        session = self._make_precreated_session(
            db,
            external_id=CLI_EXTERNAL_IDS["grok"],
            source="grok",
        )
        mark_compact_self_continuation_pending(db, session.id)
        mock_dependencies["session_manager"].db = db
        mock_dependencies["session_manager"].get.return_value = session
        mock_dependencies["session_manager"].update_context_usage.return_value = True
        handlers = EventHandlers(**mock_dependencies)
        metadata = {"_platform_session_id": session.id} if include_platform_session else {}
        event = make_event(
            HookEventType.POST_COMPACT,
            session_id=session.external_id,
            source=event_source,
            data={"source": "manual", "cwd": "/some/dir"},
            metadata=metadata,
        )

        with patch(
            "gobby.sessions.compact_continuation.schedule_compact_self_continuation"
        ) as mock_schedule:
            response = handlers.handle_post_compact(event)

        assert response.decision == "allow"
        variables = SessionVariableManager(db).get_variables(session.id)
        assert COMPACT_SELF_CONTINUE_VARIABLE in variables
        mock_schedule.assert_not_called()

    def test_grok_post_compact_schedule_failure_restores_marker(
        self, hub_db: HubDatabase, mock_dependencies: dict
    ) -> None:
        db = self._make_db(hub_db)
        session = self._make_precreated_session(
            db,
            external_id=CLI_EXTERNAL_IDS["grok"],
            source="grok",
        )
        mark_compact_self_continuation_pending(db, session.id)
        variables_before = SessionVariableManager(db).get_variables(session.id)
        pending_before = variables_before[COMPACT_SELF_CONTINUE_VARIABLE]
        mock_dependencies["session_manager"].db = db
        mock_dependencies["session_manager"].get.return_value = session
        mock_dependencies["session_manager"].update_context_usage.return_value = True
        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.POST_COMPACT,
            session_id=session.external_id,
            source="grok",
            data={"source": "manual", "cwd": "/some/dir"},
            metadata={"_platform_session_id": session.id},
        )

        with patch(
            "gobby.sessions.compact_continuation.schedule_compact_self_continuation",
            return_value=False,
        ) as mock_schedule:
            response = handlers.handle_post_compact(event)

        assert response.decision == "allow"
        variables_after = SessionVariableManager(db).get_variables(session.id)
        assert variables_after[COMPACT_SELF_CONTINUE_VARIABLE] == pending_before
        mock_schedule.assert_called_once_with(
            session,
            COMPACT_SELF_CONTINUE_PROMPT,
            loop=mock_dependencies["session_coordinator"]._event_loop,
        )

    @pytest.mark.parametrize("cli_source", ["claude", "codex", "qwen", "droid", "grok"])
    def test_non_compact_start_preserves_pending_continuation(
        self, hub_db: HubDatabase, mock_dependencies: dict, cli_source: str
    ) -> None:
        """An unrelated session start cannot consume a compact continuation."""
        db = self._make_db(hub_db)
        session = self._make_precreated_session(
            db,
            external_id=CLI_EXTERNAL_IDS[cli_source],
            source=cli_source,
        )
        mark_compact_self_continuation_pending(db, session.id)
        mock_dependencies["session_storage"].db = db
        mock_dependencies["session_storage"].get.return_value = session
        mock_dependencies["task_manager"].list_tasks.return_value = []

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id=session.id,
            source=cli_source,
            data={"cwd": "/some/dir"},
            metadata={},
        )

        scheduled: list[tuple[object, str]] = []
        with (
            patch.object(handlers, "_activate_default_agent", return_value=None),
            patch(
                "gobby.hooks.event_handlers._session_start.consume_and_schedule_compact_self_continuation",
                side_effect=self._fake_compact_self_consumer(scheduled),
            ) as mock_schedule,
        ):
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        variables = SessionVariableManager(db).get_variables(session.id)
        assert COMPACT_SELF_CONTINUE_VARIABLE in variables
        mock_schedule.assert_not_called()
        assert scheduled == []

    def test_manual_compact_without_pending_flag_does_not_schedule_continuation(
        self, hub_db: HubDatabase, mock_dependencies: dict
    ) -> None:
        """A manual compact without the pending flag does not schedule continuation."""
        db = self._make_db(hub_db)
        session = self._make_precreated_session(db)
        mock_dependencies["session_storage"].db = db
        mock_dependencies["session_storage"].get.return_value = session
        mock_dependencies["task_manager"].list_tasks.return_value = []

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id=session.id,
            data={"source": "compact", "cwd": "/some/dir"},
            metadata={},
        )

        scheduled: list[tuple[object, str]] = []
        with (
            patch.object(handlers, "_activate_default_agent", return_value=None),
            patch(
                "gobby.hooks.event_handlers._session_start.consume_and_schedule_compact_self_continuation",
                side_effect=self._fake_compact_self_consumer(scheduled),
            ) as mock_schedule,
        ):
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        mock_schedule.assert_called_once()
        assert scheduled == []

    def test_stale_compact_pending_flag_clears_without_scheduling_continuation(
        self, hub_db: HubDatabase, mock_dependencies: dict
    ) -> None:
        """A stale self-compact flag is cleared without scheduling a continuation."""
        db = self._make_db(hub_db)
        session = self._make_precreated_session(db)
        stale_time = datetime.now(UTC) - timedelta(seconds=601)
        mark_compact_self_continuation_pending(db, session.id, now=stale_time)
        mock_dependencies["session_storage"].db = db
        mock_dependencies["session_storage"].get.return_value = session
        mock_dependencies["task_manager"].list_tasks.return_value = []

        handlers = EventHandlers(**mock_dependencies)
        event = make_event(
            HookEventType.SESSION_START,
            session_id=session.id,
            data={"source": "compact", "cwd": "/some/dir"},
            metadata={},
        )

        scheduled: list[tuple[object, str]] = []
        with (
            patch.object(handlers, "_activate_default_agent", return_value=None),
            patch(
                "gobby.hooks.event_handlers._session_start.consume_and_schedule_compact_self_continuation",
                side_effect=self._fake_compact_self_consumer(scheduled),
            ) as mock_schedule,
        ):
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        variables = SessionVariableManager(db).get_variables(session.id)
        assert COMPACT_SELF_CONTINUE_VARIABLE not in variables
        mock_schedule.assert_called_once()
        assert scheduled == []


def _clear_start_event(
    *,
    external_id: str,
    terminal_context: dict[str, Any] | None = None,
    project_id: str = "project-1",
    cli_source: str = "claude",
) -> Any:
    context = dict(TERMINAL_CONTEXT) if terminal_context is None else terminal_context
    return make_event(
        HookEventType.SESSION_START,
        session_id=external_id,
        source=cli_source,
        data={
            "source": "clear",
            "cwd": "/some/dir",
            "project_id": project_id,
            "terminal_context": context,
        },
        metadata={},
    )


def _clear_resolution(
    *,
    predecessor: Any | None = None,
    attempt_id: str | None = None,
    degrade_reason: str | None = None,
) -> SessionStartResolution:
    return SessionStartResolution(
        session=None,
        session_source="clear",
        clear_predecessor=predecessor,
        clear_attempt_id=attempt_id,
        clear_degrade_reason=degrade_reason,
    )


class TestSessionStartClearBinding:
    """SessionStart(source=clear) binds a distinct successor after an atomic take."""

    def _successor_row(self, session_id: str = "succ-clear-1") -> MagicMock:
        row = _make_row(session_id=session_id, status="active")
        row.parent_session_id = None
        row.summary_markdown = None
        return row

    def test_clear_skips_live_predecessor_remap_and_registers_new_row(
        self, mock_dependencies: dict[str, Any]
    ) -> None:
        predecessor = _make_row(session_id="pred-live-1", status="active")
        successor = self._successor_row()
        mock_dependencies["session_storage"].get.side_effect = (
            lambda session_id: predecessor
            if session_id == predecessor.id
            else successor
            if session_id == successor.id
            else None
        )
        mock_dependencies["session_manager"].register_session.return_value = successor.id
        mock_dependencies["task_manager"].list_tasks.return_value = []
        handlers = EventHandlers(**mock_dependencies)
        event = _clear_start_event(
            external_id="new-ext-after-clear",
            terminal_context={**TERMINAL_CONTEXT, "gobby_session_id": predecessor.id},
        )
        resolution = _clear_resolution(predecessor=predecessor, attempt_id="attempt-1")

        with (
            patch.object(handlers, "_handle_pre_created_session") as mock_precreated,
            patch.object(handlers, "_activate_default_agent", return_value=None),
            patch(
                "gobby.hooks.event_handlers._session_start.flow.resolve_session_start_identity",
                return_value=resolution,
            ),
            patch(
                "gobby.hooks.event_handlers._session_start.flow.take_clear_handoff_marker",
                return_value=True,
            ),
            patch("gobby.hooks.event_handlers._session_start.flow.seed_clear_handoff_variables"),
            patch("gobby.hooks.event_handlers._session_start.flow.preserve_task_claim_state"),
            patch(
                "gobby.hooks.event_handlers._session_start.flow.schedule_clear_self_continuation",
                return_value=True,
            ),
        ):
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        mock_precreated.assert_not_called()
        mock_dependencies["session_manager"].register_session.assert_called_once()
        mock_dependencies["session_manager"].update.assert_not_called()
        assert event.metadata["_platform_session_id"] == successor.id
        assert event.metadata["_platform_session_id"] != predecessor.id

    def test_clear_skips_inactive_precreated_early_return(
        self, mock_dependencies: dict[str, Any]
    ) -> None:
        expired = _make_row(session_id="expired-ext-row", status="expired")
        successor = self._successor_row("succ-after-expired")
        mock_dependencies["session_storage"].get.side_effect = (
            lambda session_id: expired
            if session_id in {expired.id, "stale-ext"}
            else successor
            if session_id == successor.id
            else None
        )
        mock_dependencies["session_manager"].register_session.return_value = successor.id
        mock_dependencies["task_manager"].list_tasks.return_value = []
        handlers = EventHandlers(**mock_dependencies)
        event = _clear_start_event(external_id="stale-ext")

        with (
            patch.object(handlers, "_activate_default_agent", return_value=None),
            patch(
                "gobby.hooks.event_handlers._session_start.flow.resolve_session_start_identity",
                return_value=_clear_resolution(),
            ),
            patch(
                "gobby.hooks.event_handlers._session_start.flow.take_clear_handoff_marker"
            ) as mock_take,
        ):
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        mock_dependencies["session_manager"].register_session.assert_called_once()
        mock_take.assert_not_called()
        assert event.metadata["_platform_session_id"] == successor.id

    def test_clear_resolver_receives_enriched_terminal_context(
        self, mock_dependencies: dict[str, Any]
    ) -> None:
        successor = self._successor_row("succ-enriched")
        mock_dependencies["session_storage"].get.side_effect = (
            lambda session_id: successor if session_id == successor.id else None
        )
        mock_dependencies["session_manager"].register_session.return_value = successor.id
        mock_dependencies["task_manager"].list_tasks.return_value = []
        handlers = EventHandlers(**mock_dependencies)
        raw_context = {**TERMINAL_CONTEXT, "parent_pid": os.getpid()}
        event = _clear_start_event(external_id="ext-enriched", terminal_context=raw_context)

        with (
            patch.object(handlers, "_activate_default_agent", return_value=None),
            patch(
                "gobby.hooks.event_handlers._session_start.flow.resolve_session_start_identity",
                return_value=_clear_resolution(),
            ) as mock_resolve,
            patch("gobby.hooks.event_handlers._session_start.flow.take_clear_handoff_marker"),
        ):
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        resolved_context = mock_resolve.call_args.kwargs["terminal_context"]
        assert resolved_context["parent_pid"] == os.getpid()
        assert resolved_context["parent_create_time"] > 0
        assert "parent_create_time" not in raw_context

    def test_clear_skips_web_chat_external_id_reuse(
        self, mock_dependencies: dict[str, Any]
    ) -> None:
        web_chat = _make_row(session_id="web-chat-1", status="active")
        web_chat.session_type = "web_chat"
        successor = self._successor_row("succ-not-web")
        mock_dependencies["session_storage"].find_by_external_id.return_value = web_chat
        mock_dependencies["session_storage"].get.side_effect = (
            lambda session_id: successor if session_id == successor.id else None
        )
        mock_dependencies["session_manager"].register_session.return_value = successor.id
        mock_dependencies["task_manager"].list_tasks.return_value = []
        handlers = EventHandlers(**mock_dependencies)
        event = _clear_start_event(external_id="shared-ext")

        with (
            patch.object(handlers, "_handle_pre_created_session") as mock_precreated,
            patch.object(handlers, "_activate_default_agent", return_value=None),
            patch(
                "gobby.hooks.event_handlers._session_start.flow.resolve_session_start_identity",
                return_value=_clear_resolution(),
            ),
        ):
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        mock_precreated.assert_not_called()
        mock_dependencies["session_manager"].register_session.assert_called_once()
        assert event.metadata["_platform_session_id"] == successor.id

    def test_losing_take_skips_seed_claims_and_schedule(
        self, mock_dependencies: dict[str, Any]
    ) -> None:
        predecessor = _make_row(session_id="pred-lost", status="active")
        predecessor.summary_markdown = CLEAR_HANDOFF
        successor = self._successor_row()
        mock_dependencies["session_storage"].get.side_effect = (
            lambda session_id: successor if session_id == successor.id else None
        )
        mock_dependencies["session_manager"].register_session.return_value = successor.id
        mock_dependencies["task_manager"].list_tasks.return_value = []
        handlers = EventHandlers(**mock_dependencies)
        event = _clear_start_event(external_id="loser-ext")

        with (
            patch.object(handlers, "_activate_default_agent", return_value=None),
            patch(
                "gobby.hooks.event_handlers._session_start.flow.resolve_session_start_identity",
                return_value=_clear_resolution(predecessor=predecessor, attempt_id="attempt-lost"),
            ),
            patch(
                "gobby.hooks.event_handlers._session_start.flow.take_clear_handoff_marker",
                return_value=False,
            ) as mock_take,
            patch(
                "gobby.hooks.event_handlers._session_start.flow.seed_clear_handoff_variables"
            ) as mock_seed,
            patch(
                "gobby.hooks.event_handlers._session_start.flow.preserve_task_claim_state"
            ) as mock_claims,
            patch(
                "gobby.hooks.event_handlers._session_start.flow.schedule_clear_self_continuation"
            ) as mock_schedule,
        ):
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        mock_take.assert_called_once()
        assert mock_take.call_args.args[1] == predecessor.id
        assert mock_take.call_args.kwargs["attempt_id"] == "attempt-lost"
        assert mock_take.call_args.kwargs["successor_id"] == successor.id
        mock_seed.assert_not_called()
        mock_claims.assert_not_called()
        mock_schedule.assert_not_called()
        mock_dependencies["session_manager"].register_session.assert_called_once()
        assert event.metadata["_platform_session_id"] == successor.id

    def test_winning_take_seeds_transfers_claims_and_schedules(
        self, mock_dependencies: dict[str, Any]
    ) -> None:
        predecessor = _make_row(session_id="pred-win", status="active")
        predecessor.summary_markdown = CLEAR_HANDOFF
        predecessor.seq_num = 88
        successor = self._successor_row()
        mock_dependencies["session_storage"].get.side_effect = (
            lambda session_id: successor if session_id == successor.id else None
        )
        mock_dependencies["session_manager"].register_session.return_value = successor.id
        mock_dependencies["session_manager"].db = MagicMock()
        mock_dependencies["task_manager"].list_tasks.return_value = []
        handlers = EventHandlers(**mock_dependencies)
        event = _clear_start_event(external_id="winner-ext")

        with (
            patch.object(handlers, "_activate_default_agent", return_value=None),
            patch(
                "gobby.hooks.event_handlers._session_start.flow.resolve_session_start_identity",
                return_value=_clear_resolution(predecessor=predecessor, attempt_id="attempt-win"),
            ),
            patch(
                "gobby.hooks.event_handlers._session_start.flow.take_clear_handoff_marker",
                return_value=True,
            ) as mock_take,
            patch(
                "gobby.hooks.event_handlers._session_start.flow.seed_clear_handoff_variables"
            ) as mock_seed,
            patch(
                "gobby.hooks.event_handlers._session_start.flow.preserve_task_claim_state"
            ) as mock_claims,
            patch(
                "gobby.hooks.event_handlers._session_start.flow.schedule_clear_self_continuation",
                return_value=True,
            ) as mock_schedule,
        ):
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        mock_take.assert_called_once()
        assert mock_take.call_args.kwargs["attempt_id"] == "attempt-win"
        assert mock_take.call_args.kwargs["successor_id"] == successor.id
        mock_seed.assert_called_once()
        assert mock_seed.call_args.args[1] == successor.id
        assert mock_seed.call_args.args[2] is predecessor
        mock_claims.assert_called_once()
        assert mock_claims.call_args.args[2] == successor.id
        assert mock_claims.call_args.args[3] == predecessor.id
        mock_schedule.assert_called_once()
        assert mock_schedule.call_args.args[0] is successor
        assert event.metadata["_platform_session_id"] == successor.id

    def test_seed_failure_after_winning_take_still_transfers_and_schedules(
        self, mock_dependencies: dict[str, Any]
    ) -> None:
        predecessor = _make_row(session_id="pred-seed-fail", status="active")
        predecessor.summary_markdown = CLEAR_HANDOFF
        successor = self._successor_row("succ-seed-fail")
        mock_dependencies["session_storage"].get.side_effect = (
            lambda session_id: successor if session_id == successor.id else None
        )
        mock_dependencies["session_manager"].register_session.return_value = successor.id
        mock_dependencies["session_manager"].db = MagicMock()
        mock_dependencies["task_manager"].list_tasks.return_value = []
        handlers = EventHandlers(**mock_dependencies)
        event = _clear_start_event(external_id="seed-fail-ext")

        with (
            patch.object(handlers, "_activate_default_agent", return_value=None),
            patch(
                "gobby.hooks.event_handlers._session_start.flow.resolve_session_start_identity",
                return_value=_clear_resolution(
                    predecessor=predecessor, attempt_id="attempt-seed-fail"
                ),
            ),
            patch(
                "gobby.hooks.event_handlers._session_start.flow.take_clear_handoff_marker",
                return_value=True,
            ),
            patch(
                "gobby.hooks.event_handlers._session_start.flow.seed_clear_handoff_variables",
                side_effect=RuntimeError("seed exploded"),
            ),
            patch(
                "gobby.hooks.event_handlers._session_start.flow.preserve_task_claim_state"
            ) as mock_claims,
            patch(
                "gobby.hooks.event_handlers._session_start.flow.schedule_clear_self_continuation",
                return_value=True,
            ) as mock_schedule,
        ):
            response = handlers.handle_session_start(event)

        assert response.decision == "allow"
        mock_claims.assert_called_once()
        assert mock_claims.call_args.args[2] == successor.id
        assert mock_claims.call_args.args[3] == predecessor.id
        mock_schedule.assert_called_once()
        assert mock_schedule.call_args.args[0] is successor
        mock_dependencies["session_manager"].register_session.assert_called_once()
        assert event.metadata["_platform_session_id"] == successor.id

    def test_concurrent_session_starts_produce_one_bound_successor(
        self, hub_db: HubDatabase
    ) -> None:
        project = LocalProjectManager(hub_db).create(
            name="clear-bind-concurrent",
            repo_path="/some/dir",
        )
        manager = SessionManager(hub_db)
        predecessor = manager.register(
            external_id="clear-pred-ext",
            machine_id=LOCAL_MACHINE_ID,
            source="claude",
            project_id=project.id,
            terminal_context=dict(TERMINAL_CONTEXT),
        )
        manager.update_summary(predecessor.id, summary_markdown=CLEAR_HANDOFF)
        loaded = manager.get(predecessor.id)
        assert loaded is not None
        predecessor = loaded
        stage_clear_attempt(
            hub_db,
            predecessor.id,
            attempt_id="attempt-concurrent",
            terminal_context=dict(TERMINAL_CONTEXT),
            chat_context=None,
        )
        handlers = EventHandlers(
            session_manager=cast(HookSessionManager, manager),
            task_manager=MagicMock(),
            session_coordinator=MagicMock(),
            get_machine_id=lambda: LOCAL_MACHINE_ID,
            resolve_project_id=lambda _project, _cwd: project.id,
            logger=logging.getLogger("test.clear-bind-concurrent"),
        )
        events = [
            _clear_start_event(
                external_id="clear-succ-ext-a",
                terminal_context={
                    **TERMINAL_CONTEXT,
                    "gobby_session_id": predecessor.id,
                },
                project_id=project.id,
            ),
            _clear_start_event(
                external_id="clear-succ-ext-b",
                terminal_context={
                    **TERMINAL_CONTEXT,
                    "gobby_session_id": predecessor.id,
                },
                project_id=project.id,
            ),
        ]
        scheduled: list[Any] = []

        def _schedule(session: Any, prompt: str, **_kwargs: Any) -> bool:
            scheduled.append((session.id, prompt))
            return True

        with (
            patch.object(handlers, "_activate_default_agent", return_value=None),
            patch(
                "gobby.hooks.event_handlers._session_start.flow.schedule_clear_self_continuation",
                side_effect=_schedule,
            ),
            patch("gobby.hooks.event_handlers._session_start.flow.preserve_task_claim_state"),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            responses = list(pool.map(handlers.handle_session_start, events))

        assert [response.decision for response in responses] == ["allow", "allow"]
        successor_ids = [event.metadata.get("_platform_session_id") for event in events]
        assert None not in successor_ids
        assert successor_ids[0] != successor_ids[1]
        assert predecessor.id not in successor_ids
        parents = [
            getattr(manager.get(session_id), "parent_session_id", None)
            for session_id in successor_ids
        ]
        assert parents.count(predecessor.id) == 1
        assert parents.count(None) == 1
        winner_id = successor_ids[parents.index(predecessor.id)]
        loser_id = successor_ids[0 if winner_id == successor_ids[1] else 1]
        winner_vars = SessionVariableManager(hub_db).get_variables(winner_id)
        loser_vars = SessionVariableManager(hub_db).get_variables(loser_id)
        assert winner_vars.get("clear_handoff_inject_pending") is True
        assert winner_vars.get("handoff_summary_injectable") == CLEAR_HANDOFF
        assert loser_vars.get("clear_handoff_inject_pending") is not True
        assert [session_id for session_id, _prompt in scheduled] == [winner_id]


class TestClearHandoffRuleTemplate:
    """Fossil session_start injection is gone; clear handoff is a turn_start rule."""

    def test_fossil_template_replaced_by_clear_handoff_rule(self) -> None:
        handoff_dir = files("gobby.install.shared").joinpath("workflows/rules/context-handoff")
        fossil = handoff_dir.joinpath("inject-previous-session-summary.yaml")
        clear = handoff_dir.joinpath("inject-clear-handoff.yaml")
        assert not fossil.is_file()
        assert clear.is_file()
        payload = yaml.safe_load(clear.read_text(encoding="utf-8"))
        rule = payload["rules"]["inject-clear-handoff-on-prompt"]
        assert rule["event"] == "turn_start"
        assert rule["priority"] == 11
        assert "clear_handoff_inject_pending" in rule["when"]
        template = next(
            effect["template"]
            for effect in rule["effects"]
            if effect.get("type") == "inject_context"
        )
        assert "Continuation Context (deliberate clear)" in template
        assert "Previous Session Context" not in template
        assert "Durable Tool-Call Evidence" not in template
        assert "Required Skill Reload" not in template
        pending = next(effect for effect in rule["effects"] if effect.get("type") == "set_variable")
        assert pending["variable"] == "clear_handoff_inject_pending"
        assert pending["value"] is False
