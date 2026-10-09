"""Tests for the HookManager coordinator."""

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import psutil
import pytest

from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import BootstrapConfig
from gobby.hooks.effect_deadline import BlockingEffectDeadline
from gobby.hooks.events import (
    ContextPart,
    HookEvent,
    HookEventType,
    HookResponse,
    SessionSource,
)
from gobby.hooks.hook_manager import HookManager
from gobby.hooks.project_context import resolve_hook_project_context
from gobby.hooks.session_lookup import NON_MATERIALIZING_EVENTS
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import reset_seeded_contexts, resolve_and_seed_contexts
from gobby.workflows.state_manager import SessionVariableManager
from tests._timing import wait_for_async_condition
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.unit

LOCAL_MACHINE_ID = "21000000-0000-4000-8000-000000000004"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", LOCAL_MACHINE_ID):
        yield


@pytest.fixture
def mock_daemon_client() -> Any:
    """Create a mock daemon client."""
    client = MagicMock()
    # Mock check_status to return (is_ready, message, status, error)
    client.check_status.return_value = (True, "Daemon ready", "ready", None)
    return client


@pytest.fixture
def hook_manager_with_mocks(
    isolated_checkout_factory: IsolatedCheckoutFactory,
    temp_dir: Path,
    mock_daemon_client: MagicMock,
    hub_db: HubDatabase,
) -> Iterator[HookManager]:
    """Create a HookManager with mocked dependencies."""
    db = hub_db

    # Create a test project
    project_mgr = LocalProjectManager(db)
    isolated_checkout_factory(project_mgr.db, "test-project", root=temp_dir)

    # The factory already wrote .gobby/project.json for project resolution.

    from gobby.config.extensions import HookExtensionsConfig, WebhooksConfig

    # Create config with disabled webhooks.
    test_config = DaemonConfig(
        hook_extensions=HookExtensionsConfig(
            webhooks=WebhooksConfig(enabled=False),
        ),
    )

    with (
        patch("gobby.hooks.factory.DaemonClient") as MockDaemonClient,
        patch("gobby.hooks.webhooks.httpx.AsyncClient") as MockHttpClient,
    ):
        MockDaemonClient.return_value = mock_daemon_client
        MockHttpClient.return_value = MagicMock()

        manager = HookManager(
            daemon_host="localhost",
            daemon_port=60887,
            config=test_config,
            database=db,
        )

        # Pre-warm the daemon status cache
        manager._health_monitor._cached_daemon_is_ready = True
        manager._health_monitor._cached_daemon_status = "ready"

        yield manager

        # Cleanup: the database itself is owned by the hub_db fixture.
        manager.shutdown()


@pytest.fixture
def sample_session_start_event(temp_dir: Path) -> HookEvent:
    """Create a sample session start event."""
    return HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id="test-external-id-123",
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            "source": "startup",
            "cwd": str(temp_dir),
            "transcript_path": str(temp_dir / "transcript.jsonl"),
        },
        machine_id="21000000-0000-4000-8000-000000000004",
    )


class TestHookManagerInit:
    """Tests for HookManager initialization."""

    def test_init_creates_subsystems(self, hook_manager_with_mocks: HookManager) -> None:
        """Test that initialization creates all subsystems."""
        manager = hook_manager_with_mocks

        assert manager._daemon_client is not None
        assert manager._transcript_processor is not None
        assert manager._session_manager is not None
        assert manager._database is not None

    def test_init_has_skill_manager(self, hook_manager_with_mocks: HookManager) -> None:
        """Test that HookManager has a skill_manager."""
        manager = hook_manager_with_mocks

        # Verify skill_manager exists on HookManager
        assert hasattr(manager, "_skill_manager")
        assert manager._skill_manager is not None

    def test_init_sets_daemon_url(self, hook_manager_with_mocks: HookManager) -> None:
        """Test that daemon URL is set correctly."""
        manager = hook_manager_with_mocks
        assert manager.daemon_url == "http://localhost:60887"

    def test_init_creates_event_handlers(self, hook_manager_with_mocks: HookManager) -> None:
        """Test that event handlers are created."""
        manager = hook_manager_with_mocks
        handler_map = manager._event_handlers.get_handler_map()

        # Check key event types have handlers
        assert HookEventType.SESSION_START in handler_map
        assert HookEventType.SESSION_END in handler_map
        assert HookEventType.BEFORE_AGENT in handler_map
        assert HookEventType.AFTER_AGENT in handler_map
        assert HookEventType.BEFORE_TOOL in handler_map
        assert HookEventType.AFTER_TOOL in handler_map


class TestHookManagerHandle:
    """Tests for the handle() method."""

    def test_handle_returns_hook_response(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
    ) -> None:
        """Test that handle returns a HookResponse."""
        response = hook_manager_with_mocks.handle(sample_session_start_event)

        assert isinstance(response, HookResponse)
        assert response.decision == "allow"

    def test_handle_daemon_not_ready(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
    ) -> None:
        """Test handling when daemon is not ready."""
        from unittest.mock import patch

        from gobby.hooks.health_gate import DaemonNotReadyError

        manager = hook_manager_with_mocks

        # Simulate daemon not ready by mocking HealthMonitor's get_cached_status
        # Also mock check_now() since critical hooks (like SESSION_START) retry
        with (
            patch.object(
                manager._health_monitor,
                "get_cached_status",
                return_value=(False, None, "not_running", "Connection refused"),
            ),
            patch.object(
                manager._health_monitor,
                "check_now",
                return_value=False,  # Retries also fail
            ),
            pytest.raises(DaemonNotReadyError) as excinfo,
        ):
            manager.handle(sample_session_start_event)

        # Retained for replay, not fail-open
        assert excinfo.value.daemon_status == "not_running"
        assert excinfo.value.reason == "Connection refused"

    def test_handle_daemon_recovers_after_retry(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
    ) -> None:
        """Test daemon recovery after retry for critical hooks."""
        from unittest.mock import MagicMock, patch

        manager = hook_manager_with_mocks

        # Create a mock that returns True on the second call (simulating recovery)
        check_now_mock = MagicMock(side_effect=[False, True])  # Fails first, then succeeds

        # Simulate daemon not ready initially, but check_now recovers
        with (
            patch.object(
                manager._health_monitor,
                "get_cached_status",
                return_value=(False, None, "not_running", "Connection refused"),
            ),
            patch.object(
                manager._health_monitor,
                "check_now",
                check_now_mock,
            ),
        ):
            response = manager.handle(sample_session_start_event)

        # Should succeed after retry (no reason means it proceeded to handler)
        assert response.decision == "allow"
        # check_now should have been called (retry logic was triggered)
        assert check_now_mock.call_count >= 1

    def test_handle_unknown_event_type(self, hook_manager_with_mocks: HookManager) -> None:
        """Test handling unknown event type fails open."""
        from unittest.mock import patch

        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.NOTIFICATION,
            session_id="test",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={},
            machine_id=LOCAL_MACHINE_ID,
        )

        # Mock the event handlers to return None for any event type
        with patch.object(manager._event_handlers, "get_handler", return_value=None):
            response = manager.handle(event)

        # Should fail open
        assert response.decision == "allow"

    @pytest.mark.parametrize("event_type", [HookEventType.STOP, HookEventType.SESSION_END])
    def test_process_bound_native_child_terminal_hook_skips_parent_lifecycle(
        self,
        hook_manager_with_mocks: HookManager,
        temp_dir: Path,
        event_type: HookEventType,
    ) -> None:
        manager = hook_manager_with_mocks
        event = HookEvent(
            event_type=event_type,
            session_id="native-child",
            source=SessionSource.GROK,
            timestamp=datetime.now(UTC),
            data={"cwd": str(temp_dir)},
            machine_id=LOCAL_MACHINE_ID,
        )

        def bind_native_child(candidate: HookEvent, **_kwargs: Any) -> str:
            candidate.metadata["_native_subagent_binding"] = True
            candidate.metadata["_platform_session_id"] = "spawned-parent"
            return "spawned-parent"

        with (
            patch.object(manager._session_lookup, "resolve", side_effect=bind_native_child),
            patch(
                "gobby.hooks.hook_manager.validate_managed_agent_hook",
                side_effect=AssertionError("native child reached managed terminal ingress"),
            ) as validate_ingress,
        ):
            response = manager.handle(event)

        assert response.decision == "allow"
        validate_ingress.assert_not_called()

    def test_non_session_end_hook_revives_expired_terminal_session(
        self,
        hook_manager_with_mocks: HookManager,
        temp_dir: Path,
    ) -> None:
        """Hook activity repairs false-expired terminal sessions before handling."""
        manager = hook_manager_with_mocks
        project_id = manager._resolve_project_id(None, str(temp_dir))
        session = manager.session_manager.register(
            external_id="codex-ext-1",
            machine_id="21000000-0000-4000-8000-000000000004",
            source="codex",
            project_id=project_id,
            transcript_path=str(temp_dir / "codex.jsonl"),
            terminal_context={"parent_pid": 99999},
        )
        manager.session_manager.update_status(session.id, "expired")

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="codex-ext-1",
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={"cwd": str(temp_dir), "tool_name": "shell", "tool_input": {}},
            machine_id="21000000-0000-4000-8000-000000000004",
            cwd=str(temp_dir),
        )

        status_at_reconciliation: list[str] = []

        def observe_reconciliation(*_args: Any, **_kwargs: Any) -> None:
            current = manager.session_manager.get(session.id)
            assert current is not None
            status_at_reconciliation.append(current.status)

        with patch(
            "gobby.hooks.hook_manager.reconcile_session_activation",
            side_effect=observe_reconciliation,
        ):
            response = manager.handle(event)

        assert response.decision == "allow"
        assert status_at_reconciliation == ["active"]
        revived = manager.session_manager.get(session.id)
        assert revived is not None
        assert revived.status == "active"

    def test_delayed_after_tool_keeps_superseded_terminal_session_expired(
        self,
        hook_manager_with_mocks: HookManager,
        temp_dir: Path,
    ) -> None:
        """Historical PostToolUse processing cannot reclaim a reused tmux pane."""
        manager = hook_manager_with_mocks
        project_id = manager._resolve_project_id(None, str(temp_dir))
        terminal_context = {
            "tmux_pane": "%154",
            "tmux_socket_path": "/tmp/tmux-501/default",
        }
        older = manager.session_manager.register(
            external_id="claude-stale-external",
            machine_id="21000000-0000-4000-8000-000000000004",
            source="claude",
            project_id=project_id,
            transcript_path=str(temp_dir / "stale.jsonl"),
            terminal_context=terminal_context,
        )
        manager.session_manager.update_status(older.id, "expired")
        newer = manager.session_manager.register(
            external_id="claude-live-external",
            machine_id="21000000-0000-4000-8000-000000000004",
            source="claude",
            project_id=project_id,
            transcript_path=str(temp_dir / "live.jsonl"),
            terminal_context=terminal_context,
        )
        event = HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id="claude-stale-external",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={
                "cwd": str(temp_dir),
                "tool_name": "Read",
                "tool_input": {},
                "tool_output": {},
            },
            machine_id="21000000-0000-4000-8000-000000000004",
            cwd=str(temp_dir),
        )

        response = manager.handle(event)

        assert response.decision == "allow"
        assert event.metadata["_platform_session_id"] == older.id
        historical = manager.session_manager.get(older.id)
        live = manager.session_manager.get(newer.id)
        assert historical is not None
        assert live is not None
        assert historical.status == "expired"
        assert live.status == "active"


class TestHookManagerSessionStart:
    """Tests for session start handling."""

    def test_startup_session_start_defers_session_registration(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
    ) -> None:
        """A startup SessionStart leaves row creation to first activity."""
        response = hook_manager_with_mocks.handle(sample_session_start_event)

        assert response.decision == "allow"
        assert response.metadata == {}
        assert "_platform_session_id" not in sample_session_start_event.metadata
        assert (
            hook_manager_with_mocks._session_manager.get_session_id(
                sample_session_start_event.session_id,
                sample_session_start_event.source.value,
            )
            is None
        )

    def test_session_start_returns_response(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
    ) -> None:
        """A deferred startup returns no context before first activity."""
        response = hook_manager_with_mocks.handle(sample_session_start_event)

        assert response.decision == "allow"
        assert response.system_message is None
        assert response.context is None

    def test_session_resume_no_handoff_message(
        self,
        hook_manager_with_mocks: HookManager,
        temp_dir: Path,
    ) -> None:
        """Test that resume source doesn't show 'Context restored' system_message.

        Parent session finding only happens on source='clear' (handoff scenario).
        On resume we get basic session info only, no parent context.
        """
        # Create a resume event (source="resume" means continuing same session)
        resume_event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id="test-resume-session-123",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={
                "source": "resume",  # Key: this is a resume, not startup
                "cwd": str(temp_dir),
                "transcript_path": str(temp_dir / "transcript.jsonl"),
            },
            machine_id="21000000-0000-4000-8000-000000000004",
        )

        response = hook_manager_with_mocks.handle(resume_event)

        # Should be allowed
        assert response.decision == "allow"

        # Should have session ID banner but NOT "Context restored" message
        # Parent finding only runs on source='clear'
        assert response.system_message is not None
        assert "Gobby Session ID:" in response.system_message
        assert "Context restored" not in (response.system_message or "")


