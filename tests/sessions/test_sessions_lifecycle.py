# mypy: disable-error-code="no-untyped-def,no-untyped-call,assignment,attr-defined,union-attr"
import asyncio
import json
import os
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, TypeVar, cast
from unittest.mock import AsyncMock, MagicMock, call, patch

import psycopg
import pytest

from gobby.config.features import KnowledgeGraphQueueConfig
from gobby.config.persistence import MemoryDreamConfig
from gobby.config.runtime import RuntimeActiveBundle
from gobby.config.sessions import SessionLifecycleConfig
from gobby.sessions import lifecycle as lifecycle_mod
from gobby.sessions.lifecycle import SessionLifecycleManager
from gobby.sessions.transcript_index import load_index_sidecar
from gobby.storage.session_models import Session
from tests.config_runtime_helpers import static_session_capture

pytestmark = pytest.mark.unit
T = TypeVar("T")

_SESSION_MANAGER_PATCH = "gobby.sessions.lifecycle.SessionManager"
DROID_FIXTURE_DIR = Path(__file__).parent / "transcripts" / "fixtures" / "droid"
DROID_FIXTURE_JSONL = DROID_FIXTURE_DIR / "dbf95187-5fa4-43a0-b207-8c24f412baf7.jsonl"
DROID_FIXTURE_SETTINGS = DROID_FIXTURE_DIR / "dbf95187-5fa4-43a0-b207-8c24f412baf7.settings.json"
AGY_STATS_FIXTURE = Path(__file__).parent / "fixtures" / "agy" / "print_tool_calls_terminal.jsonl"


class EmptyTokenEventStore:
    def delete_session_events(self, _session_id: str, *, origin: str) -> None:
        _ = origin

    def get_session_totals(self, _session_id: str) -> dict[str, int]:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
        }

    def record_batch(self, events: list[object]) -> list[bool]:
        return [False for _ in events]


class InsertingTokenEventStore(EmptyTokenEventStore):
    def record_batch(self, events: list[object]) -> list[bool]:
        return [True for _ in events]


def _usage_message(message_id: str, input_tokens: int, output_tokens: int) -> MagicMock:
    from gobby.sessions.transcripts.base import ParsedMessage, TokenUsage

    msg = MagicMock(spec=ParsedMessage)
    msg.role = "assistant"
    msg.content_type = "text"
    msg.content = f"reply {message_id}"
    msg.tool_name = None
    msg.model = "claude-sonnet-4-6"
    msg.raw_json = {}
    msg.usage = TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens)
    msg.timestamp = None
    msg.message_id = message_id
    return msg


def _set_llm_service(manager: SessionLifecycleManager, llm: Any) -> Any:
    """Swap the ai_services entry in the manager's captured runtime bundle.

    The bundle's services mapping is an immutable ``MappingProxyType``, so the
    swap republishes a bundle with the amended services, mirroring a subscriber
    rebuild.
    """
    bundle = manager._capture_bundle()
    services = dict(bundle.services)
    if llm is None:
        services.pop("ai_services", None)
    else:
        services["ai_services"] = SimpleNamespace(llm_service=llm)
    new_bundle = RuntimeActiveBundle(
        snapshot=bundle.snapshot,
        services=MappingProxyType(services),
    )
    manager._capture_bundle = lambda: new_bundle
    return llm


def _memory_services(memory_manager: Any) -> dict[str, object]:
    """Build a runtime-services mapping exposing one memory manager."""
    return {"memory_services": SimpleNamespace(memory_manager=memory_manager)}


@pytest.fixture
def mock_db() -> MagicMock:
    return MagicMock()


@pytest.fixture
def mock_config() -> SessionLifecycleConfig:
    return SessionLifecycleConfig(
        expire_check_interval_minutes=1,
        transcript_processing_interval_minutes=1,
        active_session_pause_minutes=30,
        stale_session_timeout_hours=24,
        transcript_processing_batch_size=10,
    )


@pytest.fixture
def manager(mock_db: MagicMock, mock_config: SessionLifecycleConfig) -> SessionLifecycleManager:
    with patch(_SESSION_MANAGER_PATCH):
        return SessionLifecycleManager(mock_db, static_session_capture(mock_config))


def _pending_session(session_id: str = "s1") -> MagicMock:
    session = MagicMock(spec=Session)
    session.id = session_id
    session.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    session.last_activity = session.created_at
    session.status = "expired"
    session.transcript_path = None
    session.external_id = f"ext-{session_id}"
    session.source = "claude"
    session.agent_depth = 0
    session.turn_count = 3
    return session


