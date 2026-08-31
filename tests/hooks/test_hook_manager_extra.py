"""Extra tests for HookManager."""

from __future__ import annotations

import asyncio
import importlib
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.hooks import hook_manager as hook_manager_module
from gobby.hooks.event_handlers._session_start.transcripts import (
    MAX_PENDING_TRANSCRIPT_RECHECKS,
)
from gobby.hooks.events import HookEvent, HookEventType, HookResponse, SessionSource
from gobby.hooks.hook_manager import HookManager
from gobby.hooks.receipt_effects import apply_acknowledged_receipt, take_worker_staging
from gobby.storage.hub.protocol import HubDatabase

pytestmark = pytest.mark.unit

SESSION_ID = "11111111-1111-4111-8111-111111111111"
_RECEIPT_MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "crates/gcore/assets/schema/migrations/414_hook_receipt_effects.sql"
)


@pytest.fixture
def receipts_db(temp_db: HubDatabase) -> HubDatabase:
    sql = "\n".join(
        line
        for line in _RECEIPT_MIGRATION.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("--")
    )
    statements = [statement.strip() for statement in sql.split(";") if statement.strip()]
    with temp_db.transaction() as conn:
        for statement in statements:
            conn.execute(statement)
    return temp_db


class _SetStore:
    def __init__(self) -> None:
        self.variables: dict[str, dict[str, Any]] = {}
        self.appended: list[tuple[str, str, list[str]]] = []
        self.claimed: list[tuple[str, str, list[str]]] = []

    def get_variables(self, session_id: str) -> dict[str, Any]:
        return dict(self.variables.get(session_id, {}))

    def merge_variables(self, session_id: str, updates: dict[str, Any]) -> bool:
        self.variables.setdefault(session_id, {}).update(updates)
        return True

    def append_to_set_variable(
        self,
        session_id: str,
        name: str,
        values: list[str],
        *,
        preserve_order: bool = False,
    ) -> bool:
        del preserve_order
        self.appended.append((session_id, name, list(values)))
        existing = list(self.variables.setdefault(session_id, {}).get(name, []))
        for value in values:
            if value not in existing:
                existing.append(value)
        self.variables[session_id][name] = existing
        return True

    def claim_set_variable_values(self, session_id: str, name: str, values: list[str]) -> list[str]:
        self.claimed.append((session_id, name, list(values)))
        return list(values)


class TestReregisterActiveSessions:
    def test_reregister_active_sessions(self):
        """Test _reregister_active_sessions calls coordinator method."""
        with patch("gobby.hooks.hook_manager.HookManagerFactory.create") as mock_create:
            mock_components = MagicMock()
            mock_create.return_value = mock_components

            manager = HookManager()

            # Reset mock to verify explicit call
            mock_components.session_coordinator.reregister_active_sessions.reset_mock()

            manager._reregister_active_sessions()
            mock_components.session_coordinator.reregister_active_sessions.assert_called_once()
            assert mock_components.session_coordinator.reregister_active_sessions.call_count == 1
            assert (
                mock_components.session_coordinator.reregister_active_sessions.call_args is not None
            )