class TestHookManagerSessionEnd:
    """Tests for session end handling."""

    def test_session_end_allows(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
        temp_dir: Path,
    ) -> None:
        """Test that session end is allowed."""
        # First start a session
        hook_manager_with_mocks.handle(sample_session_start_event)

        # Create transcript file in temp directory
        transcript_path = temp_dir / "transcript.jsonl"
        transcript_path.touch()

        # Then end it
        end_event = HookEvent(
            event_type=HookEventType.SESSION_END,
            session_id="test-external-id-123",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"transcript_path": str(transcript_path)},
            machine_id="21000000-0000-4000-8000-000000000004",
        )

        response = hook_manager_with_mocks.handle(end_event)
        assert response.decision == "allow"

    @pytest.mark.integration
    def test_session_end_auto_links_commits(
        self,
        hook_manager_with_mocks: HookManager,
        temp_dir: Path,
    ) -> None:
        """Test that session end auto-links commits made during session."""
        from gobby.storage.session_models import Session
        from gobby.tasks.commits import AutoLinkResult

        # Create transcript file in temp directory
        transcript_path = temp_dir / "transcript.jsonl"
        transcript_path.touch()

        # Mock session storage to return a session with created_at
        mock_session = Session(
            id="test-session-id",
            external_id="test-external-id-123",
            machine_id="21000000-0000-4000-8000-000000000004",
            source="claude",
            project_id="test-project-id",
            title="Test Session",
            status="active",
            transcript_path=None,
            summary_path=None,
            summary_markdown=None,
            git_branch=None,
            parent_session_id=None,
            created_at=datetime(2026, 1, 4, tzinfo=UTC),
            updated_at=datetime(2026, 1, 4, tzinfo=UTC),
        )

        mock_project = MagicMock()
        mock_project.name = "session-project"
        # Mock auto_link_commits to verify it's called
        mock_result = AutoLinkResult(
            linked_tasks={"gt-123abc": ["abc1234", "def5678"]},
            total_linked=2,
            skipped=1,
        )

        with (
            patch.object(
                hook_manager_with_mocks._session_manager,
                "get_session_id",
                return_value="test-session-id",
            ),
            patch.object(
                hook_manager_with_mocks._session_manager, "get", return_value=mock_session
            ),
            patch("gobby.hooks.session_end_auto_link.LocalProjectManager") as project_manager_cls,
            patch(
                "gobby.hooks.session_end_auto_link.auto_link_commits", return_value=mock_result
            ) as mock_auto_link,
        ):
            project_manager_cls.return_value.get.return_value = mock_project
            end_event = HookEvent(
                event_type=HookEventType.SESSION_END,
                session_id="test-external-id-123",
                source=SessionSource.CLAUDE,
                timestamp=datetime.now(UTC),
                data={"transcript_path": str(transcript_path), "cwd": str(temp_dir)},
                machine_id="21000000-0000-4000-8000-000000000004",
                metadata={"_platform_session_id": "test-session-id"},
            )

            response = hook_manager_with_mocks.handle(end_event)
            hook_manager_with_mocks._session_end_auto_link_worker.shutdown()

            assert response.decision == "allow"

            # Verify auto_link_commits was called
            mock_auto_link.assert_called_once()
            call_kwargs = mock_auto_link.call_args.kwargs
            assert "task_manager" in call_kwargs
            assert call_kwargs["since"] == "2026-01-04T00:00:00+00:00"
            assert call_kwargs["cwd"] == str(temp_dir)
            assert call_kwargs["project_id"] == mock_session.project_id
            assert call_kwargs["project_name"] == "session-project"


class TestHookManagerBeforeAgent:
    """Tests for before agent (user prompt submit) handling."""

    def test_first_user_prompt_submit_registers_session(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
        temp_dir: Path,
    ) -> None:
        manager = hook_manager_with_mocks

        start_response = manager.handle(sample_session_start_event)

        assert start_response.decision == "allow"
        assert start_response.system_message is None
        assert "_platform_session_id" not in sample_session_start_event.metadata
        assert (
            manager._session_manager.get_session_id(
                sample_session_start_event.session_id,
                sample_session_start_event.source.value,
            )
            is None
        )

        first_prompt = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=sample_session_start_event.session_id,
            source=sample_session_start_event.source,
            timestamp=datetime.now(UTC),
            data={
                "prompt": "Help me write a function",
                "cwd": str(temp_dir),
                "transcript_path": str(temp_dir / "transcript.jsonl"),
            },
            machine_id=LOCAL_MACHINE_ID,
        )

        with patch.object(
            manager._event_handlers,
            "_activate_materialized_session",
            wraps=manager._event_handlers._activate_materialized_session,
        ) as activate:
            response = manager.handle(first_prompt)

        assert response.decision == "allow"
        assert response.system_message is not None
        assert "Gobby Session ID:" in response.system_message
        activate.assert_called_once()
        assert first_prompt.metadata.get("_platform_session_id")

    def test_first_codex_prompt_persists_terminal_identity(
        self,
        hook_manager_with_mocks: HookManager,
        temp_dir: Path,
    ) -> None:
        manager = hook_manager_with_mocks
        external_id = "codex-first-prompt-terminal-context"
        start = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id=external_id,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={"source": "startup", "cwd": str(temp_dir)},
            machine_id=LOCAL_MACHINE_ID,
        )
        assert manager.handle(start).decision == "allow"
        assert "_platform_session_id" not in start.metadata

        terminal_context = {
            "parent_pid": 30769,
            "tty": "/dev/ttys091",
            "tmux_pane": "%91",
            "tmux_socket_path": "/tmp/tmux-501/default",
            "tmux_window_id": "@42",
            "tmux_session": "gobby",
            "term_program": "tmux",
        }
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

        first_prompt = CodexHooksAdapter().translate_to_hook_event(
            {
                "hook_type": "UserPromptSubmit",
                "input_data": {
                    "session_id": external_id,
                    "prompt": "Fix the hook regression",
                    "cwd": str(temp_dir),
                    "terminal_context": terminal_context,
                    "effort": {"level": " high "},
                    "machine_id": LOCAL_MACHINE_ID,
                },
                "source": "codex",
            }
        )
        assert first_prompt is not None
        assert first_prompt.data["effort"] == "high"

        # Parent identity is read from the live process table, so pin it rather than
        # depending on this machine having PID 30769 running codex.
        with (
            patch(
                "gobby.hooks.event_handlers._session_start.schedule_tmux_window_rename"
            ) as initial_rename,
            patch(
                "gobby.sessions.tmux_window_naming.schedule_tmux_window_rename"
            ) as promoted_rename,
            patch(
                "gobby.hooks.terminal_context.psutil.Process",
                return_value=SimpleNamespace(
                    create_time=lambda: 1784592177.0,
                    name=lambda: "codex",
                    cmdline=lambda: ["codex", "--yolo"],
                ),
            ),
        ):
            response = manager.handle(first_prompt)

        assert response.decision == "allow"
        session_id = first_prompt.metadata["_platform_session_id"]
        session = manager._session_manager.get(session_id)
        assert session is not None
        # The first prompt never titles the session; the provisional title stands.
        assert session.title == f"test-project#{session.seq_num}: Codex"
        assert session.title_source == "provisional"
        assert session.reasoning_effort == "high"
        assert session.terminal_context is not None
        for key, value in {**terminal_context, "cwd": str(temp_dir)}.items():
            assert session.terminal_context[key] == value
        assert session.terminal_context["parent_name"] == "codex"
        assert isinstance(session.terminal_context["parent_create_time"], float)
        initial_rename.assert_called_once()
        promoted_rename.assert_not_called()

    def test_shared_codex_app_server_seats_keep_distinct_identities(
        self,
        hook_manager_with_mocks: HookManager,
        temp_dir: Path,
    ) -> None:
        """Two seats hooked through one managed app-server bind to their own TUIs.

        Codex 0.157 fires every seat's hooks from one ``codex app-server
        --managed-daemon`` process, so ghook reports the daemon's pid and inherited
        GOBBY_TERMINAL_ID for both seats (#22929). Each session must instead record
        the ``codex resume <thread>`` process in its own pane, and the second seat's
        start must not expire the first.
        """
        from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
        from gobby.hooks.terminal_context import clear_codex_seat_index

        manager = hook_manager_with_mocks
        host_pid = 93395
        host_context = {
            "parent_pid": host_pid,
            "tty": None,
            "tmux_pane": None,
            "tmux_socket_path": None,
            "tmux_window_id": None,
            "tmux_session": None,
            "term_program": None,
            "gobby_session_id": None,
            "gobby_agent_run_id": None,
            "gobby_terminal_id": "358981a1-df59-43bd-a04e-6cd698238442",
            "gobby_pane_ref": None,
        }
        seats = {
            "01a0d6a4-4800-7d90-a5d8-3b751ad44281": (
                22512,
                "/dev/ttys001",
                "99b23bfd-3aa7-4ebc-9037-071aefb56955",
            ),
            "01a0d723-8589-75d2-adb9-9263ee2bd8bf": (
                23170,
                "/dev/ttys003",
                "eb81fd00-e2f6-47f2-a0e1-448159a1aeec",
            ),
        }

        def process(pid: int, cmdline: list[str], create_time: float) -> MagicMock:
            mock = MagicMock()
            mock.pid = pid
            mock.info = {"name": "codex"}
            mock.name.return_value = "codex"
            mock.cmdline.return_value = cmdline
            mock.create_time.return_value = create_time
            return mock

        host = process(
            host_pid, ["codex", "app-server", "--listen", "unix://", "--managed-daemon"], 50.0
        )
        table = [host]
        for offset, (thread_id, (pid, tty, terminal_id)) in enumerate(seats.items()):
            seat = process(pid, ["codex", "resume", thread_id, "--yolo"], 100.0 + offset)
            seat.terminal.return_value = tty
            seat.environ.return_value = {"GOBBY_TERMINAL_ID": terminal_id}
            table.append(seat)

        def lookup(pid: int) -> MagicMock:
            for candidate in table:
                if candidate.pid == pid:
                    return candidate
            raise psutil.NoSuchProcess(pid)

        session_ids: dict[str, str] = {}
        clear_codex_seat_index()
        try:
            with (
                patch("gobby.hooks.event_handlers._session_start.schedule_tmux_window_rename"),
                patch("gobby.sessions.tmux_window_naming.schedule_tmux_window_rename"),
                patch("gobby.hooks.terminal_context.psutil.Process", side_effect=lookup),
                patch(
                    "gobby.hooks.terminal_context.psutil.process_iter",
                    side_effect=lambda **_: list(table),
                ),
            ):
                for thread_id in seats:
                    start = HookEvent(
                        event_type=HookEventType.SESSION_START,
                        session_id=thread_id,
                        source=SessionSource.CODEX,
                        timestamp=datetime.now(UTC),
                        data={"source": "startup", "cwd": str(temp_dir)},
                        machine_id=LOCAL_MACHINE_ID,
                    )
                    assert manager.handle(start).decision == "allow"
                    prompt = CodexHooksAdapter().translate_to_hook_event(
                        {
                            "hook_type": "UserPromptSubmit",
                            "input_data": {
                                "session_id": thread_id,
                                "prompt": "Which pane am I in?",
                                "cwd": str(temp_dir),
                                "terminal_context": dict(host_context),
                                "machine_id": LOCAL_MACHINE_ID,
                            },
                            "source": "codex",
                        }
                    )
                    assert prompt is not None
                    assert manager.handle(prompt).decision == "allow"
                    session_ids[thread_id] = prompt.metadata["_platform_session_id"]
        finally:
            clear_codex_seat_index()

        assert len(set(session_ids.values())) == 2
        for thread_id, (pid, tty, terminal_id) in seats.items():
            session = manager._session_manager.get(session_ids[thread_id])
            assert session is not None
            assert session.status == "active"
            assert session.terminal_context is not None
            assert session.terminal_context["cwd"] == str(temp_dir)
            assert session.terminal_context["parent_pid"] == pid
            assert session.terminal_context["parent_name"] == "codex"
            assert session.terminal_context["tty"] == tty
            assert session.terminal_context["gobby_terminal_id"] == terminal_id

    def test_before_agent_allows(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
    ) -> None:
        """Test that before agent is allowed."""
        # Start session first
        hook_manager_with_mocks.handle(sample_session_start_event)

        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id="test-external-id-123",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"prompt": "Help me write a function"},
            machine_id="21000000-0000-4000-8000-000000000004",
        )

        response = hook_manager_with_mocks.handle(event)
        assert response.decision == "allow"

    def test_ordinary_spawned_session_keeps_provisional_title_after_first_prompt(
        self,
        hook_manager_with_mocks: HookManager,
        temp_dir: Path,
    ) -> None:
        manager = hook_manager_with_mocks
        project = LocalProjectManager(manager._session_manager.db).get_by_name("test-project")
        assert project is not None
        child_id = manager._session_manager.register_session(
            external_id="spawned-child-external",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=project.id,
            project_path=str(temp_dir),
            agent_depth=1,
        )
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id="spawned-child-external",
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={"prompt": "Investigate spawned worker behavior", "cwd": str(temp_dir)},
            machine_id=LOCAL_MACHINE_ID,
        )

        response = manager.handle(event)

        assert response.decision == "allow"
        child = manager._session_manager.get(child_id)
        assert child is not None
        assert child.agent_depth == 1
        assert child.title == f"test-project#{child.seq_num}: Codex"
        assert child.title_source == "provisional"

    def test_internal_native_subagent_prompt_does_not_title_parent(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
    ) -> None:
        hook_manager_with_mocks.handle(sample_session_start_event)
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=sample_session_start_event.session_id,
            source=sample_session_start_event.source,
            timestamp=datetime.now(UTC),
            data={"prompt": "Internal delegated investigation"},
            metadata={"_native_subagent_binding": True},
            machine_id=LOCAL_MACHINE_ID,
        )

        response = hook_manager_with_mocks.handle(event)

        assert response.decision == "allow"
        parent = hook_manager_with_mocks._session_manager.get(
            event.metadata["_platform_session_id"]
        )
        assert parent is not None
        assert parent.title is not None
        assert parent.title.endswith(f"#{parent.seq_num}: Claude")
        assert parent.title_source == "provisional"

    def test_spawn_only_fallback_definition_does_not_log_persona_injection_error(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        hook_manager_with_mocks.handle(sample_session_start_event)
        event = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=sample_session_start_event.session_id,
            source=sample_session_start_event.source,
            timestamp=datetime.now(UTC),
            data={"prompt": "Check fallback persona behavior"},
            machine_id=LOCAL_MACHINE_ID,
        )

        with caplog.at_level(logging.ERROR, logger="gobby.hooks"):
            response = hook_manager_with_mocks.handle(event)

        assert response.decision == "allow"
        assert "does not support the 'persona' surface" not in caplog.text


