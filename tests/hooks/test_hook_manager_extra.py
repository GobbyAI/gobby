"""Extra tests for HookManager."""

import asyncio
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.hooks import hook_manager as hook_manager_module
from gobby.hooks.hook_manager import HookManager

pytestmark = pytest.mark.unit


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
    """Tests for _dedup_memory_results filtering and ID tracking."""

    def _make_manager(self, mock_components=None):
        patcher = patch("gobby.hooks.hook_manager.HookManagerFactory.create")
        mock_create = patcher.start()
        if mock_components is None:
            mock_components = MagicMock()
        mock_create.return_value = mock_components
        manager = HookManager()
        patcher.stop()
        return manager

    def _make_result(self, *ids):
        return {
            "success": True,
            "memories": [{"id": mid, "content": f"Memory {mid}", "type": "fact"} for mid in ids],
        }

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_filters_already_injected(self, MockSVM):
        """Previously injected memories are excluded from results."""
        mock_svm = MockSVM.return_value
        mock_svm.claim_set_variable_values.return_value = ["c"]

        manager = self._make_manager()
        result = manager._dedup_memory_results(self._make_result("a", "b", "c"), "sess-1")

        assert len(result["memories"]) == 1
        assert result["memories"][0]["id"] == "c"
        mock_svm.claim_set_variable_values.assert_called_once_with(
            "sess-1", "injected_memory_ids", ["a", "b", "c"]
        )

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_first_prompt_no_filtering(self, MockSVM):
        """First prompt (empty injected_memory_ids) injects all memories."""
        mock_svm = MockSVM.return_value
        mock_svm.claim_set_variable_values.return_value = ["a", "b"]

        manager = self._make_manager()
        result = manager._dedup_memory_results(self._make_result("a", "b"), "sess-1")

        assert len(result["memories"]) == 2
        mock_svm.claim_set_variable_values.assert_called_once_with(
            "sess-1", "injected_memory_ids", ["a", "b"]
        )

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_no_variable_set_yet(self, MockSVM):
        """Session with no injected_memory_ids variable injects all."""
        mock_svm = MockSVM.return_value
        mock_svm.claim_set_variable_values.return_value = ["x"]

        manager = self._make_manager()
        result = manager._dedup_memory_results(self._make_result("x"), "sess-1")

        assert len(result["memories"]) == 1
        mock_svm.claim_set_variable_values.assert_called_once_with(
            "sess-1", "injected_memory_ids", ["x"]
        )

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_all_filtered_returns_empty(self, MockSVM):
        """When all memories were already injected, returns empty list."""
        mock_svm = MockSVM.return_value
        mock_svm.claim_set_variable_values.return_value = []

        manager = self._make_manager()
        result = manager._dedup_memory_results(self._make_result("a", "b"), "sess-1")

        assert result["memories"] == []
        mock_svm.claim_set_variable_values.assert_called_once_with(
            "sess-1", "injected_memory_ids", ["a", "b"]
        )

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_db_error_fails_open(self, MockSVM):
        """Database errors return unfiltered results."""
        MockSVM.side_effect = RuntimeError("db unavailable")

        manager = self._make_manager()
        original = self._make_result("a", "b")
        result = manager._dedup_memory_results(original, "sess-1")

        assert result is original
        assert len(result["memories"]) == 2

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_empty_memories_skips_tracking(self, MockSVM):
        """Empty memory list doesn't call append_to_set_variable."""
        mock_svm = MockSVM.return_value
        manager = self._make_manager()
        result = manager._dedup_memory_results({"success": True, "memories": []}, "sess-1")

        assert result["memories"] == []
        mock_svm.claim_set_variable_values.assert_not_called()


class TestDedupSkillResults:
    @patch("gobby.workflows.state_manager.SessionVariableManager")
    @patch("gobby.hooks.hook_manager.HookManagerFactory.create")
    def test_filters_skills_not_atomically_claimed(self, mock_create, MockSVM):
        mock_create.return_value = MagicMock()
        mock_svm = MockSVM.return_value
        mock_svm.claim_set_variable_values.return_value = ["new-skill"]
        manager = HookManager()
        payload = {
            "results": [
                {"skill_name": "already-seen", "score": 0.9},
                {"skill_name": "new-skill", "score": 0.8},
            ]
        }

        result = manager._dedup_skill_results(payload, "sess-1")

        assert result["results"] == [{"skill_name": "new-skill", "score": 0.8}]
        assert result["count"] == 1
        mock_svm.claim_set_variable_values.assert_called_once_with(
            "sess-1", "suggested_skill_names", ["already-seen", "new-skill"]
        )