class TestDispatchSessionSummaries:
    def test_dispatch_session_summaries_forwards_memory_manager_and_config(self) -> None:
        memory_manager = object()
        llm_service = MagicMock()
        components = MagicMock()
        components.memory_manager = memory_manager
        components.config.session_summary = "summary-config"
        dispatcher = MagicMock()
        with (
            patch(
                "gobby.hooks.hook_manager.HookManagerFactory.create",
                return_value=components,
            ),
            patch(
                "gobby.hooks.hook_manager.build_session_summary_dispatcher",
                return_value=dispatcher,
            ) as build_dispatcher,
        ):
            manager = HookManager(llm_service=llm_service)
            manager._dispatch_session_summaries("session-1", set_handoff_ready=True)

        assert not hasattr(hook_manager_module, "SessionSummaryDispatcher")
        assert manager._memory_manager is memory_manager
        assert manager._config is components.config
        assert manager._session_manager is components.session_manager
        assert manager._current_llm_service() is llm_service
        build_dispatcher.assert_called_once_with(
            session_manager=components.session_manager,
            llm_service=llm_service,
            session_summary_config="summary-config",
            database=components.database,
            loop=manager._loop,
            logger=manager.logger,
            memory_manager=memory_manager,
            config=components.config,
        )
        dispatcher.dispatch.assert_called_once_with(
            "session-1",
            _background=False,
            done_event=None,
            set_handoff_ready=True,
        )

    @patch("gobby.hooks.session_summary_dispatcher.asyncio.get_running_loop")
    def test_dispatches_on_running_loop(self, mock_get_loop) -> None:
        """Tests that a running loop uses the retained-task scheduler."""
        mock_loop = MagicMock()
        mock_get_loop.return_value = mock_loop

        with (
            patch("gobby.hooks.hook_manager.HookManagerFactory.create") as mock_create,
            patch(
                "gobby.hooks.session_summary_dispatcher.create_background_task"
            ) as mock_background_task,
        ):
            mock_components = MagicMock()
            mock_create.return_value = mock_components
            manager = HookManager()

            # Mock path resolution
            manager._resolve_summary_output_path = MagicMock(return_value="/tmp/sum")

            event = threading.Event()
            manager._dispatch_session_summaries("sess-1", done_event=event)

            mock_background_task.assert_called_once()
            coro = mock_background_task.call_args.args[0]
            assert mock_background_task.call_args.kwargs == {"loop": mock_loop}
            assert mock_get_loop.call_count == 2
            assert mock_create.call_count == 1
            coro.close()

    @pytest.mark.asyncio
    async def test_running_loop_task_is_retained_until_summary_finishes(self) -> None:
        from gobby.hooks import background_tasks

        started = asyncio.Event()
        release = asyncio.Event()

        async def generate(**_kwargs: Any) -> None:
            started.set()
            await release.wait()

        with (
            patch("gobby.sessions.summarize.generate_session_summaries", side_effect=generate),
            patch("gobby.hooks.hook_manager.HookManagerFactory.create") as mock_create,
        ):
            mock_create.return_value = MagicMock()
            manager = HookManager()
            manager._dispatch_session_summaries("sess-1")

            await started.wait()
            assert len(background_tasks._background_tasks) == 1
            task = next(iter(background_tasks._background_tasks))
            callback_complete = asyncio.Event()
            task.add_done_callback(lambda _task: callback_complete.set())

            release.set()
            await callback_complete.wait()

            assert not background_tasks._background_tasks

    @patch("gobby.hooks.session_summary_dispatcher.asyncio.get_running_loop")
    @patch("gobby.hooks.session_summary_dispatcher.asyncio.run_coroutine_threadsafe")
    @patch("gobby.sessions.summarize.generate_session_summaries", new_callable=AsyncMock)
    def test_dispatches_threadsafe_when_no_running_loop(
        self, mock_generate, mock_threadsafe, mock_get_loop
    ):
        """Tests dispatch when no running loop, but manager has a running _loop."""
        # Force RuntimeError on get_running_loop
        mock_get_loop.side_effect = RuntimeError("no loop")

        with patch("gobby.hooks.hook_manager.HookManagerFactory.create") as mock_create:
            mock_components = MagicMock()
            mock_create.return_value = mock_components
            manager = HookManager()

            # Fake that the manager has an attached loop
            manager._loop = MagicMock()
            manager._loop.is_running.return_value = True

            manager._resolve_summary_output_path = MagicMock(return_value="/tmp/sum")

            event = threading.Event()
            manager._dispatch_session_summaries("sess-1", done_event=event)

            mock_threadsafe.assert_called_once()
            assert mock_threadsafe.call_count == 1
            assert mock_threadsafe.call_args is not None

    @patch("gobby.hooks.session_summary_dispatcher.asyncio.get_running_loop")
    @patch("gobby.hooks.session_summary_dispatcher.threading.Thread")
    @patch("gobby.sessions.summarize.generate_session_summaries", new_callable=AsyncMock)
    def test_dispatches_in_new_thread(self, mock_generate, mock_thread, mock_get_loop):
        """Tests fallback to a new daemon thread when no loop is available/running."""
        mock_get_loop.side_effect = RuntimeError("no loop")

        with patch("gobby.hooks.hook_manager.HookManagerFactory.create") as mock_create:
            mock_components = MagicMock()
            mock_create.return_value = mock_components
            manager = HookManager()

            # Manager has no attached loop or it's not running
            manager._loop = None

            manager._resolve_summary_output_path = MagicMock(return_value="/tmp/sum")

            mock_thread_instance = MagicMock()
            mock_thread.return_value = mock_thread_instance

            event = threading.Event()
            manager._dispatch_session_summaries("sess-1", done_event=event)

            mock_thread.assert_called_once()
            assert mock_thread.call_count == 1
            assert mock_thread.call_args is not None
            assert mock_thread.call_args[1]["daemon"] is True
            mock_thread_instance.start.assert_called_once()
            assert mock_thread_instance.start.call_count == 1
            assert mock_thread_instance.start.call_args is not None