class TestHookManagerToolEvents:
    """Tests for tool event handling."""

    def test_before_tool_allows(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
    ) -> None:
        """Test that before tool use is allowed."""
        hook_manager_with_mocks.handle(sample_session_start_event)

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="test-external-id-123",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "bash", "tool_input": {"command": "ls"}},
            machine_id="21000000-0000-4000-8000-000000000004",
        )

        response = hook_manager_with_mocks.handle(event)
        assert response.decision == "allow"

    def test_after_tool_allows(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
    ) -> None:
        """Test that after tool use is allowed."""
        hook_manager_with_mocks.handle(sample_session_start_event)

        event = HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id="test-external-id-123",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "Read", "tool_output": "file1.txt\nfile2.txt"},
            machine_id="21000000-0000-4000-8000-000000000004",
        )

        response = hook_manager_with_mocks.handle(event)
        assert response.decision == "allow"


class TestHookManagerShutdown:
    """Tests for HookManager shutdown."""

    def test_shutdown_stops_health_check(self, hook_manager_with_mocks: HookManager) -> None:
        """Test that shutdown stops health check monitoring."""
        manager = hook_manager_with_mocks

        # Should have a health monitor with timer running or already shutdown
        assert (
            manager._health_monitor._health_check_timer is not None
            or manager._health_monitor._is_shutdown
        )

        manager.shutdown()

        # Should be marked as shutdown in the health monitor
        assert manager._health_monitor._is_shutdown is True


class TestHookManagerGetEventHandler:
    """Tests for event handler lookup."""

    def test_get_handler_for_known_event(self, hook_manager_with_mocks: HookManager) -> None:
        """Test getting handler for known event type."""
        handler = hook_manager_with_mocks._get_event_handler(HookEventType.SESSION_START)
        assert handler is not None
        assert callable(handler)

    def test_get_handler_for_all_event_types(self, hook_manager_with_mocks: HookManager) -> None:
        """Test that all event types in map have handlers."""
        handler_map = hook_manager_with_mocks._event_handlers.get_handler_map()
        for event_type in handler_map:
            handler = hook_manager_with_mocks._get_event_handler(event_type)
            assert handler is not None


class TestHookManagerMachineId:
    """Tests for machine ID functionality."""

    def test_get_machine_id(self, hook_manager_with_mocks: HookManager) -> None:
        """Test getting machine ID returns a string."""
        result = hook_manager_with_mocks.get_machine_id()
        # Should return a string (either from cache, config, or generated)
        assert result is None or isinstance(result, str)


class TestHookManagerCachedDaemonStatus:
    """Tests for cached daemon status."""

    def test_get_cached_daemon_status(self, hook_manager_with_mocks: HookManager) -> None:
        """Test getting cached daemon status."""
        manager = hook_manager_with_mocks
        # The startup health check runs on a timer thread; let it finish before
        # seeding the cache so it cannot overwrite the seeded values.
        startup_check = manager._health_monitor._health_check_timer
        manager._health_monitor.stop()
        if startup_check is not None:
            startup_check.join(timeout=5)

        # Set cached values on the health monitor (delegation target)
        manager._health_monitor._cached_daemon_is_ready = True
        manager._health_monitor._cached_daemon_message = "Ready"
        manager._health_monitor._cached_daemon_status = "healthy"
        manager._health_monitor._cached_daemon_error = None

        is_ready, message, status, error = manager._get_cached_daemon_status()

        assert is_ready is True
        assert message == "Ready"
        assert status == "healthy"
        assert error is None


class TestHookManagerConfigLoadError:
    """Tests for config loading error handling."""

    def test_init_handles_config_load_error(
        self,
        temp_dir: Path,
        mock_daemon_client: MagicMock,
        hub_db: HubDatabase,
    ) -> None:
        """Test that init handles config loading errors gracefully."""
        fallback_bootstrap = BootstrapConfig()
        with (
            patch("gobby.hooks.factory.DaemonClient") as MockDaemonClient,
            patch(
                "gobby.hooks.factory.load_bootstrap",
                side_effect=[Exception("Config load failed"), fallback_bootstrap],
            ) as mock_load_bootstrap,
        ):
            MockDaemonClient.return_value = mock_daemon_client

            # Should not raise - handles error gracefully
            manager = HookManager(
                daemon_host="localhost",
                daemon_port=60887,
                config=None,  # Force config loading
                database=hub_db,
            )

            # Manager should still be created with defaults
            assert manager is not None
            assert isinstance(manager._config, DaemonConfig)
            assert mock_load_bootstrap.call_count == 2

            manager.shutdown()

    def test_init_uses_default_health_check_interval_without_config(
        self,
        temp_dir: Path,
        mock_daemon_client: MagicMock,
        hub_db: HubDatabase,
    ) -> None:
        """Test that init uses default health check interval when config is None."""
        fallback_bootstrap = BootstrapConfig()
        with (
            patch("gobby.hooks.factory.DaemonClient") as MockDaemonClient,
            patch(
                "gobby.hooks.factory.load_bootstrap",
                side_effect=[Exception("Config load failed"), fallback_bootstrap],
            ) as mock_load_bootstrap,
        ):
            MockDaemonClient.return_value = mock_daemon_client

            manager = HookManager(
                daemon_host="localhost",
                daemon_port=60887,
                config=None,
                database=hub_db,
            )

            # Health check should still work with defaults
            assert manager._health_monitor is not None
            assert manager._health_monitor._health_check_interval == 10.0
            assert mock_load_bootstrap.call_count == 2

            manager.shutdown()


class TestHookManagerWorkflowBlocking:
    """Tests for workflow blocking behavior."""

    def test_handle_workflow_blocks_event(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that workflow can block an event."""
        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="test-workflow-block",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "bash"},
            machine_id="21000000-0000-4000-8000-000000000004",
        )

        # Mock workflow handler to return block decision
        with patch.object(
            manager._workflow_handler,
            "handle",
            return_value=HookResponse(decision="block", reason="Workflow blocked"),
        ):
            response = manager.handle(event)

        assert response.decision == "block"
        assert response.reason == "Workflow blocked"

    def test_handle_workflow_ask_decision(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that workflow can return ask decision."""
        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="test-workflow-ask",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "bash"},
            machine_id="21000000-0000-4000-8000-000000000004",
        )

        # Mock workflow handler to return ask decision
        with patch.object(
            manager._workflow_handler,
            "handle",
            return_value=HookResponse(decision="ask", reason="Need confirmation"),
        ):
            response = manager.handle(event)

        assert response.decision == "ask"
        assert response.reason == "Need confirmation"

    def test_handle_workflow_context_merged(
        self,
        hook_manager_with_mocks: HookManager,
        sample_session_start_event: HookEvent,
        temp_dir: Path,
    ) -> None:
        """Test that workflow context is merged into response."""
        manager = hook_manager_with_mocks
        manager.handle(sample_session_start_event)
        first_prompt = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=sample_session_start_event.session_id,
            source=sample_session_start_event.source,
            timestamp=datetime.now(UTC),
            data={"prompt": "hello", "cwd": str(temp_dir)},
            machine_id=LOCAL_MACHINE_ID,
        )

        # Mock workflow handler to return context
        workflow_response = HookResponse(decision="allow", context="Workflow context info")
        with patch.object(manager._workflow_handler, "handle", return_value=workflow_response):
            response = manager.handle(first_prompt)

        assert response.decision == "allow"
        assert "Workflow context info" in (response.context or "")

    def test_handle_workflow_error_fails_open(
        self, hook_manager_with_mocks: HookManager, sample_session_start_event: HookEvent
    ) -> None:
        """Test that workflow errors fail open."""
        manager = hook_manager_with_mocks

        # Mock workflow handler to raise exception
        with patch.object(
            manager._workflow_handler,
            "handle",
            side_effect=Exception("Workflow engine error"),
        ):
            response = manager.handle(sample_session_start_event)

        # Should still allow (fail-open)
        assert response.decision == "allow"