class TestSessionLifecycleManager:
    """Tests for SessionLifecycleManager."""

    @pytest.mark.asyncio
    async def test_start_creates_background_tasks(self, manager: SessionLifecycleManager) -> None:
        """Test that start() creates background tasks."""
        await manager.start()

        assert manager._running is True
        assert manager._expire_task is not None
        assert manager._process_task is not None
        assert not manager._expire_task.done()
        assert not manager._process_task.done()

        # Clean stop
        await manager.stop()
        assert manager._running is False
        assert manager._expire_task is None

    @pytest.mark.asyncio
    async def test_stop_cancels_background_tasks(
        self,
        manager: SessionLifecycleManager,
    ) -> None:
        """Test that stop() cancels tasks."""
        await manager.start()

        expire_task = manager._expire_task
        process_task = manager._process_task

        await manager.stop()

        assert expire_task.cancelled() or expire_task.done()
        assert process_task.cancelled() or process_task.done()

    @pytest.mark.asyncio
    async def test_stop_returns_when_cancelled_task_is_slow_to_drain(
        self,
        manager: SessionLifecycleManager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Shutdown should not block on best-effort background work."""

        async def slow_to_cancel() -> None:
            await asyncio.Event().wait()

        async def fake_wait(
            tasks: set[asyncio.Task[None]],
            timeout: float | None,
        ) -> tuple[set[asyncio.Task[None]], set[asyncio.Task[None]]]:
            assert timeout == 0.01
            return set(), set(tasks)

        monkeypatch.setattr(asyncio, "wait", fake_wait)

        task = asyncio.create_task(slow_to_cancel())
        manager._running = True
        manager._expire_task = task

        await asyncio.wait_for(manager.stop(drain_timeout=0.01), timeout=0.2)

        assert manager._running is False
        assert manager._expire_task is None

        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_expire_stale_sessions(self, manager: SessionLifecycleManager) -> None:
        """Test expiring stale sessions."""
        # Setup mocks
        manager.session_manager.pause_inactive_active_sessions.return_value = 2
        manager.session_manager.expire_orphaned_handoff_sessions.return_value = 1
        manager.session_manager.expire_stale_sessions.return_value = 3
        manager.session_manager.expire_empty_sessions.return_value = 4
        manager.session_manager.prune_empty_sessions.return_value = 5
        manager.session_manager.prune_stale_compact_workflow_instances.return_value = 7
        manager.session_manager.cleanup_expired_session_state.return_value = None

        count = await manager._expire_stale_sessions(manager._capture_active().session_lifecycle)

        assert count == 15
        manager.session_manager.pause_inactive_active_sessions.assert_called_once_with(
            timeout_minutes=manager._capture_active().session_lifecycle.active_session_pause_minutes
        )
        manager.session_manager.expire_orphaned_handoff_sessions.assert_called_once_with(
            timeout_minutes=30
        )
        manager.session_manager.prune_stale_compact_workflow_instances.assert_called_once_with(
            retention_hours=24
        )
        manager.session_manager.cleanup_expired_session_state.assert_called_once_with()
        manager.session_manager.expire_stale_sessions.assert_called_once_with(
            timeout_hours=manager._capture_active().session_lifecycle.stale_session_timeout_hours
        )
        manager.session_manager.expire_empty_sessions.assert_called_once_with(timeout_hours=2)
        manager.session_manager.prune_empty_sessions.assert_called_once_with(min_age_hours=1)

    @pytest.mark.asyncio
    async def test_expire_stale_sessions_retires_hook_effects(
        self, manager: SessionLifecycleManager
    ) -> None:
        manager.session_manager.pause_inactive_active_sessions.return_value = 0
        manager.session_manager.expire_orphaned_handoff_sessions.return_value = 0
        manager.session_manager.expire_stale_sessions.return_value = 1
        manager.session_manager.expire_empty_sessions.return_value = 0
        manager.session_manager.prune_empty_sessions.return_value = 0
        manager.session_manager.prune_stale_compact_workflow_instances.return_value = 0
        manager.session_manager.cleanup_expired_session_state.return_value = None

        assert callable(getattr(lifecycle_mod, "retire_expired_session_hook_effects", None))
        with patch.object(lifecycle_mod, "retire_expired_session_hook_effects") as retire:
            await manager._expire_stale_sessions(manager._capture_active().session_lifecycle)

        retire.assert_called_once()
        assert retire.call_args.args[0] is manager.session_manager.db

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error",
        [ValueError("bad stats"), psycopg.OperationalError("db unavailable")],
    )
    async def test_process_session_transcript_logs_known_stats_errors(
        self,
        manager: SessionLifecycleManager,
        tmp_path: Path,
        error: Exception,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"type":"user","message":{"role":"user","content":"hello"}}\n',
            encoding="utf-8",
        )
        manager.token_event_store = EmptyTokenEventStore()
        session = SimpleNamespace(
            source="claude",
            project_id="proj-1",
            context_window=None,
            model=None,
            usage_input_tokens=0,
            usage_output_tokens=0,
            usage_cache_creation_tokens=0,
            usage_cache_read_tokens=0,
        )
        manager.session_manager.get.return_value = session
        manager.session_manager.update_stats.side_effect = error

        with caplog.at_level("WARNING", logger="gobby.sessions.lifecycle"):
            await manager._process_session_transcript("session-1", str(transcript))

        assert any(getattr(record, "session_id", None) == "session-1" for record in caplog.records)
        manager.session_manager.update_usage.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_session_transcript_propagates_unexpected_stats_errors(
        self,
        manager: SessionLifecycleManager,
        tmp_path: Path,
    ) -> None:
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            '{"type":"user","message":{"role":"user","content":"hello"}}\n',
            encoding="utf-8",
        )
        manager.token_event_store = EmptyTokenEventStore()
        manager.session_manager.get.return_value = SimpleNamespace(source="claude")
        manager.session_manager.update_stats.side_effect = RuntimeError("bug")

        with pytest.raises(RuntimeError, match="bug"):
            await manager._process_session_transcript("session-1", str(transcript))

    @pytest.mark.asyncio
    async def test_process_pending_transcripts_none_found(
        self, manager: SessionLifecycleManager
    ) -> None:
        """Test processing when no sessions pending."""
        manager.session_manager.get_pending_transcript_sessions.return_value = []

        processed = await manager._process_pending_transcripts(manager._capture_active())

        assert processed == 0
        manager.session_manager.mark_transcript_processed.assert_not_called()

    @pytest.mark.asyncio
    async def test_pending_transcript_query_runs_off_the_event_loop_thread(
        self, manager: SessionLifecycleManager
    ) -> None:
        """The query is synchronous psycopg, and a pool checkout also runs its
        runtime-role round trip inline. The sampler caught this chain at 40% of
        a 2.44s loop stall (#20845)."""
        query_threads: list[int] = []

        def _record(**_kwargs: object) -> list[object]:
            query_threads.append(threading.get_ident())
            return []

        manager.session_manager.get_pending_transcript_sessions.side_effect = _record
        loop_thread = threading.get_ident()

        await manager._process_pending_transcripts(manager._capture_active())

        assert query_threads, "the pending-transcript query never ran"
        assert loop_thread not in query_threads, (
            "the pending-transcript query ran on the event loop thread"
        )

    @pytest.mark.asyncio
    async def test_transcript_token_event_db_work_runs_off_the_event_loop_thread(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """The token-event path is synchronous psycopg reached from the loop
        thread; per-event record() alone was 72% of the hot samples in a
        10.61s loop stall (#20885). Every store call must run off-loop."""
        from gobby.sessions.transcripts.base import ParsedMessage, TokenUsage

        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')

        session = MagicMock()
        session.source = "claude"
        session.project_id = "proj-1"
        session.context_window = None
        session.model = None
        manager.session_manager.get.return_value = session

        db_threads: dict[str, list[int]] = {
            "delete_session_events": [],
            "get_session_totals": [],
            "record_batch": [],
        }

        class _ThreadRecordingStore:
            def delete_session_events(self, _session_id: str, *, origin: str) -> int:
                _ = origin
                db_threads["delete_session_events"].append(threading.get_ident())
                return 0

            def get_session_totals(self, _session_id: str) -> dict[str, int]:
                db_threads["get_session_totals"].append(threading.get_ident())
                return {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 0,
                }

            def record_batch(self, events: list[object]) -> list[bool]:
                db_threads["record_batch"].append(threading.get_ident())
                return [True] * len(events)

        manager.token_event_store = _ThreadRecordingStore()

        msg = MagicMock(spec=ParsedMessage)
        msg.model = "claude-sonnet-4-6"
        msg.raw_json = {}
        msg.usage = TokenUsage(input_tokens=11, output_tokens=7)
        msg.timestamp = None
        msg.message_id = "msg-1"
        msg.content_type = "text"
        loop_thread = threading.get_ident()

        with patch("gobby.sessions.transcript_processing.get_parser") as parser:
            parser.return_value.parse_lines.return_value = [msg]
            await manager._process_session_transcript("s1", str(transcript_path))

        for method, threads in db_threads.items():
            assert threads, f"{method} never ran"
            assert loop_thread not in threads, f"{method} ran on the event loop thread"

    @pytest.mark.asyncio
    async def test_transcript_parse_and_session_writes_run_off_the_event_loop_thread(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """An expired session's transcript can be hundreds of megabytes; parsing
        it and writing its stats on the loop thread wedged hooks, HTTP and
        terminal input for minutes after a gterm host restart (#22811)."""
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')
        threads: dict[str, list[int]] = {}

        def _recording(name: str, result: object = None) -> Callable[..., object]:
            def _record(*_args: object, **_kwargs: object) -> object:
                threads.setdefault(name, []).append(threading.get_ident())
                return result

            return _record

        session = MagicMock()
        session.source = "claude"
        session.project_id = "proj-1"
        session.context_window = None
        session.model = None
        manager.session_manager.get.side_effect = _recording("get", session)
        manager.session_manager.update_stats.side_effect = _recording("update_stats")
        manager.session_manager.update_usage.side_effect = _recording("update_usage")
        manager.token_event_store = InsertingTokenEventStore()
        loop_thread = threading.get_ident()

        with patch("gobby.sessions.transcript_processing.get_parser") as parser:
            parser.return_value.parse_lines.side_effect = _recording(
                "parse_lines", [_usage_message("msg-1", 11, 7)]
            )
            await manager._process_session_transcript("s1", str(transcript_path))

        for name in ("get", "parse_lines", "update_stats", "update_usage"):
            assert threads.get(name), f"{name} never ran"
            assert loop_thread not in threads[name], f"{name} ran on the event loop thread"

    @pytest.mark.asyncio
    async def test_transcript_broadcasts_carry_per_event_running_totals(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """Payloads are built off-loop and broadcast afterwards; each keeps
        message order and the totals as of its own event, not the final totals."""
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')

        session = MagicMock()
        session.source = "claude"
        session.project_id = "proj-1"
        session.context_window = None
        session.model = None
        manager.session_manager.get.return_value = session
        manager.token_event_store = InsertingTokenEventStore()
        ws_server = SimpleNamespace(broadcast_token_event=AsyncMock())

        with (
            patch("gobby.sessions.transcript_processing.get_parser") as parser,
            patch(
                "gobby.sessions.transcript_processing.get_app_context",
                return_value=SimpleNamespace(websocket_server=ws_server),
            ),
        ):
            parser.return_value.parse_lines.return_value = [
                _usage_message("msg-1", 11, 7),
                _usage_message("msg-2", 5, 3),
            ]
            await manager._process_session_transcript("s1", str(transcript_path))

        payloads = [c.args[0] for c in ws_server.broadcast_token_event.await_args_list]
        assert [p["message_id"] for p in payloads] == ["msg-1", "msg-2"]
        assert [p["session_totals"]["input_tokens"] for p in payloads] == [11, 16]
        assert [p["session_totals"]["output_tokens"] for p in payloads] == [7, 10]

    @pytest.mark.asyncio
    async def test_transcript_context_reconciliation_runs_once_per_distinct_input(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """Each reconcile_model_context call builds a resolver that reads the
        database; an 86 MB transcript made 16,628 calls over 14 distinct
        inputs, so one pass reconciles each input once (#22811)."""
        from gobby.llm.context_windows import reconcile_model_context

        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')

        session = MagicMock()
        session.source = "claude"
        session.project_id = "proj-1"
        session.context_window = None
        session.model = None
        manager.session_manager.get.return_value = session
        manager.token_event_store = InsertingTokenEventStore()
        reconcile_inputs: list[tuple[object, ...]] = []

        def _counting(*args: Any, **kwargs: Any) -> Any:
            reconcile_inputs.append(args)
            return reconcile_model_context(*args, **kwargs)

        with (
            patch("gobby.sessions.transcript_processing.get_parser") as parser,
            patch(
                "gobby.sessions.transcript_processing.reconcile_model_context",
                side_effect=_counting,
            ),
        ):
            parser.return_value.parse_lines.return_value = [
                _usage_message(f"msg-{index}", 10, 5) for index in range(6)
            ]
            await manager._process_session_transcript("s1", str(transcript_path))

        assert reconcile_inputs, "reconcile_model_context never ran"
        assert len(reconcile_inputs) == len(set(reconcile_inputs))
        assert len(reconcile_inputs) < 6

    @pytest.mark.asyncio
    async def test_transcript_token_events_are_recorded_in_batches_not_per_event(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """A transcript pass with many usage events issues a bounded number of
        store operations — one record_batch call plus the fixed delete/totals
        bookkeeping — never one insert per event (#20885)."""
        from gobby.sessions.transcripts.base import ParsedMessage, TokenUsage

        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')

        session = MagicMock()
        session.source = "claude"
        session.project_id = "proj-1"
        session.context_window = None
        session.model = None
        manager.session_manager.get.return_value = session

        class _CountingStore:
            """Intentionally has no record(): a per-event insert fails loudly."""

            def __init__(self) -> None:
                self.calls: list[str] = []
                self.batch_sizes: list[int] = []

            def delete_session_events(self, _session_id: str, *, origin: str) -> int:
                _ = origin
                self.calls.append("delete_session_events")
                return 0

            def get_session_totals(self, _session_id: str) -> dict[str, int]:
                self.calls.append("get_session_totals")
                return {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 0,
                }

            def record_batch(self, events: list[object]) -> list[bool]:
                self.calls.append("record_batch")
                self.batch_sizes.append(len(events))
                return [True] * len(events)

        store = _CountingStore()
        manager.token_event_store = store

        messages = []
        for i in range(40):
            msg = MagicMock(spec=ParsedMessage)
            msg.model = "claude-sonnet-4-6"
            msg.raw_json = {}
            msg.usage = TokenUsage(input_tokens=10 + i, output_tokens=5)
            msg.timestamp = None
            msg.message_id = f"msg-{i}"
            msg.content_type = "text"
            messages.append(msg)

        with patch("gobby.sessions.transcript_processing.get_parser") as parser:
            parser.return_value.parse_lines.return_value = messages
            await manager._process_session_transcript("s1", str(transcript_path))

        assert store.batch_sizes == [40]
        # Two origin-scoped deletes, two totals reads, one batched insert:
        # bounded bookkeeping, independent of the 40 events.
        assert store.calls.count("record_batch") == 1
        assert len(store.calls) == 5

    async def test_process_pending_transcripts_success(
        self, manager: SessionLifecycleManager
    ) -> None:
        session = _pending_session()
        manager.session_manager.get_pending_transcript_sessions.return_value = [session]
        manager.session_manager.get.return_value = session
        with (
            patch.object(manager, "_process_session_transcript", new_callable=AsyncMock) as parse,
            patch.object(
                manager,
                "_generate_artifacts_if_needed",
                new_callable=AsyncMock,
                return_value={"success": True, "source_context_hash": "current"},
            ),
        ):
            assert await manager._process_pending_transcripts(manager._capture_active()) == 1
        parse.assert_awaited_once_with("s1", None)
        manager.session_manager.mark_transcript_processed.assert_called_once_with(
            "s1",
            expected_session=session,
            source_hash="current",
        )

    @pytest.mark.asyncio
    async def test_digestless_crash_uses_refreshed_turn_count(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """Parsed transcript stats decide whether a crash session needs artifacts."""
        from gobby.sessions.transcripts.base import ParsedMessage

        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')

        session = _pending_session()
        session.id = "s1"
        session.transcript_path = str(transcript_path)
        session.external_id = "ext-s1"
        session.agent_depth = 0
        session.source = "claude"
        session.handoff_markdown = None
        session.turn_count = 0
        session.summary_markdown = "valid summary"
        session.project_id = None
        session.context_window = None
        session.model = None
        manager.session_manager.get_pending_transcript_sessions.return_value = [session]
        manager.session_manager.get.return_value = session
        _set_llm_service(manager, MagicMock())

        messages = []
        for content in ("First", "Second", "Third"):
            message = MagicMock(spec=ParsedMessage)
            message.role = "assistant"
            message.content_type = "text"
            message.content = content
            message.tool_name = None
            message.model = None
            message.usage = None
            messages.append(message)

        def update_stats(_session_id: str, **stats: Any) -> None:
            session.turn_count = stats["turn_count"]

        manager.session_manager.update_stats.side_effect = update_stats

        with (
            patch("gobby.sessions.transcript_processing.get_parser") as mock_parser,
            patch.object(
                manager,
                "_generate_artifacts_if_needed",
                new_callable=AsyncMock,
                return_value={"success": True},
            ) as mock_generate,
            patch("gobby.sessions.transcript_processing.rebuild_and_persist_index"),
            patch("gobby.sessions.transcript_processing.backup_transcript", return_value=None),
            patch(
                "gobby.sessions.transcript_processing.is_summary_markdown_valid", return_value=True
            ),
        ):
            mock_parser.return_value.parse_lines.return_value = messages

            processed = await manager._process_pending_transcripts(manager._capture_active())

        manager.session_manager.update_stats.assert_called_once_with(
            "s1",
            message_count=3,
            turn_count=3,
            tool_call_count=0,
            last_assistant_content="Third",
        )
        assert session.turn_count == 3
        mock_generate.assert_awaited_once_with(
            "s1",
            manager._capture_active().session_summary,
            allow_llm=True,
            archive_dir=manager._capture_active().session_lifecycle.transcript_archive_dir,
        )
        assert processed == 1

    async def test_process_pending_transcripts_skips_subagent_sessions(
        self, manager: SessionLifecycleManager
    ) -> None:
        session = _pending_session()
        session.source = "claude"
        session.agent_depth = 1
        manager.session_manager.get_pending_transcript_sessions.return_value = [session]
        manager.session_manager.get.return_value = session
        with (
            patch.object(manager, "_process_session_transcript", new_callable=AsyncMock),
            patch.object(
                manager,
                "_generate_artifacts_if_needed",
                new_callable=AsyncMock,
                return_value={"success": False, "error": "Transcript file not found"},
            ) as generate,
        ):
            assert await manager._process_pending_transcripts(manager._capture_active()) == 0
        assert generate.await_args.kwargs["allow_llm"] is False
        manager.session_manager.mark_transcript_processed.assert_not_called()

    async def test_process_pending_transcripts_skips_pipeline_sessions(
        self, manager: SessionLifecycleManager
    ) -> None:
        session = _pending_session()
        session.source = "pipeline"
        session.agent_depth = 0
        manager.session_manager.get_pending_transcript_sessions.return_value = [session]
        manager.session_manager.get.return_value = session
        with (
            patch.object(manager, "_process_session_transcript", new_callable=AsyncMock),
            patch.object(
                manager,
                "_generate_artifacts_if_needed",
                new_callable=AsyncMock,
                return_value={"success": False, "error": "Transcript file not found"},
            ) as generate,
        ):
            assert await manager._process_pending_transcripts(manager._capture_active()) == 0
        assert generate.await_args.kwargs["allow_llm"] is False
        manager.session_manager.mark_transcript_processed.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
    async def test_process_session_transcript_preserves_unicode_separators(
        self,
        tmp_path: Path,
        manager: SessionLifecycleManager,
        separator: str,
    ) -> None:
        content = f"First{separator}second"
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "response_item",
                    "timestamp": "2026-09-09T00:00:00Z",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        manager.token_event_store = EmptyTokenEventStore()
        session_manager = cast(MagicMock, manager.session_manager)
        session_manager.get.return_value = SimpleNamespace(
            source="codex",
            project_id="proj-1",
            context_window=None,
            model=None,
            usage_input_tokens=0,
            usage_output_tokens=0,
            usage_cache_creation_tokens=0,
            usage_cache_read_tokens=0,
        )

        await manager._process_session_transcript("session-1", str(transcript))

        assert session_manager.update_stats.call_args.kwargs == {
            "message_count": 1,
            "turn_count": 1,
            "tool_call_count": 0,
            "last_assistant_content": content,
        }

    @pytest.mark.asyncio
    async def test_process_session_transcript_real_parsing(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """Test parsing logic inside _process_session_transcript."""
        transcript_path = tmp_path / "transcript.jsonl"
        with open(transcript_path, "w") as f:
            f.write(
                '{"type": "message", "message": {"content": "hello"}, "timestamp": "2024-01-01T00:00:00Z"}\n'
            )

        message_mock = MagicMock()
        message_mock.index = 5

        with patch("gobby.sessions.transcript_processing.get_parser") as MockParser:
            parser_instance = MockParser.return_value
            parser_instance.parse_lines.return_value = [message_mock]

            await manager._process_session_transcript("s1", str(transcript_path))

            parser_instance.parse_lines.assert_called_once()
            assert manager.session_manager.update_usage.call_count == 0

    @pytest.mark.asyncio
    async def test_process_session_transcript_writes_stats(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """The expiry path persists message/turn/tool stats via update_stats.

        Sessions the live processor never tailed before expiry must still record
        real counts; the predicate matches the live path (compute_message_stats).
        """
        from gobby.sessions.transcripts.base import ParsedMessage

        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')

        session = MagicMock()
        session.source = "claude"
        manager.session_manager.get.return_value = session

        def _mk(role, content_type, content="", tool_name=None):
            # spec=ParsedMessage so the lifecycle path's ParsedToolEvent filter keeps it.
            m = MagicMock(spec=ParsedMessage)
            m.role = role
            m.content_type = content_type
            m.content = content
            m.tool_name = tool_name
            m.model = None
            m.usage = None
            return m

        messages = [
            _mk("user", "text", "Add pagination to the search endpoint"),
            _mk("assistant", "text", "Working on it"),
            _mk("assistant", "tool_use", "", tool_name="Read"),
            _mk("assistant", "text", "Done"),
        ]

        with patch("gobby.sessions.transcript_processing.get_parser") as MockParser:
            MockParser.return_value.parse_lines.return_value = messages
            await manager._process_session_transcript("s1", str(transcript_path))

        manager.session_manager.update_stats.assert_called_once_with(
            "s1",
            message_count=4,
            turn_count=2,
            tool_call_count=1,
            last_assistant_content="Done",
        )
        stats_payload = manager.session_manager.update_stats.call_args.kwargs
        assert stats_payload == {
            "message_count": 4,
            "turn_count": 2,
            "tool_call_count": 1,
            "last_assistant_content": "Done",
        }

    @pytest.mark.asyncio
    async def test_process_agy_transcript_persists_shared_stats(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        transcript_path = tmp_path / "transcript_full.jsonl"
        transcript_path.write_text(
            AGY_STATS_FIXTURE.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        session = MagicMock()
        session.source = "agy"
        session.transcript_path = str(transcript_path)
        manager.session_manager.get.return_value = session
        persisted_stats: list[tuple[str, dict[str, object]]] = []

        def capture_stats(persisted_session_id: str, **stats: object) -> None:
            persisted_stats.append((persisted_session_id, stats))

        manager.session_manager.update_stats.side_effect = capture_stats

        await manager._process_session_transcript("s1", str(transcript_path))

        assert persisted_stats == [
            (
                "s1",
                {
                    "message_count": 3,
                    "turn_count": 1,
                    "tool_call_count": 4,
                    "last_assistant_content": None,
                },
            )
        ]

    @pytest.mark.asyncio
    async def test_process_session_transcript_missing_file(
        self, manager: SessionLifecycleManager
    ) -> None:
        """Test handling of missing file."""
        await manager._process_session_transcript("s1", "/non/existent/file.jsonl")
        assert manager.session_manager.get.call_count == 0

    @pytest.mark.asyncio
    async def test_process_session_transcript_read_error(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """Test error reading transcript file."""
        transcript_path = tmp_path / "transcript.jsonl"
        with open(transcript_path, "w") as f:
            f.write("content")

        # Permission error mock or similar
        with patch("builtins.open", side_effect=OSError("Read error")):
            with pytest.raises(IOError):
                await manager._process_session_transcript("s1", str(transcript_path))

    @pytest.mark.asyncio
    async def test_process_session_transcript_empty_file(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """Test processing empty file."""
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.touch()

        await manager._process_session_transcript("s1", str(transcript_path))
        assert manager.session_manager.get.call_count == 0

    @pytest.mark.asyncio
    async def test_process_session_transcript_no_messages(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """Test file with no valid messages."""
        transcript_path = tmp_path / "transcript.jsonl"
        with open(transcript_path, "w") as f:
            f.write('{"type": "unknown"}\n')

        with patch("gobby.sessions.transcript_processing.get_parser") as MockParser:
            MockParser.return_value.parse_lines.return_value = []

            await manager._process_session_transcript("s1", str(transcript_path))
        assert manager.session_manager.update_usage.call_count == 0

    @pytest.mark.asyncio
    async def test_process_pending_transcripts_loop_error(
        self, manager: SessionLifecycleManager
    ) -> None:
        """Test error handling in process loop (single iteration logic)."""
        manager.session_manager.get_pending_transcript_sessions.side_effect = Exception("DB Error")

        # Should propagate or handle? _process_pending_transcripts does NOT catch its own top-level errors (the loop does)
        with pytest.raises(Exception, match="DB Error"):
            await manager._process_pending_transcripts(manager._capture_active())

    async def test_process_pending_transcripts_individual_error(
        self, manager: SessionLifecycleManager
    ) -> None:
        sessions = [_pending_session("s1"), _pending_session("s2")]
        manager.session_manager.get_pending_transcript_sessions.return_value = sessions
        manager.session_manager.get.side_effect = sessions
        with (
            patch.object(manager, "_process_session_transcript", new_callable=AsyncMock),
            patch.object(
                manager,
                "_generate_artifacts_if_needed",
                new_callable=AsyncMock,
                side_effect=[TimeoutError("provider unavailable"), {"success": True}],
            ),
        ):
            assert await manager._process_pending_transcripts(manager._capture_active()) == 1
        manager.session_manager.mark_transcript_processed.assert_called_once_with(
            "s2",
            expected_session=sessions[1],
            source_hash=None,
        )
        assert manager._transcript_processing_cursor == (sessions[1].created_at, "s2")

    async def test_missing_transcript_with_handoff_remains_pending_without_summary(
        self, manager: SessionLifecycleManager
    ) -> None:
        session = _pending_session()
        session.summary_markdown = "invalid summary"
        session.handoff_markdown = "Unconfirmed handoff"
        manager.session_manager.get_pending_transcript_sessions.return_value = [session]
        manager.session_manager.get.return_value = session
        with (
            patch.object(manager, "_process_session_transcript", new_callable=AsyncMock),
            patch.object(
                manager,
                "_generate_artifacts_if_needed",
                new_callable=AsyncMock,
                return_value={"success": False, "error": "Transcript file not found"},
            ),
        ):
            assert await manager._process_pending_transcripts(manager._capture_active()) == 0
        manager.session_manager.mark_transcript_processed.assert_not_called()
        manager.session_manager.record_transcript_processing_failure.assert_called_once_with(
            "s1",
            error_code="missing_source",
            error="Transcript file not found",
            expected_session=session,
        )

    async def test_missing_transcript_invalid_summary_remains_pending(
        self, manager: SessionLifecycleManager
    ) -> None:
        session = _pending_session()
        session.summary_markdown = "invalid summary"
        session.handoff_markdown = "Unconfirmed handoff"
        manager.session_manager.get_pending_transcript_sessions.return_value = [session]
        manager.session_manager.get.return_value = session
        with (
            patch.object(manager, "_process_session_transcript", new_callable=AsyncMock),
            patch.object(
                manager,
                "_generate_artifacts_if_needed",
                new_callable=AsyncMock,
                return_value={"success": False, "error": "Transcript file not found"},
            ),
        ):
            assert await manager._process_pending_transcripts(manager._capture_active()) == 0
        manager.session_manager.mark_transcript_processed.assert_not_called()
        manager.session_manager.record_transcript_processing_failure.assert_called_once_with(
            "s1",
            error_code="missing_source",
            error="Transcript file not found",
            expected_session=session,
        )

    async def test_missing_transcript_no_digest_remains_pending(
        self, manager: SessionLifecycleManager
    ) -> None:
        session = _pending_session()
        session.summary_markdown = "invalid summary"
        session.handoff_markdown = "Unconfirmed handoff"
        manager.session_manager.get_pending_transcript_sessions.return_value = [session]
        manager.session_manager.get.return_value = session
        with (
            patch.object(manager, "_process_session_transcript", new_callable=AsyncMock),
            patch.object(
                manager,
                "_generate_artifacts_if_needed",
                new_callable=AsyncMock,
                return_value={"success": False, "error": "Transcript file not found"},
            ),
        ):
            assert await manager._process_pending_transcripts(manager._capture_active()) == 0
        manager.session_manager.mark_transcript_processed.assert_not_called()
        manager.session_manager.record_transcript_processing_failure.assert_called_once_with(
            "s1",
            error_code="missing_source",
            error="Transcript file not found",
            expected_session=session,
        )

    @pytest.mark.asyncio
    async def test_pending_graph_memory_db_work_uses_memory_run_db(
        self, mock_db: MagicMock, mock_config: SessionLifecycleConfig
    ) -> None:
        """Queued graph memory DB work uses the bounded memory DB executor."""

        class MemoryManagerStub:
            def __init__(self) -> None:
                self.kg_service = AsyncMock()
                self.kg_service.add_to_graph.return_value = SimpleNamespace(status="success")
                self.pending = [
                    SimpleNamespace(
                        id="mem-1",
                        content="remember this",
                        project_id="proj-1",
                        is_global=False,
                    )
                ]
                self.marked: list[str] = []
                self.run_db_calls: list[
                    tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]
                ] = []

            async def run_db(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
                self.run_db_calls.append((func, args, kwargs))
                return func(*args, **kwargs)

            def get_pending_graph_memories(self, limit: int = 20) -> list[SimpleNamespace]:
                assert limit == 3
                return self.pending

            def mark_graph_processed(self, memory_id: str) -> None:
                self.marked.append(memory_id)

            def record_graph_failure(
                self,
                memory_id: str,
                *,
                deterministic: bool,
                max_attempts: int,
            ) -> str:
                raise AssertionError("success must not record a failure")

        memory_manager = MemoryManagerStub()
        with patch(_SESSION_MANAGER_PATCH):
            manager = SessionLifecycleManager(
                mock_db,
                static_session_capture(mock_config, services=_memory_services(memory_manager)),
            )

        with patch(
            "gobby.sessions.lifecycle.asyncio.to_thread", new_callable=AsyncMock
        ) as to_thread:
            processed = await manager._process_pending_graph_memories(
                KnowledgeGraphQueueConfig(batch_size=3)
            )

        assert processed == 1
        assert memory_manager.marked == ["mem-1"]
        assert [call[0] for call in memory_manager.run_db_calls] == [
            memory_manager.get_pending_graph_memories,
            memory_manager.mark_graph_processed,
        ]
        to_thread.assert_not_awaited()

    @pytest.mark.parametrize(
        ("result_or_error", "deterministic"),
        [
            ("deterministic_failure", True),
            ("retryable_failure", False),
            ("partial_failure", False),
            (RuntimeError("provider unavailable"), False),
        ],
    )
    async def test_pending_graph_failure_policy_is_persisted(
        self,
        mock_db: MagicMock,
        mock_config: SessionLifecycleConfig,
        result_or_error: str | Exception,
        deterministic: bool,
    ) -> None:
        """Only deterministic extraction failures consume the bounded attempt budget."""

        class MemoryManagerStub:
            def __init__(self) -> None:
                self.kg_service = AsyncMock()
                if isinstance(result_or_error, Exception):
                    self.kg_service.add_to_graph.side_effect = result_or_error
                else:
                    self.kg_service.add_to_graph.return_value = SimpleNamespace(
                        status=result_or_error
                    )
                self.memory = SimpleNamespace(
                    id="mem-failure",
                    content="poisoned content",
                    project_id="proj-1",
                    is_global=False,
                )
                self.failures: list[tuple[str, bool, int]] = []

            async def run_db(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
                return func(*args, **kwargs)

            def get_pending_graph_memories(self, limit: int = 20) -> list[SimpleNamespace]:
                return [self.memory]

            def mark_graph_processed(self, memory_id: str) -> None:
                raise AssertionError("failed extraction must not be completed")

            def record_graph_failure(
                self,
                memory_id: str,
                *,
                deterministic: bool,
                max_attempts: int,
            ) -> str:
                self.failures.append((memory_id, deterministic, max_attempts))
                return "pending"

        memory_manager = MemoryManagerStub()
        queue_config = KnowledgeGraphQueueConfig(max_deterministic_attempts=4)
        with patch(_SESSION_MANAGER_PATCH):
            manager = SessionLifecycleManager(
                mock_db,
                static_session_capture(
                    mock_config,
                    kg_queue=queue_config,
                    services=_memory_services(memory_manager),
                ),
            )

        assert await manager._process_pending_graph_memories(queue_config) == 0
        assert memory_manager.failures == [("mem-failure", deterministic, 4)]


class TestBackgroundLoops:
    """Tests for infinite background loops."""

    @pytest.mark.asyncio
    async def test_expire_loop_runs_and_calls_delegate(
        self, manager: SessionLifecycleManager
    ) -> None:
        """Test expire loop calls delegate and sleeps."""
        manager._running = True

        # Mock delegate to verify call
        manager._expire_stale_sessions = AsyncMock(return_value=0)

        # Mock sleep to run once then stop loop
        async def side_effect_sleep(seconds):
            manager._running = False  # Stop after first sleep
            return

        with patch("asyncio.sleep", side_effect=side_effect_sleep) as mock_sleep:
            await manager._expire_loop()

            manager._expire_stale_sessions.assert_awaited_once()
            mock_sleep.assert_awaited_once_with(
                manager._capture_active().session_lifecycle.expire_check_interval_minutes * 60
            )
            assert manager._running is False

    @pytest.mark.asyncio
    async def test_process_loop_runs_and_calls_delegate(
        self, manager: SessionLifecycleManager
    ) -> None:
        """Test process loop calls delegate and sleeps."""
        manager._running = True

        manager._process_pending_transcripts = AsyncMock(return_value=0)

        async def side_effect_sleep(seconds):
            manager._running = False
            return

        with patch("asyncio.sleep", side_effect=side_effect_sleep) as mock_sleep:
            await manager._process_loop()

            manager._process_pending_transcripts.assert_awaited_once()
            mock_sleep.assert_awaited_once_with(
                manager._capture_active().session_lifecycle.transcript_processing_interval_minutes
                * 60
            )
            assert manager._running is False

    @pytest.mark.asyncio
    async def test_loops_handle_exceptions(self, manager: SessionLifecycleManager) -> None:
        """Test loops catch exceptions from delegate."""
        manager._running = True

        # Delegate raises exception
        manager._expire_stale_sessions = AsyncMock(side_effect=Exception("Boom"))

        # Log error is called
        with patch("gobby.sessions.lifecycle.logger.error") as mock_logger:

            async def side_effect_sleep(seconds):
                manager._running = False
                return

            with patch("asyncio.sleep", side_effect=side_effect_sleep):
                await manager._expire_loop()

            mock_logger.assert_called_with(
                "Error in expire loop: %s", manager._expire_stale_sessions.side_effect
            )
            assert manager._running is False

    @pytest.mark.asyncio
    async def test_loops_handle_cancellation(self, manager: SessionLifecycleManager) -> None:
        """Test loops exit on CancelledError during sleep."""
        manager._running = True
        manager._expire_stale_sessions = AsyncMock()

        with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
            await manager._expire_loop()
            assert manager._expire_stale_sessions.await_count == 1


class TestPromptFileCleanup:
    """Tests for _cleanup_prompt_files (#7389)."""

    def test_removes_old_prompt_files(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """Old prompt files are deleted."""
        prompt_dir = tmp_path / "gobby-prompts"
        prompt_dir.mkdir()

        # Create a file and backdate its mtime by 2 hours
        old_file = prompt_dir / "prompt-old-session.txt"
        old_file.write_text("old prompt")
        old_mtime = time.time() - 7200
        os.utime(old_file, (old_mtime, old_mtime))

        with patch("tempfile.gettempdir", return_value=str(tmp_path)):
            removed = manager._cleanup_prompt_files(max_age_seconds=3600)

        assert removed == 1
        assert not old_file.exists()

    def test_keeps_recent_prompt_files(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """Recent prompt files are kept."""
        prompt_dir = tmp_path / "gobby-prompts"
        prompt_dir.mkdir()

        recent_file = prompt_dir / "prompt-recent-session.txt"
        recent_file.write_text("recent prompt")
        # File just created — mtime is now

        with patch("tempfile.gettempdir", return_value=str(tmp_path)):
            removed = manager._cleanup_prompt_files(max_age_seconds=3600)

        assert removed == 0
        assert recent_file.exists()

    def test_mixed_old_and_recent(self, tmp_path: Path, manager: SessionLifecycleManager) -> None:
        """Only old files are removed, recent ones kept."""
        prompt_dir = tmp_path / "gobby-prompts"
        prompt_dir.mkdir()

        old_file = prompt_dir / "prompt-old.txt"
        old_file.write_text("old")
        old_mtime = time.time() - 7200
        os.utime(old_file, (old_mtime, old_mtime))

        recent_file = prompt_dir / "prompt-recent.txt"
        recent_file.write_text("recent")

        with patch("tempfile.gettempdir", return_value=str(tmp_path)):
            removed = manager._cleanup_prompt_files(max_age_seconds=3600)

        assert removed == 1
        assert not old_file.exists()
        assert recent_file.exists()

    def test_no_prompt_dir(self, tmp_path: Path, manager: SessionLifecycleManager) -> None:
        """Returns 0 when prompt directory doesn't exist."""
        with patch("tempfile.gettempdir", return_value=str(tmp_path)):
            removed = manager._cleanup_prompt_files()

        assert removed == 0

    @pytest.mark.asyncio
    async def test_expire_calls_cleanup(self, manager: SessionLifecycleManager) -> None:
        """_expire_stale_sessions calls _cleanup_prompt_files."""
        manager.session_manager = MagicMock()
        manager.session_manager.pause_inactive_active_sessions.return_value = 0
        manager.session_manager.expire_orphaned_handoff_sessions.return_value = 0
        manager.session_manager.expire_stale_sessions.return_value = 0
        manager.session_manager.expire_empty_sessions.return_value = 0
        manager.session_manager.prune_empty_sessions.return_value = 0

        with patch.object(manager, "_cleanup_prompt_files") as mock_cleanup:
            expired_count = await manager._expire_stale_sessions(
                manager._capture_active().session_lifecycle
            )

        assert expired_count == 0
        mock_cleanup.assert_called_once()


class TestGenerateArtifactsIfNeeded:
    @pytest.mark.parametrize("allow_llm", [True, False])
    async def test_canonical_pipeline_receives_policy_and_archive_reader(
        self,
        manager: SessionLifecycleManager,
        allow_llm: bool,
    ) -> None:
        from gobby.sessions.summarize import _SummaryCoreResult
        from gobby.sessions.transcript_reader import TranscriptReader

        llm = _set_llm_service(manager, MagicMock())
        outcome = {"success": True, "source_context_hash": "fresh"}
        with patch(
            "gobby.sessions.summarize._generate_session_summary_core",
            new_callable=AsyncMock,
            return_value=_SummaryCoreResult(outcome, "summary"),
        ) as generate:
            result = await manager._generate_artifacts_if_needed(
                "s1",
                manager._capture_active().session_summary,
                allow_llm=allow_llm,
                archive_dir="/isolated/archive",
            )
        assert result == outcome
        assert generate.await_args.args[2] is (llm if allow_llm else None)
        reader = generate.await_args.kwargs["transcript_reader"]
        assert isinstance(reader, TranscriptReader)
        assert reader._archive_dir == "/isolated/archive"

    async def test_transient_exception_reaches_scheduler(
        self, manager: SessionLifecycleManager
    ) -> None:
        with patch(
            "gobby.sessions.summarize._generate_session_summary_core",
            new_callable=AsyncMock,
            side_effect=TimeoutError("provider unavailable"),
        ):
            with pytest.raises(TimeoutError, match="provider unavailable"):
                await manager._generate_artifacts_if_needed(
                    "s1", manager._capture_active().session_summary
                )


class TestPurgeSoftDeletedDefinitions:
    """Tests for _purge_soft_deleted_definitions."""

    @pytest.mark.asyncio
    async def test_success(self, manager: SessionLifecycleManager) -> None:
        """Purge fans out over the four typed parent managers."""
        paths = (
            "gobby.storage.definitions.rules.RuleDefinitionManager",
            "gobby.storage.definitions.agents.AgentDefinitionManager",
            "gobby.storage.definitions.variables.SessionVariableDefaultManager",
            "gobby.storage.definitions.pipelines.PipelineDefinitionManager",
        )
        with (
            patch(paths[0]) as mock_rules,
            patch(paths[1]) as mock_agents,
            patch(paths[2]) as mock_variables,
            patch(paths[3]) as mock_pipelines,
        ):
            await manager._purge_soft_deleted_definitions()
        for mock in (mock_rules, mock_agents, mock_variables, mock_pipelines):
            assert mock.call_count == 1
            assert mock.return_value.purge_deleted.call_count == 1
            assert mock.return_value.purge_deleted.call_args == call(older_than_days=30)

    @pytest.mark.asyncio
    async def test_exception_handled(self, manager: SessionLifecycleManager) -> None:
        """Purge errors are caught and logged."""
        with patch("gobby.storage.definitions.rules.RuleDefinitionManager") as mock_rules:
            mock_rules.return_value.purge_deleted.side_effect = Exception("DB error")
            with (
                patch("gobby.storage.definitions.agents.AgentDefinitionManager"),
                patch("gobby.storage.definitions.variables.SessionVariableDefaultManager"),
                patch("gobby.storage.definitions.pipelines.PipelineDefinitionManager"),
            ):
                await manager._purge_soft_deleted_definitions()
        assert mock_rules.return_value.purge_deleted.call_count == 1
        assert mock_rules.return_value.purge_deleted.call_args == call(older_than_days=30)


class TestPurgeDreamHiddenMemories:
    """Tests for _purge_dream_hidden_memories (dream GC grace purge + retention)."""

    @staticmethod
    def _dream_config(**overrides: Any) -> MemoryDreamConfig:
        base = {
            "enabled": True,
            "purge_delete_after_days": 30,
            "purge_review_after_days": 90,
            "run_retention_days": 45,
        }
        base.update(overrides)
        return MemoryDreamConfig(**base)

    def _manager(
        self,
        mock_db: MagicMock,
        mock_config: SessionLifecycleConfig,
        *,
        memory_manager: Any,
        dream_config: MemoryDreamConfig,
    ) -> SessionLifecycleManager:
        with patch(_SESSION_MANAGER_PATCH):
            return SessionLifecycleManager(
                mock_db,
                static_session_capture(
                    mock_config,
                    dream=dream_config,
                    services=_memory_services(memory_manager),
                ),
            )

    @pytest.mark.asyncio
    async def test_purges_both_actions_then_prunes_runs(
        self, mock_db: MagicMock, mock_config: SessionLifecycleConfig
    ) -> None:
        """Both grace windows purge with reconcile, then run history is pruned."""
        purge = AsyncMock()
        memory_manager = SimpleNamespace(purge_dream_hidden=purge)
        manager = self._manager(
            mock_db, mock_config, memory_manager=memory_manager, dream_config=self._dream_config()
        )
        dream_config = manager._capture_active().memory.dream
        store = MagicMock()
        store.prune_runs = MagicMock(return_value=2)
        with patch("gobby.memory.dream.storage.MemoryDreamStore", return_value=store) as store_cls:
            await manager._purge_dream_hidden_memories(dream_config)

        assert dream_config.purge_delete_after_days == 30
        assert dream_config.purge_review_after_days == 90
        # Each action class purges with its own grace window.
        assert purge.await_args_list == [call("delete", 30), call("review", 90)]
        # Run/snapshot history pruned by run_retention_days against the same db.
        store_cls.assert_called_once_with(mock_db)
        store.prune_runs.assert_called_once_with(45)

    @pytest.mark.asyncio
    async def test_without_memory_purge_handler_still_prunes_runs(
        self, mock_db: MagicMock, mock_config: SessionLifecycleConfig
    ) -> None:
        """Run-history pruning does not require a memory purge handler."""
        memory_manager = SimpleNamespace()
        manager = self._manager(
            mock_db,
            mock_config,
            memory_manager=memory_manager,
            dream_config=self._dream_config(),
        )
        dream_config = manager._capture_active().memory.dream
        pruned_days: list[int] = []
        store = SimpleNamespace(prune_runs=pruned_days.append)
        with patch("gobby.memory.dream.storage.MemoryDreamStore", return_value=store):
            await manager._purge_dream_hidden_memories(dream_config)
        assert pruned_days == [45]

    @pytest.mark.asyncio
    async def test_runs_independently_of_dream_enabled(
        self, mock_db: MagicMock, mock_config: SessionLifecycleConfig
    ) -> None:
        """Purge reclaims rows even after dream is switched off."""
        purge = AsyncMock()
        memory_manager = SimpleNamespace(purge_dream_hidden=purge)
        manager = self._manager(
            mock_db,
            mock_config,
            memory_manager=memory_manager,
            dream_config=self._dream_config(enabled=False),
        )
        dream_config = manager._capture_active().memory.dream
        store = MagicMock()
        store.prune_runs = MagicMock(return_value=0)
        with patch("gobby.memory.dream.storage.MemoryDreamStore", return_value=store):
            await manager._purge_dream_hidden_memories(dream_config)
        assert dream_config.enabled is False
        assert purge.await_count == 2  # delete + review still purged
        store.prune_runs.assert_called_once_with(45)

    @pytest.mark.asyncio
    async def test_purge_failure_does_not_block_run_pruning(
        self, mock_db: MagicMock, mock_config: SessionLifecycleConfig
    ) -> None:
        """A purge error for one action is logged; pruning still runs."""
        purge = AsyncMock(side_effect=Exception("qdrant down"))
        memory_manager = SimpleNamespace(purge_dream_hidden=purge)
        manager = self._manager(
            mock_db, mock_config, memory_manager=memory_manager, dream_config=self._dream_config()
        )
        dream_config = manager._capture_active().memory.dream
        store = MagicMock()
        store.prune_runs = MagicMock(return_value=0)
        with patch("gobby.memory.dream.storage.MemoryDreamStore", return_value=store):
            await manager._purge_dream_hidden_memories(dream_config)  # must not raise
        assert dream_config.run_retention_days == 45
        assert purge.await_count == 2
        store.prune_runs.assert_called_once_with(45)


class TestProcessSessionTranscriptParsers:
    """Tests for _process_session_transcript parser selection."""

    @pytest.mark.asyncio
    async def test_qwen_parser_selected(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """Qwen source uses QwenTranscriptParser."""
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')

        session = MagicMock()
        session.source = "qwen"
        manager.session_manager.get.return_value = session

        with patch("gobby.sessions.transcript_processing.get_parser") as MockParser:
            MockParser.return_value.parse_lines.return_value = []
            await manager._process_session_transcript("s1", str(transcript_path))
            MockParser.assert_called_once()
            assert manager.session_manager.update_usage.call_count == 0

    @pytest.mark.asyncio
    async def test_codex_parser_selected(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """Codex source uses CodexTranscriptParser."""
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')

        session = MagicMock()
        session.source = "codex"
        manager.session_manager.get.return_value = session

        with patch("gobby.sessions.transcript_processing.get_parser") as MockParser:
            MockParser.return_value.parse_lines.return_value = []
            await manager._process_session_transcript("s1", str(transcript_path))
            MockParser.assert_called_once()
            assert manager.session_manager.update_usage.call_count == 0

    @pytest.mark.asyncio
    async def test_droid_parser_selected_with_transcript_path(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """Droid source uses DroidTranscriptParser with the transcript path for sidecars."""
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')

        session = MagicMock()
        session.source = "droid"
        session.transcript_path = str(transcript_path)
        manager.session_manager.get.return_value = session

        with patch("gobby.sessions.transcript_processing.get_parser") as MockParser:
            MockParser.return_value.parse_lines.return_value = []
            await manager._process_session_transcript("s1", str(transcript_path))
            MockParser.assert_called_once_with(
                "droid",
                session_id="s1",
                transcript_path=str(transcript_path),
            )
            assert manager.session_manager.update_usage.call_count == 0

    @pytest.mark.asyncio
    async def test_droid_session_is_not_parsed_by_claude_parser(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
        from gobby.sessions.transcripts.droid import DroidTranscriptParser

        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')
        session = MagicMock()
        session.source = "droid"
        session.transcript_path = str(transcript_path)
        manager.session_manager.get.return_value = session
        claude_cls = MagicMock(wraps=ClaudeTranscriptParser)
        droid_cls = MagicMock(wraps=DroidTranscriptParser)

        # Route through the shared registry seam: a parser built by module
        # reference would bypass PARSER_REGISTRY and leave these spies inert.
        with patch.dict(
            "gobby.sessions.transcripts.PARSER_REGISTRY",
            {"claude": claude_cls, "droid": droid_cls},
            clear=True,
        ):
            await manager._process_session_transcript("s1", str(transcript_path))

        droid_cls.assert_called_once_with(session_id="s1", transcript_path=str(transcript_path))
        claude_cls.assert_not_called()
        assert session.source == "droid"
        assert transcript_path.read_text(encoding="utf-8") == '{"type": "message"}\n'

    @pytest.mark.asyncio
    async def test_unknown_source_raises(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')

        session = MagicMock()
        session.source = "unknown-cli"
        session.transcript_path = str(transcript_path)
        manager.session_manager.get.return_value = session

        with pytest.raises(ValueError, match="Unsupported transcript source"):
            await manager._process_session_transcript("s1", str(transcript_path))

    @pytest.mark.asyncio
    async def test_droid_backfill_records_sidecar_token_usage(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """Droid lifecycle backfill records TokenUsage from the adjacent settings sidecar."""
        transcript_path = tmp_path / "droid-session.jsonl"
        transcript_path.write_text(DROID_FIXTURE_JSONL.read_text(encoding="utf-8"))
        transcript_path.with_suffix(".settings.json").write_text(
            DROID_FIXTURE_SETTINGS.read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        session = MagicMock()
        session.source = "droid"
        session.transcript_path = str(transcript_path)
        session.project_id = "project-id"
        session.context_window = None
        session.model = None
        manager.session_manager.get.return_value = session

        zero_totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
        }
        manager.token_event_store = MagicMock()
        manager.token_event_store.get_session_totals.side_effect = [
            dict(zero_totals),
            dict(zero_totals),
        ]
        manager.token_event_store.record_batch.side_effect = lambda events: [True] * len(events)

        await manager._process_session_transcript("s1", str(transcript_path))

        event = manager.token_event_store.record_batch.call_args.args[0][0]
        assert event.session_id == "s1"
        assert event.project_id == "project-id"
        assert event.source == "droid"
        assert event.origin == "transcript"
        assert event.model == "claude-3-7-sonnet-latest"
        assert event.input_tokens == 22571
        assert event.output_tokens == 512
        assert event.cache_creation_tokens == 0
        assert event.cache_read_tokens == 26112
        assert event.metadata == {"content_type": "tool_use"}
        manager.session_manager.update_usage.assert_called_once_with(
            session_id="s1",
            input_tokens=22571,
            output_tokens=512,
            cache_creation_tokens=0,
            cache_read_tokens=26112,
            context_window=None,
            model="claude-3-7-sonnet-latest",
        )
        st = os.stat(transcript_path)
        index = load_index_sidecar(
            str(transcript_path),
            "droid",
            "s1",
            seek_mode="byte",
            mtime_ns=st.st_mtime_ns,
            size=st.st_size,
        )
        assert index is not None
        assert index.post_pass_adjustments
        adjustment = index.post_pass_adjustments[0]
        assert adjustment.field == "usage"
        assert adjustment.value.input_tokens == 22571
        assert adjustment.value.output_tokens == 512

    @pytest.mark.asyncio
    async def test_codex_backfill_uses_latest_context_window_for_session_usage(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """Codex token_count backfill should hydrate context pie fields."""
        transcript_path = tmp_path / "codex-rollout.jsonl"
        transcript_path.write_text(
            """
{"timestamp":"2026-05-27T21:50:28.208Z","type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"input_tokens":104960,"cached_input_tokens":93568,"output_tokens":342,"reasoning_output_tokens":156,"total_tokens":105302},"model_context_window":258400}}}
""".lstrip()
        )

        session = MagicMock()
        session.source = "codex"
        session.transcript_path = str(transcript_path)
        session.project_id = "project-id"
        session.context_window = None
        session.model = None
        manager.session_manager.get.return_value = session

        zero_totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
        }
        manager.token_event_store = MagicMock()
        manager.token_event_store.get_session_totals.side_effect = [
            dict(zero_totals),
            dict(zero_totals),
        ]
        manager.token_event_store.record_batch.side_effect = lambda events: [True] * len(events)

        await manager._process_session_transcript("s1", str(transcript_path))

        event = manager.token_event_store.record_batch.call_args.args[0][0]
        assert event.input_tokens == 11392
        assert event.output_tokens == 342
        assert event.cache_read_tokens == 93568
        assert event.context_window == 258400
        manager.session_manager.update_usage.assert_called_once_with(
            session_id="s1",
            input_tokens=11392,
            output_tokens=342,
            cache_creation_tokens=0,
            cache_read_tokens=93568,
            context_window=258400,
            model=None,
        )
        manager.session_manager.update_context_usage.assert_called_once()
        update_context_usage_call = manager.session_manager.update_context_usage.call_args
        snapshot = (
            update_context_usage_call.kwargs.get("snapshot")
            if update_context_usage_call.kwargs
            else None
        )
        if snapshot is None:
            snapshot = update_context_usage_call.args[1]
        assert snapshot.context_used_tokens == 104960
        assert snapshot.context_usage_ratio == pytest.approx(104960 / 258400)

    @pytest.mark.asyncio
    async def test_grok_turn_completed_does_not_become_occupancy(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """Batch Grok occupancy is the compact-epoch max totalTokens, never turn usage."""
        transcript_path = tmp_path / "grok-session.jsonl"

        def update_line(update: dict[str, object], meta: dict[str, object]) -> str:
            params = {"sessionId": "grok-session", "update": update, "_meta": meta}
            return json.dumps({"method": "session/update", "params": params, "timestamp": 1})

        transcript_path.write_text(
            "\n".join(
                [
                    update_line(
                        {"sessionUpdate": "tool_call", "toolCallId": "call-1", "title": "use_tool"},
                        {"totalTokens": 322_811, "totalContextTokens": 500_000},
                    ),
                    update_line(
                        {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": "call-1",
                            "status": "completed",
                        },
                        {"totalTokens": 142_169, "totalContextTokens": 500_000},
                    ),
                    update_line(
                        {
                            "sessionUpdate": "turn_completed",
                            "prompt_id": "prompt-1",
                            "usage": {
                                "inputTokens": 2_477_977,
                                "outputTokens": 6_438,
                                "cachedReadTokens": 2_344_576,
                                "cacheCreationTokens": 0,
                                "modelCalls": 18,
                            },
                        },
                        {"eventId": "turn-1", "totalContextTokens": 500_000},
                    ),
                    update_line(
                        {
                            "sessionUpdate": "hook_execution",
                            "hookName": "PostToolUse",
                            "status": "success",
                        },
                        {"eventId": "hook-1", "totalContextTokens": 500_000},
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        session = MagicMock()
        session.source = "grok"
        session.transcript_path = str(transcript_path)
        session.project_id = "project-id"
        session.context_window = None
        session.model = None
        manager.session_manager.get.return_value = session

        zero_totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
        }
        manager.token_event_store = MagicMock()
        manager.token_event_store.get_session_totals.side_effect = [
            dict(zero_totals),
            dict(zero_totals),
        ]
        manager.token_event_store.record_batch.side_effect = lambda events: [True] * len(events)

        await manager._process_session_transcript("s1", str(transcript_path))

        events = manager.token_event_store.record_batch.call_args.args[0]
        assert [event.metadata for event in events] == [{"content_type": "turn_completed"}]
        manager.session_manager.update_context_usage.assert_called_once()
        snapshot = manager.session_manager.update_context_usage.call_args.args[1]
        assert snapshot.context_used_tokens == 322_811
        assert snapshot.confidence == "reported"

    @pytest.mark.asyncio
    async def test_session_not_found_returns_early(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """Returns early when session not found in DB."""
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')

        manager.session_manager.get.return_value = None
        await manager._process_session_transcript("s1", str(transcript_path))
        assert manager.session_manager.update_usage.call_count == 0

    @pytest.mark.asyncio
    async def test_none_transcript_path(self, manager: SessionLifecycleManager) -> None:
        """Handles None transcript_path."""
        await manager._process_session_transcript("s1", None)
        assert manager.session_manager.get.call_count == 0


class TestProcessSessionTranscriptLineParsing:
    """Tests for .json transcript dispatch."""

    @pytest.mark.asyncio
    async def test_qwen_json_uses_current_line_parser(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """Lifecycle backfill indexes Qwen's current line-envelope .json file."""
        transcript_path = tmp_path / "session-abc.json"
        fixture = (
            Path(__file__).parents[1]
            / "fixtures"
            / "transcripts"
            / "qwen"
            / "current_envelope.jsonl"
        )
        transcript_path.write_text(fixture.read_text())

        session = MagicMock()
        session.source = "qwen"
        session.project_id = None
        session.context_window = None
        session.model = "qwen3-coder"
        session.transcript_path = str(transcript_path)
        session.usage_input_tokens = 0
        session.usage_output_tokens = 0
        session.usage_cache_creation_tokens = 0
        session.usage_cache_read_tokens = 0
        manager.session_manager.get.return_value = session
        manager.token_event_store = EmptyTokenEventStore()

        await manager._process_session_transcript("s1", str(transcript_path))

        st = transcript_path.stat()
        index = load_index_sidecar(
            str(transcript_path),
            "qwen",
            "s1",
            seek_mode="byte",
            mtime_ns=st.st_mtime_ns,
            size=st.st_size,
        )
        assert index is not None
        assert index.raw_record_count == 7
        assert index.parsed_message_count == 7
        manager.session_manager.update_stats.assert_called_once()

    @pytest.mark.asyncio
    async def test_jsonl_still_uses_parse_lines(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """Qwen .jsonl transcripts use the same line parser."""
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')

        session = MagicMock()
        session.source = "qwen"
        manager.session_manager.get.return_value = session

        with patch("gobby.sessions.transcript_processing.get_parser") as MockParser:
            MockParser.return_value.parse_lines.return_value = []
            await manager._process_session_transcript("s1", str(transcript_path))
            MockParser.return_value.parse_lines.assert_called_once()
            assert manager.session_manager.update_usage.call_count == 0

    @pytest.mark.asyncio
    async def test_invalid_json_returns_early(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """An invalid Qwen envelope fails soft without crashing."""
        transcript_path = tmp_path / "session-bad.json"
        transcript_path.write_text("{invalid json content")

        session = MagicMock()
        session.source = "qwen"
        manager.session_manager.get.return_value = session

        # Should not raise
        await manager._process_session_transcript("s1", str(transcript_path))
        manager.session_manager.update_usage.assert_not_called()
        assert manager.session_manager.update_usage.call_count == 0


class TestProcessSessionTranscriptTokenPreservation:
    """Tests for preserving hook-captured tokens when transcript yields 0."""

    @pytest.mark.asyncio
    async def test_zero_tokens_preserves_existing_nonzero(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """When transcript yields 0 tokens but session has existing tokens, don't overwrite."""
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')

        session = MagicMock()
        session.source = "claude"
        session.usage_input_tokens = 5000
        session.usage_output_tokens = 2000

        # First get() returns session for parser selection, second for preservation check
        manager.session_manager.get.return_value = session

        with patch("gobby.sessions.transcript_processing.get_parser") as MockParser:
            # Parser returns messages with no usage
            msg = MagicMock()
            msg.model = None
            msg.usage = None
            MockParser.return_value.parse_lines.return_value = [msg]
            await manager._process_session_transcript("s1", str(transcript_path))

        # update_usage should NOT be called — preserving existing values
        manager.session_manager.update_usage.assert_not_called()
        assert manager.session_manager.update_usage.call_count == 0

    @pytest.mark.asyncio
    async def test_zero_tokens_updates_when_existing_also_zero(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """When both transcript and existing are 0, update_usage is still called (nothing to preserve)."""
        from gobby.sessions.transcripts.base import ParsedMessage

        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')

        session = MagicMock()
        session.source = "claude"
        session.usage_input_tokens = 0
        session.usage_output_tokens = 0

        manager.session_manager.get.return_value = session

        with patch("gobby.sessions.transcript_processing.get_parser") as MockParser:
            # spec=ParsedMessage so the lifecycle path's ParsedToolEvent filter
            # doesn't drop the mock.
            msg = MagicMock(spec=ParsedMessage)
            msg.model = None
            msg.usage = None
            MockParser.return_value.parse_lines.return_value = [msg]
            await manager._process_session_transcript("s1", str(transcript_path))

        # Both are 0, so no preservation needed — update proceeds (harmless write of zeros)
        manager.session_manager.update_usage.assert_called_once()
        call_kwargs = manager.session_manager.update_usage.call_args.kwargs
        assert call_kwargs["input_tokens"] == 0
        assert call_kwargs["output_tokens"] == 0

    @pytest.mark.asyncio
    async def test_nonzero_tokens_always_updates(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        """When transcript yields real tokens, always update regardless of existing values."""
        from gobby.sessions.transcripts.base import ParsedMessage, TokenUsage

        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')

        session = MagicMock()
        session.source = "claude"
        session.usage_input_tokens = 5000
        session.usage_output_tokens = 2000
        manager.session_manager.get.return_value = session

        msg = MagicMock(spec=ParsedMessage)
        msg.model = "claude-sonnet-4-6"
        msg.usage = TokenUsage(input_tokens=8000, output_tokens=3000)

        with patch("gobby.sessions.transcript_processing.get_parser") as MockParser:
            MockParser.return_value.parse_lines.return_value = [msg]
            await manager._process_session_transcript("s1", str(transcript_path))

        manager.session_manager.update_usage.assert_called_once()
        call_kwargs = manager.session_manager.update_usage.call_args
        assert call_kwargs.kwargs["input_tokens"] == 8000
        assert call_kwargs.kwargs["output_tokens"] == 3000

    @pytest.mark.asyncio
    async def test_final_claude_usage_preserves_one_million_session_model(
        self, tmp_path: Path, manager: SessionLifecycleManager
    ) -> None:
        from gobby.sessions.transcripts.base import ParsedMessage, TokenUsage

        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type": "message"}\n')
        session = MagicMock()
        session.source = "claude"
        session.project_id = "project-id"
        session.context_window = 200_000
        session.model = "claude-opus-4-8[1m]"
        session.usage_input_tokens = 0
        session.usage_output_tokens = 0
        session.usage_cache_creation_tokens = 0
        session.usage_cache_read_tokens = 0
        manager.session_manager.get.return_value = session
        manager.token_event_store = MagicMock()
        manager.token_event_store.get_session_totals.side_effect = [
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 0,
            },
            {
                "input_tokens": 125_071,
                "output_tokens": 1,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 0,
            },
        ]
        manager.token_event_store.record_batch.side_effect = lambda events: [True] * len(events)
        message = MagicMock(spec=ParsedMessage)
        message.model = "claude-opus-4-8"
        message.raw_json = {}
        message.usage = TokenUsage(input_tokens=125_071, output_tokens=1)

        with patch("gobby.sessions.transcript_processing.get_parser") as parser:
            parser.return_value.parse_lines.return_value = [message]
            await manager._process_session_transcript("s1", str(transcript_path))

        event = manager.token_event_store.record_batch.call_args.args[0][0]
        assert event.model == "claude-opus-4-8[1m]"
        assert event.context_window == 1_000_000
        update = manager.session_manager.update_usage.call_args
        assert update.kwargs["model"] == "claude-opus-4-8[1m]"
        assert update.kwargs["context_window"] == 1_000_000
        snapshot = manager.session_manager.update_context_usage.call_args.args[1]
        assert snapshot.context_usage_ratio == pytest.approx(0.125071)


class TestProcessPendingTranscriptsArchive:
    """Tests for transcript archive and message purge logic."""

    async def test_archive_success_retains_completion(
        self,
        manager: SessionLifecycleManager,
        tmp_path: Path,
    ) -> None:
        session = _pending_session()
        path = tmp_path / "transcript.jsonl"
        path.write_text("{}\n")
        session.transcript_path = str(path)
        manager.session_manager.get_pending_transcript_sessions.return_value = [session]
        manager.session_manager.get.return_value = session
        with (
            patch.object(manager, "_process_session_transcript", new_callable=AsyncMock),
            patch.object(
                manager,
                "_generate_artifacts_if_needed",
                new_callable=AsyncMock,
                return_value={"success": True},
            ),
            patch(
                "gobby.sessions.transcript_processing.backup_transcript",
                return_value="/archive/path.gz",
            ) as backup,
        ):
            assert await manager._process_pending_transcripts(manager._capture_active()) == 1
        backup.assert_called_once_with(
            session.external_id,
            str(path),
            manager._capture_active().session_lifecycle.transcript_archive_dir,
        )
        manager.session_manager.mark_transcript_processed.assert_called_once()

    async def test_archive_returns_none(
        self,
        manager: SessionLifecycleManager,
        tmp_path: Path,
    ) -> None:
        session = _pending_session()
        path = tmp_path / "transcript.jsonl"
        path.write_text("{}\n")
        session.transcript_path = str(path)
        manager.session_manager.get_pending_transcript_sessions.return_value = [session]
        manager.session_manager.get.return_value = session
        with (
            patch.object(manager, "_process_session_transcript", new_callable=AsyncMock),
            patch.object(
                manager,
                "_generate_artifacts_if_needed",
                new_callable=AsyncMock,
                return_value={"success": True},
            ),
            patch(
                "gobby.sessions.transcript_processing.backup_transcript", return_value=None
            ) as backup,
        ):
            assert await manager._process_pending_transcripts(manager._capture_active()) == 1
        backup.assert_called_once_with(
            session.external_id,
            str(path),
            manager._capture_active().session_lifecycle.transcript_archive_dir,
        )
        manager.session_manager.mark_transcript_processed.assert_called_once()

    async def test_archive_failure_handled(
        self,
        manager: SessionLifecycleManager,
        tmp_path: Path,
    ) -> None:
        session = _pending_session()
        path = tmp_path / "transcript.jsonl"
        path.write_text("{}\n")
        session.transcript_path = str(path)
        manager.session_manager.get_pending_transcript_sessions.return_value = [session]
        manager.session_manager.get.return_value = session
        with (
            patch.object(manager, "_process_session_transcript", new_callable=AsyncMock),
            patch.object(
                manager,
                "_generate_artifacts_if_needed",
                new_callable=AsyncMock,
                return_value={"success": True},
            ),
            patch(
                "gobby.sessions.transcript_processing.backup_transcript",
                side_effect=OSError("Backup failed"),
            ) as backup,
        ):
            assert await manager._process_pending_transcripts(manager._capture_active()) == 1
        backup.assert_called_once_with(
            session.external_id,
            str(path),
            manager._capture_active().session_lifecycle.transcript_archive_dir,
        )
        manager.session_manager.mark_transcript_processed.assert_called_once()


class TestStartStopIdempotent:
    """Tests for start/stop idempotency."""

    @pytest.mark.asyncio
    async def test_double_start_is_noop(self, manager: SessionLifecycleManager) -> None:
        """Calling start twice doesn't create duplicate tasks."""
        await manager.start()
        task1 = manager._expire_task
        task2 = manager._process_task

        await manager.start()  # Second call should be no-op

        assert manager._expire_task is task1
        assert manager._process_task is task2

        await manager.stop()

    @pytest.mark.asyncio
    async def test_stop_without_start(self, manager: SessionLifecycleManager) -> None:
        """Calling stop without start is safe."""
        await manager.stop()
        assert manager._expire_task is None
        assert manager._process_task is None