class TestDedupMemoryResults:
    """Prepare-without-claim memory discovery-dedupe."""

    @pytest.fixture(autouse=True)
    def _reset_staging(self) -> Iterator[None]:
        take_worker_staging()
        yield
        take_worker_staging()

    def _make_manager(self, mock_components: MagicMock | None = None) -> HookManager:
        patcher = patch("gobby.hooks.hook_manager.HookManagerFactory.create")
        mock_create = patcher.start()
        mock_create.return_value = mock_components if mock_components is not None else MagicMock()
        manager = HookManager()
        patcher.stop()
        return manager

    def _make_result(self, *ids: str) -> dict[str, Any]:
        return {
            "success": True,
            "memories": [{"id": mid, "content": f"Memory {mid}", "type": "fact"} for mid in ids],
        }

    def _prepare(
        self,
        mock_svm: MagicMock,
        *ids: str,
        already: list[str] | None = None,
        session_id: str = SESSION_ID,
        extra_memories: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        mock_svm.get_variables.return_value = {
            "injected_memory_ids": list(already or []),
        }
        payload = self._make_result(*ids)
        if extra_memories:
            payload["memories"].extend(extra_memories)
        result = self._make_manager()._dedup_memory_results(payload, session_id)
        return result, take_worker_staging()

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_prepare_filters_already_injected_without_claim(self, MockSVM: MagicMock) -> None:
        mock_svm = MockSVM.return_value
        result, staged = self._prepare(mock_svm, "a", "b", "c", already=["a", "b"])

        assert [memory["id"] for memory in result["memories"]] == ["c"]
        mock_svm.claim_set_variable_values.assert_not_called()
        mock_svm.append_to_set_variable.assert_not_called()
        assert staged["append_set_variables"]["injected_memory_ids"] == ["c"]
        assert staged["session_id"] == SESSION_ID

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_first_prompt_stages_all_ids_without_claim(self, MockSVM: MagicMock) -> None:
        mock_svm = MockSVM.return_value
        result, staged = self._prepare(mock_svm, "a", "b", already=[])

        assert len(result["memories"]) == 2
        mock_svm.claim_set_variable_values.assert_not_called()
        assert staged["append_set_variables"]["injected_memory_ids"] == ["a", "b"]

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_no_variable_set_yet_stages_without_claim(self, MockSVM: MagicMock) -> None:
        mock_svm = MockSVM.return_value
        mock_svm.get_variables.return_value = {}
        result = self._make_manager()._dedup_memory_results(self._make_result("x"), SESSION_ID)
        staged = take_worker_staging()

        assert len(result["memories"]) == 1
        mock_svm.claim_set_variable_values.assert_not_called()
        assert staged["append_set_variables"]["injected_memory_ids"] == ["x"]

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_all_filtered_returns_empty_without_staging(self, MockSVM: MagicMock) -> None:
        mock_svm = MockSVM.return_value
        result, staged = self._prepare(mock_svm, "a", "b", already=["a", "b"])

        assert result["memories"] == []
        mock_svm.claim_set_variable_values.assert_not_called()
        assert staged.get("append_set_variables", {}).get("injected_memory_ids") in (None, [])

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_id_less_memories_pass_through_without_claim(self, MockSVM: MagicMock) -> None:
        mock_svm = MockSVM.return_value
        result, staged = self._prepare(
            mock_svm,
            "a",
            already=[],
            extra_memories=[{"content": "no-id", "type": "fact"}],
        )

        assert len(result["memories"]) == 2
        assert any(not memory.get("id") for memory in result["memories"])
        mock_svm.claim_set_variable_values.assert_not_called()
        assert staged["append_set_variables"]["injected_memory_ids"] == ["a"]

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_db_error_fails_open(self, MockSVM: MagicMock) -> None:
        MockSVM.side_effect = RuntimeError("db unavailable")

        original = self._make_result("a", "b")
        result = self._make_manager()._dedup_memory_results(original, SESSION_ID)

        assert result is original
        assert len(result["memories"]) == 2
        assert take_worker_staging() == {}

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_empty_memories_skips_tracking(self, MockSVM: MagicMock) -> None:
        mock_svm = MockSVM.return_value
        result = self._make_manager()._dedup_memory_results(
            {"success": True, "memories": []}, SESSION_ID
        )

        assert result["memories"] == []
        mock_svm.get_variables.assert_not_called()
        mock_svm.claim_set_variable_values.assert_not_called()
        assert take_worker_staging() == {}

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_same_turn_second_prepare_filters_staged_without_claim(
        self, MockSVM: MagicMock
    ) -> None:
        mock_svm = MockSVM.return_value
        mock_svm.get_variables.return_value = {"injected_memory_ids": []}
        manager = self._make_manager()
        first = manager._dedup_memory_results(self._make_result("a", "b"), SESSION_ID)
        second = manager._dedup_memory_results(self._make_result("a", "b", "c"), SESSION_ID)
        staged = take_worker_staging()

        assert [memory["id"] for memory in first["memories"]] == ["a", "b"]
        assert [memory["id"] for memory in second["memories"]] == ["c"]
        mock_svm.claim_set_variable_values.assert_not_called()
        assert staged["append_set_variables"]["injected_memory_ids"] == ["a", "b", "c"]

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_acknowledged_commit_records_new_ids(self, MockSVM: MagicMock) -> None:
        mock_svm = MockSVM.return_value
        result, staged = self._prepare(mock_svm, "a", "b", already=[])
        store = _SetStore()

        apply_acknowledged_receipt(
            SimpleNamespace(receipt_id="r-ack", session_id=SESSION_ID, staged_payload=staged),
            variable_manager=store,
        )

        assert [memory["id"] for memory in result["memories"]] == ["a", "b"]
        mock_svm.claim_set_variable_values.assert_not_called()
        assert store.variables[SESSION_ID]["injected_memory_ids"] == ["a", "b"]
        assert store.claimed == []

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_transport_loss_release_allows_redelivery(
        self, MockSVM: MagicMock, receipts_db: HubDatabase
    ) -> None:
        mock_svm = MockSVM.return_value
        _result, staged = self._prepare(mock_svm, "a", already=[])
        receipts = importlib.import_module("gobby.storage.hook_receipts")
        receipt = receipts.prepare_receipt(
            receipts_db,
            session_id=SESSION_ID,
            envelope_id="env-memory-release",
            staged_payload=staged,
        )
        released = receipts.release_receipt(receipts_db, receipt_id=receipt.receipt_id)
        store = _SetStore()

        assert released is not None
        assert released.state == "released"
        mock_svm.claim_set_variable_values.assert_not_called()
        assert store.variables == {}
        assert store.appended == []

        mock_svm.get_variables.return_value = {}
        retry, retry_staged = self._prepare(mock_svm, "a", already=[])
        assert [memory["id"] for memory in retry["memories"]] == ["a"]
        assert retry_staged["append_set_variables"]["injected_memory_ids"] == ["a"]

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_duplicate_ack_is_a_noop(self, MockSVM: MagicMock, receipts_db: HubDatabase) -> None:
        mock_svm = MockSVM.return_value
        _result, staged = self._prepare(mock_svm, "a", already=[])
        receipts = importlib.import_module("gobby.storage.hook_receipts")
        receipt = receipts.prepare_receipt(
            receipts_db,
            session_id=SESSION_ID,
            envelope_id="env-memory-dup",
            staged_payload=staged,
        )
        store = _SetStore()
        first = receipts.acknowledge_receipt(
            receipts_db,
            receipt_id=receipt.receipt_id,
            delivery_generation=receipt.delivery_generation,
        )
        assert first is not None
        apply_acknowledged_receipt(first, variable_manager=store)
        duplicate = receipts.acknowledge_receipt(
            receipts_db,
            receipt_id=receipt.receipt_id,
            delivery_generation=receipt.delivery_generation,
        )
        assert duplicate is None
        apply_acknowledged_receipt(first, variable_manager=store)
        mock_svm.claim_set_variable_values.assert_not_called()
        assert store.variables[SESSION_ID]["injected_memory_ids"] == ["a"]

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_terminalization_does_not_commit(
        self, MockSVM: MagicMock, receipts_db: HubDatabase
    ) -> None:
        mock_svm = MockSVM.return_value
        _result, staged = self._prepare(mock_svm, "a", already=[])
        receipts = importlib.import_module("gobby.storage.hook_receipts")
        receipt = receipts.prepare_receipt(
            receipts_db,
            session_id=SESSION_ID,
            envelope_id="env-memory-term",
            staged_payload=staged,
        )
        store = _SetStore()
        terminalized = receipts.terminalize_receipts_for_envelope(
            receipts_db,
            envelope_id=receipt.current_envelope_id,
        )

        assert terminalized == 1
        mock_svm.claim_set_variable_values.assert_not_called()
        assert store.appended == []
        assert store.variables == {}


class TestDedupSkillResults:
    """Prepare-without-claim skill discovery-dedupe."""

    @pytest.fixture(autouse=True)
    def _reset_staging(self) -> Iterator[None]:
        take_worker_staging()
        yield
        take_worker_staging()

    def _manager(self) -> HookManager:
        with patch("gobby.hooks.hook_manager.HookManagerFactory.create", return_value=MagicMock()):
            return HookManager()

    def _payload(self, *names: str) -> dict[str, Any]:
        return {"results": [{"skill_name": name, "score": 0.9} for name in names]}

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_prepare_filters_already_suggested_without_claim(self, MockSVM: MagicMock) -> None:
        mock_svm = MockSVM.return_value
        mock_svm.get_variables.return_value = {"suggested_skill_names": ["already-seen"]}
        result = self._manager()._dedup_skill_results(
            self._payload("already-seen", "new-skill"), SESSION_ID
        )
        staged = take_worker_staging()

        assert result["results"] == [{"skill_name": "new-skill", "score": 0.9}]
        assert result["count"] == 1
        mock_svm.claim_set_variable_values.assert_not_called()
        assert staged["append_set_variables"]["suggested_skill_names"] == ["new-skill"]

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_acknowledged_commit_records_new_names(self, MockSVM: MagicMock) -> None:
        mock_svm = MockSVM.return_value
        mock_svm.get_variables.return_value = {}
        result = self._manager()._dedup_skill_results(self._payload("new-skill"), SESSION_ID)
        staged = take_worker_staging()
        store = _SetStore()
        apply_acknowledged_receipt(
            SimpleNamespace(receipt_id="r-skill", session_id=SESSION_ID, staged_payload=staged),
            variable_manager=store,
        )

        assert result["results"] == [{"skill_name": "new-skill", "score": 0.9}]
        mock_svm.claim_set_variable_values.assert_not_called()
        assert store.variables[SESSION_ID]["suggested_skill_names"] == ["new-skill"]
        assert store.claimed == []

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_transport_loss_release_allows_redelivery(
        self, MockSVM: MagicMock, receipts_db: HubDatabase
    ) -> None:
        mock_svm = MockSVM.return_value
        mock_svm.get_variables.return_value = {}
        self._manager()._dedup_skill_results(self._payload("new-skill"), SESSION_ID)
        staged = take_worker_staging()
        receipts = importlib.import_module("gobby.storage.hook_receipts")
        receipt = receipts.prepare_receipt(
            receipts_db,
            session_id=SESSION_ID,
            envelope_id="env-skill-release",
            staged_payload=staged,
        )
        released = receipts.release_receipt(receipts_db, receipt_id=receipt.receipt_id)
        store = _SetStore()
        assert released is not None
        assert released.state == "released"
        assert store.appended == []

        mock_svm.get_variables.return_value = {}
        retry = self._manager()._dedup_skill_results(self._payload("new-skill"), SESSION_ID)
        retry_staged = take_worker_staging()
        assert retry["results"][0]["skill_name"] == "new-skill"
        assert retry_staged["append_set_variables"]["suggested_skill_names"] == ["new-skill"]

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_duplicate_ack_is_a_noop(self, MockSVM: MagicMock, receipts_db: HubDatabase) -> None:
        mock_svm = MockSVM.return_value
        mock_svm.get_variables.return_value = {}
        self._manager()._dedup_skill_results(self._payload("new-skill"), SESSION_ID)
        staged = take_worker_staging()
        receipts = importlib.import_module("gobby.storage.hook_receipts")
        receipt = receipts.prepare_receipt(
            receipts_db,
            session_id=SESSION_ID,
            envelope_id="env-skill-dup",
            staged_payload=staged,
        )
        store = _SetStore()
        first = receipts.acknowledge_receipt(
            receipts_db,
            receipt_id=receipt.receipt_id,
            delivery_generation=receipt.delivery_generation,
        )
        assert first is not None
        apply_acknowledged_receipt(first, variable_manager=store)
        duplicate = receipts.acknowledge_receipt(
            receipts_db,
            receipt_id=receipt.receipt_id,
            delivery_generation=receipt.delivery_generation,
        )
        assert duplicate is None
        apply_acknowledged_receipt(first, variable_manager=store)
        assert store.variables[SESSION_ID]["suggested_skill_names"] == ["new-skill"]

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_terminalization_does_not_commit(
        self, MockSVM: MagicMock, receipts_db: HubDatabase
    ) -> None:
        mock_svm = MockSVM.return_value
        mock_svm.get_variables.return_value = {}
        self._manager()._dedup_skill_results(self._payload("new-skill"), SESSION_ID)
        staged = take_worker_staging()
        receipts = importlib.import_module("gobby.storage.hook_receipts")
        receipt = receipts.prepare_receipt(
            receipts_db,
            session_id=SESSION_ID,
            envelope_id="env-skill-term",
            staged_payload=staged,
        )
        store = _SetStore()
        assert (
            receipts.terminalize_receipts_for_envelope(
                receipts_db, envelope_id=receipt.current_envelope_id
            )
            == 1
        )
        assert store.appended == []

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_db_error_fails_open(self, MockSVM: MagicMock) -> None:
        MockSVM.side_effect = RuntimeError("db unavailable")
        original = self._payload("new-skill")
        result = self._manager()._dedup_skill_results(original, SESSION_ID)
        assert result is original
        assert take_worker_staging() == {}


_RECHECK_MACHINE_ID = "21000000-0000-4000-8000-000000000001"
_DERIVE_PATCH = "gobby.hooks.event_handlers._session_start.transcripts.derive_transcript_path"


def _recheck_event(
    event_type: HookEventType,
    *,
    platform_session_id: str | None = "platform-1",
) -> HookEvent:
    event = HookEvent(
        event_type=event_type,
        session_id="conv-1",
        source=SessionSource.AGY,
        timestamp=datetime.now(UTC),
        data={"transcript_path": "/nonexistent/transcript_full.jsonl"},
        project_id="proj-1",
        machine_id=_RECHECK_MACHINE_ID,
    )
    if platform_session_id is not None:
        event.metadata["_platform_session_id"] = platform_session_id
    return event


def _seam_manager(manager: HookManager, *, session: SimpleNamespace) -> HookManager:
    """Mock everything around the shared hook seam so events reach the recheck."""
    mocks = cast(Any, manager)
    mocks._session_manager.get.return_value = session
    mocks._record_machine_ingress = MagicMock()
    mocks._record_session_activity_pulse = MagicMock()
    mocks._evaluate_workflow_rules = MagicMock(return_value=(None, None))
    mocks._evaluate_blocking_webhooks = MagicMock(return_value=None)

    def handler(event: HookEvent) -> HookResponse:
        event.metadata["_platform_session_id"] = session.id
        return HookResponse(decision="allow")

    mocks._event_handlers.get_handler.return_value = handler

    def resolve(event: HookEvent, *, apply_session_mutations: bool = True) -> str:
        del apply_session_mutations
        event.metadata["_platform_session_id"] = session.id
        return str(session.id)

    mocks._session_lookup.resolve.side_effect = resolve
    return manager


class TestPendingTranscriptRecheckBudget:
    """The per-session recheck budget is dropped when its session starts or ends."""

    @pytest.fixture(autouse=True)
    def _local_machine(
        self, manager_with_mocks: HookManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(manager_with_mocks, "get_machine_id", lambda: _RECHECK_MACHINE_ID)

    def test_session_end_discards_only_that_sessions_budget(
        self, manager_with_mocks: HookManager
    ) -> None:
        manager_with_mocks._pending_transcript_rechecks.update({"platform-1": 3, "platform-2": 1})

        manager_with_mocks._recheck_pending_transcript(_recheck_event(HookEventType.SESSION_END))

        assert manager_with_mocks._pending_transcript_rechecks == {"platform-2": 1}

    def test_session_end_without_resolved_session_leaves_budgets_alone(
        self, manager_with_mocks: HookManager
    ) -> None:
        manager_with_mocks._pending_transcript_rechecks["platform-1"] = 2

        manager_with_mocks._recheck_pending_transcript(
            _recheck_event(HookEventType.SESSION_END, platform_session_id=None)
        )

        assert manager_with_mocks._pending_transcript_rechecks == {"platform-1": 2}

    @pytest.mark.parametrize("event_type", [HookEventType.SESSION_END, HookEventType.SESSION_START])
    def test_hook_seam_discards_budget_for_ending_and_starting_sessions(
        self, manager_with_mocks: HookManager, event_type: HookEventType
    ) -> None:
        session = SimpleNamespace(
            id="platform-1",
            transcript_path=None,
            source="agy",
            external_id="conv-1",
            machine_id=_RECHECK_MACHINE_ID,
            status="active",
        )
        manager = _seam_manager(manager_with_mocks, session=session)
        manager._pending_transcript_rechecks["platform-1"] = MAX_PENDING_TRANSCRIPT_RECHECKS

        with (
            patch("gobby.hooks.hook_manager.reconcile_session_activation"),
            patch(_DERIVE_PATCH, return_value=None),
        ):
            response = manager._handle_after_daemon_ready(
                _recheck_event(event_type, platform_session_id=None)
            )

        assert response.decision == "allow"
        assert manager._pending_transcript_rechecks == {}

    def test_budget_exhaustion_stops_derivation_until_the_session_ends(
        self, manager_with_mocks: HookManager
    ) -> None:
        session = SimpleNamespace(
            id="platform-1",
            transcript_path=None,
            source="agy",
            external_id="conv-1",
            machine_id=_RECHECK_MACHINE_ID,
        )
        manager = cast(Any, manager_with_mocks)
        manager._session_manager.get.return_value = session
        budgets = manager_with_mocks._pending_transcript_rechecks

        with patch(_DERIVE_PATCH, return_value=None) as derive:
            for _ in range(MAX_PENDING_TRANSCRIPT_RECHECKS + 2):
                manager_with_mocks._recheck_pending_transcript(_recheck_event(HookEventType.STOP))
        assert derive.call_count == MAX_PENDING_TRANSCRIPT_RECHECKS
        assert budgets == {"platform-1": MAX_PENDING_TRANSCRIPT_RECHECKS}

        # The path appearing later is refused once the budget is spent.
        with patch(_DERIVE_PATCH, return_value="/tmp/appeared.jsonl") as derive:
            manager_with_mocks._recheck_pending_transcript(_recheck_event(HookEventType.STOP))
        derive.assert_not_called()
        manager._session_manager.update.assert_not_called()

        manager_with_mocks._recheck_pending_transcript(_recheck_event(HookEventType.SESSION_END))
        assert budgets == {}

        # The same platform session id gets a fresh window after the end.
        with patch(_DERIVE_PATCH, return_value="/tmp/appeared.jsonl") as derive:
            manager_with_mocks._recheck_pending_transcript(_recheck_event(HookEventType.STOP))
        derive.assert_called_once()
        manager._session_manager.update.assert_called_once_with(
            "platform-1", transcript_path="/tmp/appeared.jsonl"
        )
        assert budgets == {}