class TestHookManagerWebhookBlocking:
    """Tests for webhook blocking behavior."""

    def test_handle_webhook_blocks_event(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that blocking webhook can block an event."""
        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="test-webhook-block",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "bash"},
            machine_id="21000000-0000-4000-8000-000000000004",
        )

        # Mock webhook dispatcher to return block decision
        with (
            patch.object(manager, "_dispatch_webhooks_sync", return_value=[MagicMock()]),
            patch.object(
                manager._webhook_dispatcher,
                "get_blocking_decision",
                return_value=("block", "Webhook rejected"),
            ),
        ):
            response = manager.handle(event)

        assert response.decision == "block"
        assert response.reason is not None
        assert "Webhook rejected" in response.reason

    def test_handle_webhook_error_fails_open(
        self, hook_manager_with_mocks: HookManager, sample_session_start_event: HookEvent
    ) -> None:
        """Test that webhook errors fail open."""
        manager = hook_manager_with_mocks

        # Mock webhook dispatch to raise exception
        with patch.object(
            manager, "_dispatch_webhooks_sync", side_effect=Exception("Webhook error")
        ):
            response = manager.handle(sample_session_start_event)

        # Should still allow (fail-open)
        assert response.decision == "allow"


class TestHookManagerBlockedObservability:
    """Blocked hooks still reach best-effort observers without changing the denial."""

    def test_rule_block_uses_enriched_observability_tail_once(
        self, hook_manager_with_mocks: HookManager
    ) -> None:
        manager = hook_manager_with_mocks
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="test-rule-block-observers",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "bash"},
            machine_id="21000000-0000-4000-8000-000000000004",
        )
        blocked = HookResponse(decision="block", reason="Rule denied", metadata={"origin": "rule"})
        order: list[str] = []
        observed: list[HookResponse] = []
        handler = MagicMock()

        def enrich(_event: HookEvent, response: HookResponse, **_kwargs: Any) -> None:
            order.append("enrich")
            response.metadata["enriched"] = True

        def broadcast(*args: Any, **_kwargs: Any) -> None:
            order.append("broadcast")
            observed.append(args[2])

        def dispatch(_event: HookEvent, response: HookResponse) -> None:
            order.append("webhook")
            observed.append(response)

        with (
            patch.object(manager._workflow_handler, "handle", return_value=blocked),
            patch.object(manager, "_get_event_handler", return_value=handler),
            patch.object(manager, "_evaluate_blocking_webhooks") as blocking_webhooks,
            patch.object(manager._enricher, "enrich", side_effect=enrich),
            patch("gobby.hooks.hook_manager.schedule_hook_broadcast", side_effect=broadcast),
            patch.object(manager, "_dispatch_webhooks_async", side_effect=dispatch),
        ):
            response = manager.handle(event)

        assert response is blocked
        assert response.decision == "block"
        assert response.reason == "Rule denied"
        assert response.metadata == {"origin": "rule"}
        assert order == ["enrich", "broadcast", "webhook"]
        assert len(observed) == 2
        assert all(item.decision == "block" for item in observed)
        assert all(item.metadata["enriched"] is True for item in observed)
        blocking_webhooks.assert_not_called()
        handler.assert_not_called()

    def test_blocking_webhook_block_dispatches_nonblocking_observer_after_broadcast(
        self, hook_manager_with_mocks: HookManager
    ) -> None:
        manager = hook_manager_with_mocks
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="test-webhook-block-observers",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "bash"},
            machine_id="21000000-0000-4000-8000-000000000004",
        )
        blocked = HookResponse(decision="block", reason="Blocking webhook denied")
        order: list[str] = []
        observed: list[HookResponse] = []
        handler = MagicMock()

        def blocking(_event: HookEvent, _deadline: BlockingEffectDeadline) -> HookResponse:
            order.append("blocking")
            return blocked

        def enrich(_event: HookEvent, response: HookResponse, **_kwargs: Any) -> None:
            order.append("enrich")
            response.metadata["enriched"] = True

        def broadcast(*args: Any, **_kwargs: Any) -> None:
            order.append("broadcast")
            observed.append(args[2])

        def dispatch(_event: HookEvent, response: HookResponse) -> None:
            order.append("nonblocking")
            observed.append(response)

        with (
            patch.object(manager._workflow_handler, "handle", return_value=HookResponse()),
            patch.object(manager, "_get_event_handler", return_value=handler),
            patch.object(
                manager, "_evaluate_blocking_webhooks", side_effect=blocking
            ) as blocking_webhooks,
            patch.object(manager._enricher, "enrich", side_effect=enrich),
            patch("gobby.hooks.hook_manager.schedule_hook_broadcast", side_effect=broadcast),
            patch.object(manager, "_dispatch_webhooks_async", side_effect=dispatch),
        ):
            response = manager.handle(event)

        assert response is blocked
        assert response.decision == "block"
        assert response.reason == "Blocking webhook denied"
        assert response.metadata == {}
        assert order == ["blocking", "enrich", "broadcast", "nonblocking"]
        assert len(observed) == 2
        assert all(item.decision == "block" for item in observed)
        assert all(item.metadata["enriched"] is True for item in observed)
        blocking_webhooks.assert_called_once()
        copied_event = blocking_webhooks.call_args.args[0]
        assert copied_event.event_type == HookEventType.SESSION_START
        assert copied_event.metadata["_synthetic_session_start"] is True
        assert copied_event.session_id == event.session_id
        assert isinstance(blocking_webhooks.call_args.args[1], BlockingEffectDeadline)
        handler.assert_not_called()

    def test_observer_failures_cannot_mutate_original_block(
        self, hook_manager_with_mocks: HookManager
    ) -> None:
        manager = hook_manager_with_mocks
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="test-block-observer-failures",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "bash"},
            machine_id="21000000-0000-4000-8000-000000000004",
        )
        blocked = HookResponse(
            decision="block", reason="Original denial", metadata={"origin": "rule"}
        )
        broadcasted: list[tuple[str, str | None]] = []

        def broken_enrich(_event: HookEvent, response: HookResponse, **_kwargs: Any) -> None:
            response.decision = "allow"
            response.reason = "mutated"
            raise RuntimeError("enrichment failed")

        def broadcast(*args: Any, **_kwargs: Any) -> None:
            response = args[2]
            broadcasted.append((response.decision, response.reason))

        def broken_dispatch(_event: HookEvent, response: HookResponse) -> None:
            response.decision = "allow"
            raise RuntimeError("observer failed")

        with (
            patch.object(manager._workflow_handler, "handle", return_value=blocked),
            patch.object(manager._enricher, "enrich", side_effect=broken_enrich),
            patch("gobby.hooks.hook_manager.schedule_hook_broadcast", side_effect=broadcast),
            patch.object(manager, "_dispatch_webhooks_async", side_effect=broken_dispatch),
        ):
            response = manager.handle(event)

        assert response is blocked
        assert response.decision == "block"
        assert response.reason == "Original denial"
        assert response.metadata == {"origin": "rule"}
        assert broadcasted == [("block", "Original denial")]


class TestHookManagerHandlerErrors:
    """Tests for handler error handling."""

    @pytest.mark.parametrize(
        "event_type",
        [HookEventType.SESSION_START, HookEventType.BEFORE_TOOL],
    )
    def test_handle_rejects_missing_hook_machine_id(
        self,
        hook_manager_with_mocks: HookManager,
        temp_dir: Path,
        event_type: HookEventType,
    ) -> None:
        event = HookEvent(
            event_type=event_type,
            session_id="missing-machine-id",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"cwd": str(temp_dir), "tool_name": "bash"},
        )

        response = hook_manager_with_mocks.handle(event)

        assert response.decision == "block"
        assert response.reason == "Hook envelope is missing required machine_id"

    def test_handle_handler_exception_fails_open(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that handler exceptions fail open."""
        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id="test-handler-error",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"cwd": str(temp_dir)},
            machine_id="21000000-0000-4000-8000-000000000004",
        )

        # Mock handler to raise exception
        def failing_handler(evt: Any) -> Any:
            raise Exception("Handler crashed")

        with patch.object(manager._event_handlers, "get_handler", return_value=failing_handler):
            response = manager.handle(event)

        assert response.decision == "allow"
        assert response.reason is not None
        assert "Handler error:" in response.reason


class TestHookManagerBroadcasting:
    """Tests for event broadcasting."""

    def test_handle_broadcasts_event_with_loop(
        self, hook_manager_with_mocks: HookManager, sample_session_start_event: HookEvent
    ) -> None:
        """Test that events are broadcast when broadcaster is configured."""
        import asyncio

        manager = hook_manager_with_mocks

        mock_broadcaster = MagicMock()

        async def mock_broadcast(*args: Any, **kwargs: Any) -> Any:
            return None

        mock_broadcaster.broadcast_event = MagicMock(side_effect=mock_broadcast)
        manager.broadcaster = mock_broadcaster

        # Simulate running in an event loop
        async def run_in_loop() -> Any:
            return manager.handle(sample_session_start_event)

        asyncio.run(run_in_loop())

        # Broadcaster should have been called
        assert mock_broadcaster.broadcast_event.called

    def test_handle_broadcasts_event_threadsafe(
        self, hook_manager_with_mocks: HookManager, sample_session_start_event: HookEvent
    ) -> None:
        """Test that events are broadcast thread-safely when no loop is running."""
        import asyncio
        import threading

        manager = hook_manager_with_mocks

        mock_broadcaster = MagicMock()
        broadcasted = threading.Event()

        async def mock_broadcast(*args: Any, **kwargs: Any) -> Any:
            broadcasted.set()
            return None

        mock_broadcaster.broadcast_event = MagicMock(side_effect=mock_broadcast)
        manager.broadcaster = mock_broadcaster

        # Create a loop for thread-safe scheduling and run it in a thread
        loop = asyncio.new_event_loop()
        manager._loop = loop

        import threading

        def run_loop() -> Any:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        loop_thread = threading.Thread(target=run_loop, daemon=True)
        loop_thread.start()

        try:
            # Call handle outside of event loop
            manager.handle(sample_session_start_event)
            assert broadcasted.wait(timeout=1)
        finally:
            manager._loop = None
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(timeout=1)
            loop.close()
        assert mock_broadcaster.broadcast_event.called

    def test_handle_no_loop_no_broadcaster_error(
        self, hook_manager_with_mocks: HookManager, sample_session_start_event: HookEvent
    ) -> None:
        """Test that handle works without event loop and no broadcaster."""
        manager = hook_manager_with_mocks
        manager.broadcaster = MagicMock()
        manager._loop = None

        # Should not raise
        response = manager.handle(sample_session_start_event)
        assert response.decision == "allow"

    def test_handle_broadcast_threadsafe_error(
        self, hook_manager_with_mocks: HookManager, sample_session_start_event: HookEvent
    ) -> None:
        """Test that broadcast errors from run_coroutine_threadsafe are handled."""
        import asyncio
        import warnings

        manager = hook_manager_with_mocks

        mock_broadcaster = MagicMock()

        async def mock_broadcast(*args: Any, **kwargs: Any) -> Any:
            return None

        mock_broadcaster.broadcast_event = MagicMock(side_effect=mock_broadcast)
        manager.broadcaster = mock_broadcaster

        # Create a closed loop to trigger error
        loop = asyncio.new_event_loop()
        loop.close()
        manager._loop = loop

        # Suppress the "coroutine was never awaited" warning since we're testing error handling
        # with a closed loop that can't run the coroutine
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="coroutine .* was never awaited")
            # Should not raise - error is logged
            response = manager.handle(sample_session_start_event)
        assert response.decision == "allow"

    def test_handle_dispatch_webhooks_async_error(
        self, hook_manager_with_mocks: HookManager, sample_session_start_event: HookEvent
    ) -> None:
        """Test that async webhook dispatch errors are handled."""
        manager = hook_manager_with_mocks

        # Mock _dispatch_webhooks_async to raise exception
        with patch.object(
            manager, "_dispatch_webhooks_async", side_effect=Exception("Webhook error")
        ):
            # Should not raise - error is logged
            response = manager.handle(sample_session_start_event)

        assert response.decision == "allow"


