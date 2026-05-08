"""Extra tests for HookManager."""

import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
    @patch("gobby.hooks.hook_manager.asyncio.get_running_loop")
    @patch("gobby.hooks.hook_manager.asyncio.run_coroutine_threadsafe")
    @patch("gobby.sessions.summarize.generate_session_summaries", new_callable=AsyncMock)
    def test_dispatches_on_running_loop(self, mock_generate, mock_threadsafe, mock_get_loop):
        """Tests that if a running loop exists, it creates a task on it."""
        mock_loop = MagicMock()
        mock_get_loop.return_value = mock_loop

        with patch("gobby.hooks.hook_manager.HookManagerFactory.create") as mock_create:
            mock_components = MagicMock()
            mock_create.return_value = mock_components
            manager = HookManager()

            # Mock path resolution
            manager._resolve_summary_output_path = MagicMock(return_value="/tmp/sum")

            event = threading.Event()
            manager._dispatch_session_summaries("sess-1", done_event=event)

            # It should create a task on the running loop
            mock_loop.create_task.assert_called_once()
            assert mock_loop.create_task.call_count == 1
            assert mock_loop.create_task.call_args is not None

            # To test the side-effects of the coro, we'd have to execute it,
            # but we just verified the correct branch was taken.

    @patch("gobby.hooks.hook_manager.asyncio.get_running_loop")
    @patch("gobby.hooks.hook_manager.asyncio.run_coroutine_threadsafe")
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

    @patch("gobby.hooks.hook_manager.asyncio.get_running_loop")
    @patch("threading.Thread")
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
        mock_svm.get_variables.return_value = {"injected_memory_ids": ["a", "b"]}

        manager = self._make_manager()
        result = manager._dedup_memory_results(self._make_result("a", "b", "c"), "sess-1")

        assert len(result["memories"]) == 1
        assert result["memories"][0]["id"] == "c"
        mock_svm.append_to_set_variable.assert_called_once_with(
            "sess-1", "injected_memory_ids", ["c"]
        )

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_first_prompt_no_filtering(self, MockSVM):
        """First prompt (empty injected_memory_ids) injects all memories."""
        mock_svm = MockSVM.return_value
        mock_svm.get_variables.return_value = {"injected_memory_ids": []}

        manager = self._make_manager()
        result = manager._dedup_memory_results(self._make_result("a", "b"), "sess-1")

        assert len(result["memories"]) == 2
        mock_svm.append_to_set_variable.assert_called_once_with(
            "sess-1", "injected_memory_ids", ["a", "b"]
        )

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_no_variable_set_yet(self, MockSVM):
        """Session with no injected_memory_ids variable injects all."""
        mock_svm = MockSVM.return_value
        mock_svm.get_variables.return_value = {}

        manager = self._make_manager()
        result = manager._dedup_memory_results(self._make_result("x"), "sess-1")

        assert len(result["memories"]) == 1
        mock_svm.append_to_set_variable.assert_called_once_with(
            "sess-1", "injected_memory_ids", ["x"]
        )

    @patch("gobby.workflows.state_manager.SessionVariableManager")
    def test_all_filtered_returns_empty(self, MockSVM):
        """When all memories were already injected, returns empty list."""
        mock_svm = MockSVM.return_value
        mock_svm.get_variables.return_value = {"injected_memory_ids": ["a", "b"]}

        manager = self._make_manager()
        result = manager._dedup_memory_results(self._make_result("a", "b"), "sess-1")

        assert result["memories"] == []
        mock_svm.append_to_set_variable.assert_not_called()

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
        mock_svm.get_variables.return_value = {}

        manager = self._make_manager()
        result = manager._dedup_memory_results({"success": True, "memories": []}, "sess-1")

        assert result["memories"] == []
        mock_svm.append_to_set_variable.assert_not_called()