class TestHookManagerSessionLookup:
    """Tests for session lookup and auto-registration."""

    def test_session_start_precreated_session_skips_auto_registration(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Pre-created SESSION_START rows must bind in-place without a stray auto-register."""
        manager = hook_manager_with_mocks
        project_meta = (temp_dir / ".gobby" / "project.json").read_text()
        project_id = json.loads(project_meta)["id"]
        precreated = manager.session_manager.create_web_chat_session(
            machine_id="21000000-0000-4000-8000-000000000004",
            project_id=project_id,
            source="codex",
            model="gpt-5.4",
            sandbox_enabled=False,
            sandbox_policy_hash="policy-hash",
        )

        event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id=precreated.id,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={"cwd": str(temp_dir), "source": "startup"},
            machine_id="21000000-0000-4000-8000-000000000004",
        )

        with patch.object(
            manager.session_manager,
            "register_session",
            wraps=manager.session_manager.register_session,
        ) as mock_register:
            response = manager.handle(event)

        assert response.decision == "allow"
        mock_register.assert_not_called()
        assert event.metadata["_platform_session_id"] == precreated.id

    async def test_resumed_codex_ignores_stale_wrapper_metadata_for_session_context(
        self,
        hook_manager_with_mocks: HookManager,
        temp_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A stale injected wrapper id must be replaced by the canonical Codex session."""
        manager = hook_manager_with_mocks
        external_id = "resumed-codex-session"

        start_event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id=external_id,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
                "cwd": str(temp_dir),
                "source": "startup",
                "transcript_path": str(temp_dir / "resumed-codex.jsonl"),
            },
            machine_id="21000000-0000-4000-8000-000000000004",
        )
        response = manager.handle(start_event)

        assert response.decision == "allow"
        first_prompt = HookEvent(
            event_type=HookEventType.BEFORE_AGENT,
            session_id=external_id,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
                "prompt": "resume",
                "cwd": str(temp_dir),
                "transcript_path": str(temp_dir / "resumed-codex.jsonl"),
            },
            machine_id=LOCAL_MACHINE_ID,
        )
        assert manager.handle(first_prompt).decision == "allow"
        canonical_id = first_prompt.metadata["_platform_session_id"]
        stale_wrapper_id = str(uuid.uuid4())
        assert stale_wrapper_id != canonical_id

        caplog.set_level(logging.WARNING)
        caplog.clear()
        resumed_event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id=external_id,
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "mcp__gobby__set_variable",
                "tool_input": {
                    "name": "wrapper_recovery",
                    "value": True,
                    "session_id": canonical_id,
                },
                "cwd": str(temp_dir),
            },
            machine_id="21000000-0000-4000-8000-000000000004",
            metadata={"_platform_session_id": stale_wrapper_id},
        )
        response = manager.handle(resumed_event)

        assert response.decision == "allow"
        assert resumed_event.metadata["_platform_session_id"] == canonical_id

        tokens = await resolve_and_seed_contexts(
            session_ref=resumed_event.metadata["_platform_session_id"],
            session_manager=manager.session_manager,
            db=manager._database,
        )
        try:
            assert tokens.resolved_session_id == canonical_id
            variables = SessionVariableManager(manager._database)
            variables.set_variable(tokens.resolved_session_id, "wrapper_recovery", True)
            assert variables.get_variables(canonical_id)["wrapper_recovery"] is True
            assert variables.get_variables(stale_wrapper_id) == {}
            warning_messages = [record.getMessage() for record in caplog.records]
            assert not any("Session not found" in message for message in warning_messages)
            assert not any(
                "could not resolve session ref" in message for message in warning_messages
            )
        finally:
            reset_seeded_contexts(tokens)

    def test_handle_looks_up_session_from_database(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that session is looked up from database when not in cache."""
        manager = hook_manager_with_mocks

        # Create an event for a non-cached session
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="unknown-session-id",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "bash", "cwd": str(temp_dir)},
            machine_id="21000000-0000-4000-8000-000000000004",
        )

        # Session not in cache, should query database
        with patch.object(manager._session_manager, "get_session_id", return_value=None):
            response = manager.handle(event)

        # Should still allow (session will be auto-registered)
        assert response.decision == "allow"

    def test_handle_auto_registers_unknown_session(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that unknown sessions are auto-registered."""
        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="auto-register-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "bash",
                "cwd": str(temp_dir),
                "transcript_path": str(temp_dir / "transcript.jsonl"),
            },
            machine_id="21000000-0000-4000-8000-000000000004",
        )

        # Session not in cache or database
        with (
            patch.object(manager._session_manager, "get_session_id", return_value=None),
            patch.object(manager._session_manager, "lookup_session_id", return_value=None),
            patch.object(
                manager._session_manager,
                "register_session",
                return_value="new-session-id",
            ) as mock_register,
        ):
            response = manager.handle(event)

        # Should have called register_session
        assert mock_register.called
        assert response.decision == "allow"

    def test_slow_registration_does_not_block_different_session(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        manager = hook_manager_with_mocks
        registration_started = threading.Event()
        release_registration = threading.Event()
        fast_completed = threading.Event()
        responses: dict[str, HookResponse] = {}

        def event(external_id: str) -> HookEvent:
            return HookEvent(
                event_type=HookEventType.BEFORE_TOOL,
                session_id=external_id,
                source=SessionSource.CLAUDE,
                timestamp=datetime.now(UTC),
                data={"tool_name": "bash", "cwd": str(temp_dir)},
                machine_id="21000000-0000-4000-8000-000000000004",
            )

        def register_session(*, external_id: str, **_kwargs: object) -> str:
            if external_id == "slow-session":
                registration_started.set()
                assert release_registration.wait(timeout=5)
            return f"platform-{external_id}"

        def handle(name: str) -> None:
            responses[name] = manager.handle(event(f"{name}-session"))
            if name == "fast":
                fast_completed.set()

        with (
            patch.object(manager._session_manager, "get_session_id", return_value=None),
            patch.object(manager._session_manager, "lookup_session_id", return_value=None),
            patch.object(manager._session_manager, "recover_session", return_value=None),
            patch.object(
                manager._session_manager, "register_session", side_effect=register_session
            ),
        ):
            slow_thread = threading.Thread(target=handle, args=("slow",))
            fast_thread = threading.Thread(target=handle, args=("fast",))
            slow_thread.start()
            assert registration_started.wait(timeout=5)
            fast_thread.start()
            try:
                assert fast_completed.wait(timeout=1)
            finally:
                release_registration.set()
                slow_thread.join(timeout=5)
                fast_thread.join(timeout=5)

        assert not slow_thread.is_alive()
        assert not fast_thread.is_alive()
        assert responses["slow"].decision == "allow"
        assert responses["fast"].decision == "allow"

    def test_non_start_acp_child_does_not_auto_register_terminal_duplicate(
        self,
        hook_manager_with_mocks: HookManager,
        temp_dir: Path,
    ) -> None:
        manager = hook_manager_with_mocks
        # A real UUID: the ACP branch now runs a genuine web_chat parent lookup
        # against the sessions table before deciding to skip.
        project_id = json.loads((temp_dir / ".gobby" / "project.json").read_text())["id"]
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="acp-child-session",
            source=SessionSource.GROK,
            timestamp=datetime.now(UTC),
            data={
                "cwd": str(temp_dir),
                "tool_name": "Read",
                "terminal_context": {"gobby_acp_child": "1"},
            },
            machine_id="21000000-0000-4000-8000-000000000004",
            project_id=project_id,
        )

        with (
            patch.object(manager._session_manager, "get_session_id", return_value=None),
            patch.object(manager._session_manager, "lookup_session_id", return_value=None),
            patch.object(manager._session_manager, "recover_session", return_value=None),
            patch.object(
                manager._session_manager,
                "register_session",
                return_value="phantom-terminal-session",
            ) as mock_register,
        ):
            platform_session_id = manager._session_lookup.resolve(event)

        assert platform_session_id is None
        # Unresolvable sessions leave the key absent — a stored None would
        # read as a validated canonical id downstream.
        assert "_platform_session_id" not in event.metadata
        mock_register.assert_not_called()
        row = manager._session_manager.db.fetchone(
            "SELECT COUNT(*) AS count FROM sessions WHERE external_id = %s",
            ("acp-child-session",),
        )
        assert row is not None
        assert row["count"] == 0

    def test_acp_child_marker_preserves_existing_web_chat_parent_binding(
        self,
        hook_manager_with_mocks: HookManager,
        temp_dir: Path,
    ) -> None:
        manager = hook_manager_with_mocks
        project_id = json.loads((temp_dir / ".gobby" / "project.json").read_text())["id"]
        web_chat_parent = cast(SessionManager, manager._session_manager).register(
            external_id="acp-parent-session",
            machine_id="21000000-0000-4000-8000-000000000004",
            source="grok",
            project_id=project_id,
            session_type="web_chat",
        )
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="acp-parent-session",
            source=SessionSource.GROK,
            timestamp=datetime.now(UTC),
            data={
                "cwd": str(temp_dir),
                "tool_name": "Read",
                "terminal_context": {"gobby_acp_child": "1"},
            },
            machine_id="21000000-0000-4000-8000-000000000004",
            project_id=project_id,
        )

        with (
            patch.object(manager._session_manager, "get_session_id", return_value=None),
            patch.object(manager._session_manager, "register_session") as mock_register,
            patch.object(manager._session_lookup, "_revive_expired_terminal_session"),
            patch.object(manager._session_lookup, "_backfill_terminal_context"),
            patch.object(manager._session_lookup, "_enrich_task_context"),
        ):
            platform_session_id = manager._session_lookup.resolve(event)

        assert platform_session_id == web_chat_parent.id
        assert event.metadata["_platform_session_id"] == web_chat_parent.id
        mock_register.assert_not_called()
        rows = manager._session_manager.db.fetchall(
            "SELECT id, session_type FROM sessions WHERE external_id = %s",
            ("acp-parent-session",),
        )
        assert [(str(row["id"]), row["session_type"]) for row in rows] == [
            (web_chat_parent.id, "web_chat")
        ]

    def test_handle_does_not_auto_register_unknown_session_end(
        self,
        hook_manager_with_mocks: HookManager,
        temp_dir: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Unknown SESSION_END hooks should not create placeholder session rows."""
        manager = hook_manager_with_mocks
        caplog.set_level(logging.INFO)

        event = HookEvent(
            event_type=HookEventType.SESSION_END,
            session_id="orphaned-session-end",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={
                "cwd": str(temp_dir),
                "transcript_path": str(temp_dir / "missing-transcript.jsonl"),
            },
            machine_id="21000000-0000-4000-8000-000000000004",
        )

        with (
            patch.object(manager._session_manager, "get_session_id", return_value=None),
            patch.object(manager._session_manager, "lookup_session_id", return_value=None),
            patch.object(manager._session_manager, "recover_session", return_value=None),
        ):
            response = manager.handle(event)

        rows = manager._session_manager.db.fetchall(
            "SELECT id FROM sessions WHERE external_id = %s",
            ("orphaned-session-end",),
        )

        assert response.decision == "allow"
        assert event.metadata.get("_platform_session_id") is None
        assert rows == []
        assert "Skipping auto-registration for orphaned SESSION_END" in caplog.text
        assert "SESSION_END: session_id not found" in caplog.text
        assert not [record for record in caplog.records if record.levelno >= logging.WARNING]

    def test_handle_recovers_existing_session_across_source_mismatch(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Later hooks with the wrong source should reuse the existing row."""
        manager = hook_manager_with_mocks
        project_meta = (temp_dir / ".gobby" / "project.json").read_text()
        project_id = json.loads(project_meta)["id"]
        existing = manager.session_manager.register(
            external_id="shared-session-id",
            machine_id="21000000-0000-4000-8000-000000000004",
            source="codex",
            project_id=project_id,
            transcript_path=str(temp_dir / "rollout-shared-session-id.jsonl"),
            title="Recovered Session",
        )
        existing_session_id = existing.id

        wrong_source_event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="shared-session-id",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "bash", "cwd": str(temp_dir)},
            machine_id="21000000-0000-4000-8000-000000000004",
        )

        response = manager.handle(wrong_source_event)

        assert response.decision == "allow"
        assert wrong_source_event.metadata["_platform_session_id"] == existing_session_id
        rows = manager._session_manager.db.fetchall(
            "SELECT id FROM sessions WHERE external_id = %s",
            ("shared-session-id",),
        )
        assert len(rows) == 1

    def test_handle_backfills_terminal_context_for_known_session(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Later Codex hooks should repair a session that missed SessionStart terminal metadata."""
        manager = hook_manager_with_mocks
        project_id = json.loads((temp_dir / ".gobby" / "project.json").read_text())["id"]
        registered = cast(Any, manager._session_manager).register(
            external_id="codex-missing-terminal-context",
            machine_id=LOCAL_MACHINE_ID,
            source="codex",
            project_id=project_id,
        )
        session_id = registered.id

        manager._session_manager.db.execute(
            "UPDATE sessions SET title = %s, handoff_markdown = %s WHERE id = %s",
            ("Recovered Codex Title", None, session_id),
        )

        repair_event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="codex-missing-terminal-context",
            source=SessionSource.CODEX,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "Bash",
                "cwd": str(temp_dir),
                "terminal_context": {"tmux_pane": "%5", "parent_pid": 999},
            },
            machine_id="21000000-0000-4000-8000-000000000004",
        )

        with patch("gobby.hooks.session_lookup.schedule_tmux_window_rename") as mock_schedule:
            response = manager.handle(repair_event)

        assert response.decision == "allow"
        updated = manager._session_manager.get(session_id)
        assert updated is not None
        assert updated.terminal_context is not None
        assert updated.terminal_context["tmux_pane"] == "%5"
        assert updated.terminal_context["cwd"] == str(temp_dir)
        mock_schedule.assert_called_once()

    def test_handle_resolves_active_task(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that active task is resolved for session."""
        manager = hook_manager_with_mocks

        # First register a session
        start_event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id="task-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"cwd": str(temp_dir)},
            machine_id="21000000-0000-4000-8000-000000000004",
        )
        manager.handle(start_event)

        # Now trigger a tool event with mocked task
        tool_event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="task-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "bash"},
            machine_id="21000000-0000-4000-8000-000000000004",
        )

        mock_task = MagicMock()
        mock_task.id = "gt-test123"
        mock_task.title = "Test Task"
        mock_task.status = "in_progress"

        with patch.object(
            manager._session_task_manager,
            "get_worked_on_task",
            return_value=mock_task,
        ):
            response = manager.handle(tool_event)

        assert response.decision == "allow"
        # Task context should be in event metadata
        assert tool_event.task_id == "gt-test123"

    def test_handle_task_resolution_error(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that task resolution errors are handled gracefully."""
        manager = hook_manager_with_mocks

        # First register a session
        start_event = HookEvent(
            event_type=HookEventType.SESSION_START,
            session_id="task-error-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"cwd": str(temp_dir)},
            machine_id="21000000-0000-4000-8000-000000000004",
        )
        manager.handle(start_event)

        tool_event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="task-error-session",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"tool_name": "bash"},
            machine_id="21000000-0000-4000-8000-000000000004",
        )

        with patch.object(
            manager._session_task_manager,
            "get_worked_on_task",
            side_effect=Exception("Database error"),
        ):
            response = manager.handle(tool_event)

        # Should still allow (error handled gracefully)
        assert response.decision == "allow"


class TestHookManagerWebhookDispatch:
    """Tests for webhook dispatch methods."""

    def test_dispatch_webhooks_sync_disabled(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that sync webhook dispatch returns empty when disabled."""
        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="webhook-test",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={},
            machine_id="21000000-0000-4000-8000-000000000004",
        )

        # Disable webhooks
        manager._webhook_dispatcher.config.enabled = False

        result = manager._dispatch_webhooks_sync(event)
        assert result == []

    def test_dispatch_webhooks_sync_no_matching_endpoints(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that sync webhook dispatch returns empty when no matching endpoints."""
        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="webhook-test",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={},
            machine_id="21000000-0000-4000-8000-000000000004",
        )

        # Enable webhooks but have no endpoints
        manager._webhook_dispatcher.config.enabled = True
        manager._webhook_dispatcher.config.endpoints = []

        result = manager._dispatch_webhooks_sync(event)
        assert result == []

    def test_dispatch_webhooks_sync_with_matching_endpoints(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that sync webhook dispatch works with matching endpoints."""
        from gobby.config.extensions import WebhookEndpointConfig

        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="webhook-test",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={},
            machine_id="21000000-0000-4000-8000-000000000004",
        )

        # Create a blocking endpoint
        endpoint = WebhookEndpointConfig(
            name="test-webhook",
            url="https://example.com/webhook",
            events=["before_tool"],
            can_block=True,
            enabled=True,
        )

        # Enable webhooks with a blocking endpoint
        manager._webhook_dispatcher.config.enabled = True
        manager._webhook_dispatcher.config.endpoints = [endpoint]

        # Mock the dispatch to avoid actual HTTP calls
        from gobby.hooks.webhooks import WebhookResult

        mock_result = WebhookResult(
            endpoint_name="test-webhook",
            success=True,
            status_code=200,
            response_body={"action": "allow"},
        )

        with (
            patch.object(manager._webhook_dispatcher, "_build_payload", return_value={}),
            patch.object(
                manager._webhook_dispatcher,
                "_dispatch_single",
                return_value=mock_result,
            ),
        ):
            result = manager._dispatch_webhooks_sync(event, blocking_only=True)

        assert len(result) == 1
        assert result[0].success is True

    def test_consecutive_sync_webhooks_use_loop_local_clients(
        self, hook_manager_with_mocks: HookManager
    ) -> None:
        """Each sync bridge owns and closes its client inside that event loop."""
        import asyncio

        import httpx

        from gobby.config.extensions import WebhookEndpointConfig

        manager = hook_manager_with_mocks
        endpoint = WebhookEndpointConfig(
            name="blocking-webhook",
            url="https://example.com/webhook",
            events=["before_tool"],
            can_block=True,
            retry_count=0,
            enabled=True,
        )
        manager._webhook_dispatcher.config.enabled = True
        manager._webhook_dispatcher.config.endpoints = [endpoint]
        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="webhook-test",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={},
            machine_id="21000000-0000-4000-8000-000000000004",
        )

        post_count = 0

        class LoopBoundClient:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                self.loop: asyncio.AbstractEventLoop | None = None
                self.closed = False
                created_clients.append(self)

            async def __aenter__(self) -> "LoopBoundClient":
                return self

            async def __aexit__(
                self,
                _exc_type: object,
                _exc: object,
                _traceback: object,
            ) -> None:
                await self.aclose()

            async def aclose(self) -> None:
                self.closed = True

            def build_request(
                self,
                method: str,
                url: str,
                **kwargs: Any,
            ) -> httpx.Request:
                kwargs.pop("timeout", None)
                return httpx.Request(method, url, **kwargs)

            async def send(
                self,
                request: httpx.Request,
                *,
                stream: bool = False,
                follow_redirects: bool = False,
            ) -> httpx.Response:
                nonlocal post_count
                current_loop = asyncio.get_running_loop()
                if self.loop is None:
                    self.loop = current_loop
                elif self.loop is not current_loop:
                    raise RuntimeError("client reused across event loops")
                post_count += 1
                decision = "allow" if post_count == 1 else "deny"
                assert stream is True
                assert follow_redirects is False
                return httpx.Response(200, json={"decision": decision}, request=request)

        created_clients: list[LoopBoundClient] = []
        with (
            patch.object(
                manager._webhook_dispatcher._transport,
                "_lookup_addresses",
                new=AsyncMock(return_value=("93.184.216.34",)),
            ),
            patch("gobby.hooks.webhooks.httpx.AsyncClient", LoopBoundClient),
        ):
            first = manager._dispatch_webhooks_sync(event, blocking_only=True)
            second = manager._dispatch_webhooks_sync(event, blocking_only=True)

        assert manager._webhook_dispatcher.get_blocking_decision(first)[0] == "allow"
        assert manager._webhook_dispatcher.get_blocking_decision(second)[0] == "block"
        assert len(created_clients) == 2
        assert all(client.closed for client in created_clients)

    def test_dispatch_webhooks_async_disabled(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that async webhook dispatch does nothing when disabled."""
        from gobby.config.extensions import WebhookEndpointConfig

        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="webhook-async-test",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={},
            machine_id="21000000-0000-4000-8000-000000000004",
        )

        endpoint = WebhookEndpointConfig(
            name="disabled-async-webhook",
            url="https://example.com/webhook",
            events=["before_tool"],
            can_block=False,
            enabled=True,
        )

        manager._webhook_dispatcher.config.enabled = False
        manager._webhook_dispatcher.config.endpoints = [endpoint]

        with (
            patch.object(manager._webhook_dispatcher, "_build_payload") as build_payload,
            patch.object(
                manager._webhook_dispatcher,
                "_dispatch_single",
                new_callable=AsyncMock,
            ) as dispatch_single,
        ):
            result = manager._dispatch_webhooks_async(event)

        assert result is None
        build_payload.assert_not_called()
        dispatch_single.assert_not_called()

    def test_dispatch_webhooks_async_no_matching_endpoints(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that async webhook dispatch does nothing when no matching endpoints."""
        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="webhook-async-test",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={},
            machine_id="21000000-0000-4000-8000-000000000004",
        )

        # Enable webhooks but have no non-blocking endpoints
        manager._webhook_dispatcher.config.enabled = True
        manager._webhook_dispatcher.config.endpoints = []

        with (
            patch.object(manager._webhook_dispatcher, "_build_payload") as build_payload,
            patch.object(
                manager._webhook_dispatcher,
                "_dispatch_single",
                new_callable=AsyncMock,
            ) as dispatch_single,
        ):
            result = manager._dispatch_webhooks_async(event)

        assert result is None
        build_payload.assert_not_called()
        dispatch_single.assert_not_called()

    def test_dispatch_webhooks_async_with_matching_endpoints(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that async webhook dispatch schedules tasks for matching endpoints."""
        import asyncio
        import threading

        from gobby.config.extensions import WebhookEndpointConfig

        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="webhook-async-test",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={},
            machine_id="21000000-0000-4000-8000-000000000004",
        )

        # Create a non-blocking endpoint
        endpoint = WebhookEndpointConfig(
            name="test-async-webhook",
            url="https://example.com/webhook",
            events=["before_tool"],
            can_block=False,
            enabled=True,
        )

        manager._webhook_dispatcher.config.enabled = True
        manager._webhook_dispatcher.config.endpoints = [endpoint]

        # Create a loop for async dispatch
        loop = asyncio.new_event_loop()
        manager._loop = loop

        def run_loop() -> Any:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        loop_thread = threading.Thread(target=run_loop, daemon=True)
        loop_thread.start()
        dispatched = threading.Event()
        observer_response = HookResponse(
            decision="block", reason="Observed denial", metadata={"enriched": True}
        )

        try:

            async def mock_dispatch(*args: Any, **kwargs: Any) -> Any:
                dispatched.set()
                return None

            with (
                patch.object(
                    manager._webhook_dispatcher, "_build_payload", return_value={}
                ) as mock_build_payload,
                patch.object(
                    manager._webhook_dispatcher,
                    "_dispatch_single",
                    new_callable=AsyncMock,
                    side_effect=mock_dispatch,
                ) as mock_dispatch_single,
            ):
                # Should schedule async task
                manager._dispatch_webhooks_async(event, observer_response)
                assert dispatched.wait(timeout=1), "Async webhook dispatch never ran"
                assert mock_dispatch_single.await_count == 1
                mock_build_payload.assert_called_once_with(event, observer_response)

                async def no_op() -> None:
                    return None

                asyncio.run_coroutine_threadsafe(no_op(), loop).result(timeout=1)
        finally:
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(timeout=1)
            loop.close()

    def test_dispatch_webhooks_async_within_running_loop(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that async webhook dispatch creates task when inside running loop."""
        import asyncio

        from gobby.config.extensions import WebhookEndpointConfig

        manager = hook_manager_with_mocks

        event = HookEvent(
            event_type=HookEventType.BEFORE_TOOL,
            session_id="webhook-async-loop-test",
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={},
            machine_id="21000000-0000-4000-8000-000000000004",
        )

        # Create a non-blocking endpoint
        endpoint = WebhookEndpointConfig(
            name="test-loop-webhook",
            url="https://example.com/webhook",
            events=["before_tool"],
            can_block=False,
            enabled=True,
        )

        manager._webhook_dispatcher.config.enabled = True
        manager._webhook_dispatcher.config.endpoints = [endpoint]

        async def run_dispatch() -> Any:
            with (
                patch.object(manager._webhook_dispatcher, "_build_payload", return_value={}),
                patch.object(
                    manager._webhook_dispatcher,
                    "_dispatch_single",
                    new_callable=AsyncMock,
                ),
            ):
                manager._dispatch_webhooks_async(event)
                dispatch_single = cast(AsyncMock, manager._webhook_dispatcher._dispatch_single)
                await wait_for_async_condition(
                    lambda: dispatch_single.await_count == 1,
                    description="webhook dispatch",
                )
                assert dispatch_single.await_count == 1

        asyncio.run(run_dispatch())


class TestHookManagerShutdownWebhook:
    """Tests for shutdown webhook cleanup."""

    def test_shutdown_closes_webhook_dispatcher_with_loop(
        self, hook_manager_with_mocks: HookManager
    ) -> None:
        """Test that shutdown closes webhook dispatcher when loop is available."""
        import asyncio

        manager = hook_manager_with_mocks

        # Set up a loop in a separate thread (like in real async context)
        import threading

        loop = asyncio.new_event_loop()
        manager._loop = loop

        def run_loop() -> Any:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        loop_thread = threading.Thread(target=run_loop, daemon=True)
        loop_thread.start()

        try:
            manager.shutdown()
        finally:
            manager._loop = None
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(timeout=1)
            loop.close()

        assert manager._health_monitor._is_shutdown is True

    def test_shutdown_closes_webhook_dispatcher_without_loop(
        self, hook_manager_with_mocks: HookManager
    ) -> None:
        """Test that shutdown closes webhook dispatcher when no loop is available."""
        manager = hook_manager_with_mocks
        manager._loop = None

        # Should not raise
        manager.shutdown()

        assert manager._health_monitor._is_shutdown is True

    def test_shutdown_handles_webhook_close_error(
        self, hook_manager_with_mocks: HookManager
    ) -> None:
        """Test that shutdown handles webhook dispatcher close errors."""
        manager = hook_manager_with_mocks

        # Mock close to raise exception
        async def failing_close() -> Any:
            raise Exception("Close failed")

        cast(Any, manager._webhook_dispatcher).close = failing_close
        manager._loop = None

        # Should not raise - error is logged
        manager.shutdown()

        assert manager._health_monitor._is_shutdown is True


class TestHookManagerResolveProjectId:
    """Tests for project ID resolution."""

    def test_resolve_project_id_returns_provided_id(
        self, hook_manager_with_mocks: HookManager
    ) -> None:
        """Test that provided project ID is returned directly."""
        manager = hook_manager_with_mocks

        result = manager._resolve_project_id("my-project-id", "/some/path")
        assert result == "my-project-id"

    def test_resolve_project_id_from_project_context(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that project ID is resolved from project.json."""
        manager = hook_manager_with_mocks

        # Create project.json
        gobby_dir = temp_dir / ".gobby"
        gobby_dir.mkdir(exist_ok=True)
        (gobby_dir / "project.json").write_text('{"id": "context-project-id", "name": "test"}')

        result = manager._resolve_project_id(None, str(temp_dir))
        assert result == "context-project-id"

    def test_resolve_project_id_raises_without_project_json(
        self, hook_manager_with_mocks: HookManager, temp_dir: Path
    ) -> None:
        """Test that ValueError is raised when no project.json exists."""
        manager = hook_manager_with_mocks

        # Create a new temp dir without project.json
        new_dir = temp_dir / "new_project"
        new_dir.mkdir()

        with patch("gobby.utils.project_context.get_project_context", return_value=None):
            with pytest.raises(ValueError, match="gobby init"):
                manager._resolve_project_id(None, str(new_dir))


class TestHookManagerLogging:
    """Tests for centralized hook logger ownership."""

    def test_hook_manager_uses_existing_central_logger(
        self,
        mock_daemon_client: MagicMock,
        hub_db: HubDatabase,
        default_config: DaemonConfig,
    ) -> None:
        import logging

        logger = logging.getLogger("gobby.hooks")
        handler = logging.StreamHandler()
        logger.addHandler(handler)

        with patch("gobby.hooks.factory.DaemonClient") as MockDaemonClient:
            MockDaemonClient.return_value = mock_daemon_client

            manager = HookManager(
                daemon_host="localhost",
                daemon_port=60887,
                config=default_config,
                database=hub_db,
            )

            assert manager.logger is logger
            assert manager.logger.handlers == [handler]

            manager.shutdown()

        logger.removeHandler(handler)


class TestHookManagerContextMerging:
    """Tests for context merging between workflow and response."""

    def test_merge_workflow_context_with_existing_response_context(
        self, hook_manager_with_mocks: HookManager, sample_session_start_event: HookEvent
    ) -> None:
        """Test that workflow context is appended to existing response context."""
        manager = hook_manager_with_mocks

        # Mock workflow handler to return context
        workflow_response = HookResponse(decision="allow", context="Workflow context")

        # Mock event handler to return response with context. The handler must
        # bind the canonical session id or rule evaluation is skipped entirely.
        def handler_with_context(event: Any) -> Any:
            event.metadata["_platform_session_id"] = "platform-session-1"
            return HookResponse(decision="allow", context="Handler context")

        with (
            patch.object(manager._workflow_handler, "handle", return_value=workflow_response),
            patch.object(manager._event_handlers, "get_handler", return_value=handler_with_context),
        ):
            response = manager.handle(sample_session_start_event)

        # Both contexts should be present
        assert response.context is not None
        assert "Handler context" in response.context
        assert "Workflow context" in response.context


class TestHookManagerMachineIdFallback:
    """Tests for machine ID fallback behavior."""

    def test_get_machine_id_returns_none_on_none(
        self, hook_manager_with_mocks: HookManager
    ) -> None:
        """get_machine_id propagates None instead of inventing a fallback identity.

        The 'unknown-machine' fallback was removed by gobby-#19411 (UUID
        machine attribution): a fabricated identity would defeat ownership
        enforcement, so an unresolvable machine id must surface as None.
        """
        manager = hook_manager_with_mocks

        with patch("gobby.utils.machine_id.get_machine_id", return_value=None):
            result = manager.get_machine_id()
            assert result is None

    def test_get_machine_id_returns_value_when_available(
        self, hook_manager_with_mocks: HookManager
    ) -> None:
        """Test that get_machine_id returns the underlying value when available."""
        manager = hook_manager_with_mocks

        with patch("gobby.utils.machine_id.get_machine_id", return_value="my-machine-id"):
            result = manager.get_machine_id()
            assert result == "my-machine-id"


def _deferred_start_event(
    temp_dir: Path,
    external_id: str,
    source: SessionSource = SessionSource.CLAUDE,
) -> HookEvent:
    return HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id=external_id,
        source=source,
        timestamp=datetime.now(UTC),
        data={
            "source": "startup",
            "cwd": str(temp_dir),
            "transcript_path": str(temp_dir / f"{external_id}.jsonl"),
        },
        machine_id=LOCAL_MACHINE_ID,
    )


# Subagent events are absent from ``NON_MATERIALIZING_EVENTS`` because they can still
# materialize by binding to a parent session. This matrix only ever sends orphans, and
# a second guard in ``SessionLookupService`` refuses to auto-register those, so here
# they behave as non-materializing without belonging to the production frozenset.
_ORPHAN_NON_MATERIALIZING_EVENTS = NON_MATERIALIZING_EVENTS | {
    HookEventType.SUBAGENT_START,
    HookEventType.SUBAGENT_STOP,
}

_FIRST_HOOK_CASES = tuple(
    (event_type, event_type not in _ORPHAN_NON_MATERIALIZING_EVENTS)
    for event_type in HookEventType
    if event_type is not HookEventType.SESSION_START
)


@pytest.mark.parametrize(
    ("event_type", "should_materialize"),
    _FIRST_HOOK_CASES,
    ids=[event_type.value for event_type, _expected in _FIRST_HOOK_CASES],
)
def test_first_hook_materialization_matrix(
    hook_manager_with_mocks: HookManager,
    temp_dir: Path,
    event_type: HookEventType,
    should_materialize: bool,
) -> None:
    manager = hook_manager_with_mocks
    external_id = f"first-{event_type.value}"
    start = _deferred_start_event(temp_dir, external_id)
    assert manager.handle(start).decision == "allow"
    assert "_platform_session_id" not in start.metadata

    event = HookEvent(
        event_type=event_type,
        session_id=external_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"cwd": str(temp_dir)},
        machine_id=LOCAL_MACHINE_ID,
    )
    handler = MagicMock(return_value=HookResponse(decision="allow"))

    with (
        patch(
            "gobby.hooks.hook_manager.validate_managed_agent_hook",
            return_value=SimpleNamespace(
                accepted=True,
                ambiguous=False,
                run_id=None,
                reason=None,
            ),
        ),
        patch.object(manager, "_get_event_handler", return_value=handler),
        patch.object(
            manager._event_handlers,
            "_activate_materialized_session",
            return_value=[],
        ) as activate,
        patch.object(
            manager._event_handlers,
            "_inject_agent_instructions_if_needed",
        ),
        patch.object(manager, "_evaluate_workflow_rules", return_value=(None, None)),
        patch.object(manager, "_evaluate_blocking_webhooks", return_value=None),
        patch.object(manager, "_dispatch_webhooks_async"),
    ):
        response = manager.handle(event)

    assert response.decision == "allow"
    if should_materialize:
        assert isinstance(event.metadata.get("_platform_session_id"), str)
        activate.assert_called_once()
    else:
        assert "_platform_session_id" not in event.metadata
        activate.assert_not_called()


def test_first_pre_tool_use_without_ups_registers_session(
    hook_manager_with_mocks: HookManager,
    temp_dir: Path,
) -> None:
    manager = hook_manager_with_mocks
    external_id = "first-pre-tool-with-agent"
    assert manager.handle(_deferred_start_event(temp_dir, external_id)).decision == "allow"
    event = HookEvent(
        event_type=HookEventType.BEFORE_TOOL,
        session_id=external_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "Bash",
            "tool_input": {"command": "pwd"},
            "cwd": str(temp_dir),
        },
        machine_id=LOCAL_MACHINE_ID,
    )

    def inject_agent(
        _event: HookEvent,
        _session_id: str,
        response: HookResponse,
    ) -> None:
        response.context = "active-agent-instructions"

    with (
        patch.object(
            manager._event_handlers,
            "_activate_materialized_session",
            return_value=[("claimed_tasks", "claimed-task-context")],
        ),
        patch.object(
            manager._event_handlers,
            "_inject_agent_instructions_if_needed",
            side_effect=inject_agent,
        ) as inject,
        patch.object(manager, "_evaluate_workflow_rules", return_value=(None, None)),
        patch.object(manager, "_evaluate_blocking_webhooks", return_value=None),
        patch.object(manager, "_dispatch_webhooks_async"),
    ):
        response = manager.handle(event)

    assert response.decision == "allow"
    assert response.system_message is not None
    assert "active-agent-instructions" in (response.context or "")
    inject.assert_called_once()


def test_copied_session_start_uses_deferred_identity_schema(
    hook_manager_with_mocks: HookManager,
    temp_dir: Path,
) -> None:
    manager = hook_manager_with_mocks
    external_id = "copied-start-schema"
    assert manager.handle(_deferred_start_event(temp_dir, external_id)).decision == "allow"
    event = HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id=external_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={
            "prompt": "live prompt must not leak",
            "cwd": str(temp_dir),
            "transcript_path": str(temp_dir / "copied-start.jsonl"),
            "terminal_context": {"tmux_pane": "%77"},
        },
        machine_id=LOCAL_MACHINE_ID,
    )
    evaluated: list[HookEvent] = []

    def evaluate(
        copied_or_live: HookEvent,
        _deadline: BlockingEffectDeadline,
    ) -> tuple[list[ContextPart] | None, None]:
        evaluated.append(copied_or_live)
        if copied_or_live.metadata.get("_synthetic_session_start"):
            return [("rule:copied", "copied-rule-context")], None
        return None, None

    with (
        patch.object(
            manager,
            "_get_event_handler",
            return_value=lambda _event: HookResponse(
                decision="allow", context="live-handler-context"
            ),
        ),
        patch.object(
            manager._event_handlers,
            "_activate_materialized_session",
            return_value=[("claimed_tasks", "claimed-task-context")],
        ),
        patch.object(
            manager._event_handlers,
            "_inject_agent_instructions_if_needed",
        ),
        patch.object(manager, "_evaluate_workflow_rules", side_effect=evaluate),
        patch.object(manager, "_evaluate_blocking_webhooks", return_value=None),
        patch.object(manager, "_dispatch_webhooks_async") as dispatch,
    ):
        response = manager.handle(event)

    synthetic = evaluated[0]
    assert synthetic.event_type is HookEventType.SESSION_START
    assert synthetic.session_id == external_id
    assert synthetic.data == {
        "source": "startup",
        "cwd": str(temp_dir),
        "transcript_path": str(temp_dir / "copied-start.jsonl"),
        "terminal_context": {"tmux_pane": "%77", "cwd": str(temp_dir)},
    }
    assert synthetic.metadata == {
        "_platform_session_id": event.metadata["_platform_session_id"],
        "_synthetic_session_start": True,
    }
    assert event.event_type is HookEventType.BEFORE_AGENT
    expected = [
        ("claimed_tasks", "claimed-task-context"),
        ("rule:copied", "copied-rule-context"),
        ("response.context", "live-handler-context"),
    ]
    assert [part for part in response.context_contributors() if part in expected] == expected
    assert any(
        call.args[0].metadata.get("_synthetic_session_start") is True
        for call in dispatch.call_args_list
    )


def test_copied_session_start_rule_block_gates_live_response(
    hook_manager_with_mocks: HookManager,
    temp_dir: Path,
) -> None:
    manager = hook_manager_with_mocks
    external_id = "copied-start-block"
    assert manager.handle(_deferred_start_event(temp_dir, external_id)).decision == "allow"
    event = HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id=external_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"prompt": "blocked", "cwd": str(temp_dir)},
        machine_id=LOCAL_MACHINE_ID,
    )
    blocked = HookResponse(decision="block", reason="copied startup blocked")
    handler = MagicMock(return_value=HookResponse(decision="allow"))

    def evaluate(
        copied_or_live: HookEvent,
        _deadline: BlockingEffectDeadline,
    ) -> tuple[None, HookResponse | None]:
        if copied_or_live.metadata.get("_synthetic_session_start"):
            return None, blocked
        pytest.fail("live rules ran after copied SessionStart blocked")

    with (
        patch.object(manager, "_get_event_handler", return_value=handler),
        patch.object(
            manager._event_handlers,
            "_activate_materialized_session",
            return_value=[],
        ),
        patch.object(manager, "_evaluate_workflow_rules", side_effect=evaluate),
        patch.object(manager, "_dispatch_webhooks_async"),
    ):
        response = manager.handle(event)

    assert response is blocked
    assert response.decision == "block"
    assert response.reason == "copied startup blocked"
    handler.assert_not_called()


def test_concurrent_first_hooks_materialize_and_copy_once(
    hook_manager_with_mocks: HookManager,
    temp_dir: Path,
) -> None:
    manager = hook_manager_with_mocks
    external_id = "concurrent-first-hooks"
    assert manager.handle(_deferred_start_event(temp_dir, external_id)).decision == "allow"
    barrier = threading.Barrier(2)
    responses: list[HookResponse] = []
    evaluated: list[HookEvent] = []

    def handle(event_type: HookEventType) -> None:
        event = HookEvent(
            event_type=event_type,
            session_id=external_id,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={"cwd": str(temp_dir)},
            machine_id=LOCAL_MACHINE_ID,
        )
        barrier.wait(timeout=5)
        responses.append(manager.handle(event))

    def evaluate(event: HookEvent, _deadline: BlockingEffectDeadline) -> tuple[None, None]:
        evaluated.append(event)
        return None, None

    handler = MagicMock(return_value=HookResponse(decision="allow"))
    with (
        patch.object(manager, "_get_event_handler", return_value=handler),
        patch.object(
            manager._event_handlers,
            "_activate_materialized_session",
            return_value=[],
        ) as activate,
        patch.object(
            manager._event_handlers,
            "_inject_agent_instructions_if_needed",
        ),
        patch.object(manager, "_evaluate_workflow_rules", side_effect=evaluate),
        patch.object(manager, "_evaluate_blocking_webhooks", return_value=None),
        patch.object(manager, "_dispatch_webhooks_async"),
        patch.object(
            manager._session_manager,
            "register_session",
            wraps=manager._session_manager.register_session,
        ) as register,
    ):
        first = threading.Thread(target=handle, args=(HookEventType.BEFORE_AGENT,))
        second = threading.Thread(target=handle, args=(HookEventType.BEFORE_TOOL,))
        first.start()
        second.start()
        first.join(timeout=5)
        second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert [response.decision for response in responses] == ["allow", "allow"]
    register.assert_called_once()
    activate.assert_called_once()
    assert sum(bool(event.metadata.get("_synthetic_session_start")) for event in evaluated) == 1


def test_activation_failure_repairs_without_replaying_startup_packet(
    hook_manager_with_mocks: HookManager,
    temp_dir: Path,
) -> None:
    manager = hook_manager_with_mocks
    external_id = "activation-repair"
    assert manager.handle(_deferred_start_event(temp_dir, external_id)).decision == "allow"
    first = HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id=external_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"prompt": "first", "cwd": str(temp_dir)},
        machine_id=LOCAL_MACHINE_ID,
    )

    with patch.object(
        manager._event_handlers,
        "_activate_materialized_session",
        side_effect=RuntimeError("activation crashed"),
    ):
        failed = manager.handle(first)

    assert failed.decision == "allow"
    assert failed.reason == "Handler error: activation crashed"
    assert first.metadata.get("_platform_session_id")

    retry = HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id=external_id,
        source=SessionSource.CLAUDE,
        timestamp=datetime.now(UTC),
        data={"prompt": "retry", "cwd": str(temp_dir)},
        machine_id=LOCAL_MACHINE_ID,
    )
    with patch("gobby.hooks.hook_manager.reconcile_session_activation") as reconcile:
        repaired = manager.handle(retry)

    assert repaired.decision == "allow"
    assert repaired.system_message is None
    assert "_startup_context" not in retry.metadata
    reconcile.assert_called_once()


@pytest.mark.parametrize(
    ("source", "native_hook", "delivery"),
    [
        (SessionSource.CLAUDE, "user-prompt-submit", "split"),
        (SessionSource.DROID, "UserPromptSubmit", "split"),
        (SessionSource.CODEX, "UserPromptSubmit", "combined"),
        (SessionSource.AGY, "PreInvocation", "agy"),
        (SessionSource.GROK, "user_prompt_submit", "drop"),
    ],
)
def test_first_activity_startup_context_provider_matrix(
    hook_manager_with_mocks: HookManager,
    temp_dir: Path,
    source: SessionSource,
    native_hook: str,
    delivery: str,
) -> None:
    from gobby.adapters.agy import AgyAdapter
    from gobby.adapters.claude_code import ClaudeCodeAdapter
    from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter
    from gobby.adapters.droid import DroidAdapter
    from gobby.adapters.grok import GrokAdapter

    adapters = {
        SessionSource.CLAUDE: ClaudeCodeAdapter(),
        SessionSource.DROID: DroidAdapter(),
        SessionSource.CODEX: CodexHooksAdapter(),
        SessionSource.AGY: AgyAdapter(),
        SessionSource.GROK: GrokAdapter(),
    }
    manager = hook_manager_with_mocks
    external_id = f"provider-{source.value}"
    assert manager.handle(_deferred_start_event(temp_dir, external_id, source)).decision == "allow"
    event = HookEvent(
        event_type=HookEventType.BEFORE_AGENT,
        session_id=external_id,
        source=source,
        timestamp=datetime.now(UTC),
        data={"prompt": "hello", "cwd": str(temp_dir)},
        machine_id=LOCAL_MACHINE_ID,
    )

    def evaluate(
        copied_or_live: HookEvent,
        _deadline: BlockingEffectDeadline,
    ) -> tuple[list[ContextPart] | None, None]:
        if copied_or_live.metadata.get("_synthetic_session_start"):
            return [("rule:copied", "copied-rule-context")], None
        return None, None

    with (
        patch.object(
            manager._event_handlers,
            "_activate_materialized_session",
            return_value=[("claimed_tasks", "claimed-task-context")],
        ),
        patch.object(
            manager._event_handlers,
            "_inject_agent_instructions_if_needed",
        ),
        patch.object(manager, "_evaluate_workflow_rules", side_effect=evaluate),
        patch.object(manager, "_evaluate_blocking_webhooks", return_value=None),
        patch.object(manager, "_dispatch_webhooks_async"),
    ):
        response = manager.handle(event)

    if delivery == "drop":
        assert response.system_message is None
        assert response.context is None
    else:
        assert response.system_message is not None
        assert "claimed-task-context" in (response.context or "")
        assert "copied-rule-context" in (response.context or "")
    native = cast(Any, adapters[source]).translate_from_hook_response(
        response,
        hook_type=native_hook,
    )
    rendered = json.dumps(native)

    if delivery == "split":
        assert "Gobby Session ID:" in native["systemMessage"]
        assert "claimed-task-context" in native["hookSpecificOutput"]["additionalContext"]
    elif delivery == "combined":
        assert "systemMessage" not in native
        context = native["hookSpecificOutput"]["additionalContext"]
        assert "Gobby Session ID:" in context
        assert "copied-rule-context" in context
    elif delivery == "agy":
        assert "claimed-task-context" in rendered
        assert "Gobby Session ID:" in rendered
    else:
        assert "claimed-task-context" not in rendered
        assert "copied-rule-context" not in rendered
        assert "Gobby Session ID:" not in rendered


def test_codex_successor_after_a_slow_login_binds_its_existing_pane(
    hook_manager_with_mocks: HookManager,
    temp_dir: Path,
) -> None:
    """#23032: a successor seat that waited 188 s at login still binds its Gobby pane.

    The pane is still bound to the expired predecessor, another live seat in the same
    cwd is owned by its own session, and the new TUI minted its thread long after the
    60 s fresh-seat window, so only the database ownership check can name the seat.
    """
    from gobby.hooks.terminal_context import clear_codex_seat_index
    from gobby.storage.terminals import TerminalManager

    manager = hook_manager_with_mocks
    sessions = manager._session_manager
    terminals = TerminalManager(sessions.db)
    manager._event_handlers.terminal_manager = terminals
    project = LocalProjectManager(sessions.db).get_by_name("test-project")
    assert project is not None
    pane_id = str(uuid.uuid4())
    terminals.create_pending(
        terminal_id=pane_id,
        project_id=project.id,
        backend="native",
        ownership="gobby",
        spawn_key=pane_id,
        machine_id=LOCAL_MACHINE_ID,
    )

    def tui(pid: int, cmdline: list[str], create_time: float, terminal_id: str) -> MagicMock:
        mock = MagicMock()
        mock.pid = pid
        mock.info = {"name": "codex"}
        mock.name.return_value = "codex"
        mock.cmdline.return_value = cmdline
        mock.create_time.return_value = create_time
        mock.parent.return_value = None
        mock.cwd.return_value = str(temp_dir)
        mock.terminal.return_value = "/dev/ttys007"
        mock.environ.return_value = {"GOBBY_TERMINAL_ID": terminal_id}
        return mock

    minted_at = time.time()
    successor_thread = str(
        uuid.UUID(int=(int(minted_at * 1000) << 80) | (7 << 76) | (0x2 << 62) | 0x1234)
    )
    predecessor_id = sessions.register_session(
        external_id="01a0d6a4-4800-7d90-a5d8-3b751ad44281",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=project.id,
        terminal_context={
            "cwd": str(temp_dir),
            "parent_pid": 22512,
            "parent_create_time": minted_at - 7200,
            "gobby_terminal_id": pane_id,
        },
    )
    assert terminals.bind_session(pane_id, predecessor_id, project.id) is not None
    sessions.update_status(predecessor_id, "expired")
    neighbour = tui(23170, ["codex", "--yolo"], minted_at - 600, str(uuid.uuid4()))
    sessions.register_session(
        external_id="01a0d723-8589-75d2-adb9-9263ee2bd8bf",
        machine_id=LOCAL_MACHINE_ID,
        source="codex",
        project_id=project.id,
        terminal_context={
            "cwd": str(temp_dir),
            "parent_pid": neighbour.pid,
            "parent_create_time": minted_at - 600,
        },
    )
    host = tui(93395, ["codex", "app-server", "--listen", "unix://", "--managed-daemon"], 50.0, "")
    table = [host, neighbour, tui(12856, ["codex", "--yolo"], minted_at - 188, pane_id)]

    def lookup(pid: int) -> MagicMock:
        for candidate in table:
            if candidate.pid == pid:
                return candidate
        raise psutil.NoSuchProcess(pid)

    from gobby.adapters.codex_impl.hooks_adapter import CodexHooksAdapter

    # The shared host's own identity: its pid, and whichever pane first started it.
    host_context = {"parent_pid": host.pid, "gobby_terminal_id": str(uuid.uuid4())}
    start = HookEvent(
        event_type=HookEventType.SESSION_START,
        session_id=successor_thread,
        source=SessionSource.CODEX,
        timestamp=datetime.now(UTC),
        data={"source": "startup", "cwd": str(temp_dir), "terminal_context": host_context},
        machine_id=LOCAL_MACHINE_ID,
    )
    prompt = CodexHooksAdapter().translate_to_hook_event(
        {
            "hook_type": "UserPromptSubmit",
            "input_data": {
                "session_id": successor_thread,
                "prompt": "Call get_handoff() on gobby-sessions.",
                "cwd": str(temp_dir),
                "terminal_context": dict(host_context),
                "machine_id": LOCAL_MACHINE_ID,
            },
            "source": "codex",
        }
    )
    assert prompt is not None
    clear_codex_seat_index()
    try:
        with (
            patch("gobby.hooks.event_handlers._session_start.schedule_tmux_window_rename"),
            patch("gobby.sessions.tmux_window_naming.schedule_tmux_window_rename"),
            patch("gobby.hooks.terminal_context.psutil.Process", side_effect=lookup),
            patch(
                "gobby.hooks.terminal_context.psutil.process_iter",
                side_effect=lambda **_: list(table),
            ),
        ):
            assert manager.handle(start).decision == "allow"
            assert manager.handle(prompt).decision == "allow"
    finally:
        clear_codex_seat_index()

    successor = sessions.get(prompt.metadata["_platform_session_id"])
    assert successor is not None
    assert successor.external_id == successor_thread
    assert successor.terminal_context is not None
    assert successor.terminal_context["parent_pid"] == 12856
    assert successor.terminal_context["gobby_terminal_id"] == pane_id
    bound = sessions.db.fetchall("SELECT id FROM terminals WHERE session_id = %s", (successor.id,))
    assert [str(row["id"]) for row in bound] == [pane_id]
    predecessor = sessions.get(predecessor_id)
    assert predecessor is not None
    assert predecessor.status == "expired"

    sessions.update_status(successor.id, "paused")
    paused = sessions.get(successor.id)
    assert paused is not None
    live = terminals.resolve_live_for_session(paused)
    assert live is not None
    assert live.id == pane_id


def test_session_resolution_reads_the_session_row_once(
    hook_manager_with_mocks: HookManager,
    sample_session_start_event: HookEvent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A steady-state hook resolves its session from one row read (#23063).

    Every read is a pooled round trip, and under hook fanout each costs a pool
    checkout plus a slow query, so re-reading the row already loaded to validate
    the cached mapping multiplies session_resolution latency.
    """
    manager = hook_manager_with_mocks
    manager.handle(sample_session_start_event)

    resolving = threading.Event()

    def flag_resolution[**P, R](function: Callable[P, R]) -> Callable[P, R]:
        def resolve(*args: P.args, **kwargs: P.kwargs) -> R:
            resolving.set()
            try:
                return function(*args, **kwargs)
            finally:
                resolving.clear()

        return resolve

    reads: list[str] = []
    original_get = SessionManager.get

    def counting_get(self: SessionManager, session_id: str) -> Any:
        if resolving.is_set():
            reads.append(session_id)
        return original_get(self, session_id)

    monkeypatch.setattr(
        "gobby.hooks.hook_manager.resolve_hook_project_context",
        flag_resolution(resolve_hook_project_context),
    )
    monkeypatch.setattr(
        manager._session_lookup, "resolve", flag_resolution(manager._session_lookup.resolve)
    )
    monkeypatch.setattr(SessionManager, "get", counting_get)

    def after_tool() -> HookEvent:
        return HookEvent(
            event_type=HookEventType.AFTER_TOOL,
            session_id=sample_session_start_event.session_id,
            source=SessionSource.CLAUDE,
            timestamp=datetime.now(UTC),
            data={
                "tool_name": "Read",
                "tool_output": "x",
                "cwd": sample_session_start_event.cwd,
                "terminal_context": {"gobby_terminal_id": "t-23063", "parent_pid": 4242},
            },
            machine_id=manager.get_machine_id(),
        )

    # The first hook backfills the new terminal context; the next is steady state.
    manager.handle(after_tool())
    reads.clear()
    event = after_tool()
    manager.handle(event)

    assert reads == [event.metadata["_platform_session_id"]]
