"""Tests for the memory CLI module."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner, Result

from gobby import paths
from gobby.cli import cli
from gobby.cli.memory.main import memory as memory_cli
from gobby.sync.memories import MemoryBackupError, MemoryRestoreError

pytestmark = pytest.mark.unit


class _FakeHTTPResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    @property
    def is_error(self) -> bool:
        return self.status_code >= 400

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


class TestMemoryCreateCommand:
    """Tests for gobby memory create command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @patch("gobby.cli.memory.resolve_project_ref")
    @patch("gobby.cli.memory.get_memory_manager")
    def test_create_without_project_uses_current_project_context(
        self,
        mock_get_manager: MagicMock,
        mock_resolve_project: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test create resolves current project when --project is omitted."""
        mock_resolve_project.return_value = "proj-current"
        mock_manager = MagicMock()
        mock_manager.create_memory = AsyncMock(
            return_value=SimpleNamespace(id="mem-1", content="remember this")
        )
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(
            cli,
            [
                "memory",
                "create",
                "remember this",
                "--rationale",
                "Future sessions should reuse this reminder.",
            ],
        )

        assert result.exit_code == 0
        assert "Created memory: mem-1 - remember this" in result.output
        mock_resolve_project.assert_called_once_with(None)
        mock_manager.create_memory.assert_awaited_once_with(
            content="remember this",
            memory_type="fact",
            project_id="proj-current",
            source_type="user",
            rationale="Future sessions should reuse this reminder.",
        )

    @patch("gobby.cli.memory.get_memory_manager")
    def test_create_rejects_noncanonical_memory_type(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        result = runner.invoke(
            cli,
            [
                "memory",
                "create",
                "Bad type",
                "--type",
                "debugging_pattern",
                "--rationale",
                "Future sessions should reuse this reminder.",
            ],
        )

        assert result.exit_code == 2
        assert "Invalid value for '--type'" in result.output
        mock_get_manager.assert_not_called()

    def test_create_requires_rationale(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["memory", "create", "remember this"])

        assert result.exit_code == 2
        assert "Missing option '--rationale'" in result.output


class TestMemoryShowCommand:
    """Tests for gobby memory show command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @patch("gobby.cli.memory.resolve_memory_id")
    @patch("gobby.cli.memory.get_memory_manager")
    def test_show_success(
        self,
        mock_get_manager: MagicMock,
        mock_resolve: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test showing a memory item successfully (mocked)."""
        mock_manager = MagicMock()
        mock_item = MagicMock()
        mock_item.id = "mem-123"
        mock_item.content = "Remember this"
        mock_item.memory_type = "fact"
        mock_item.created_at = "2024-01-01"
        mock_item.updated_at = "2024-01-01"
        mock_item.source_type = "user"
        mock_item.access_count = 0
        mock_item.tags = []

        mock_manager.get_memory.return_value = mock_item
        mock_get_manager.return_value = mock_manager

        mock_resolve.return_value = "mem-123"

        result = runner.invoke(cli, ["memory", "show", "mem-123"])

        assert result.exit_code == 0
        assert "ID: mem-123" in result.output
        assert "Remember this" in result.output

    def test_show_help(self, runner: CliRunner) -> None:
        """Test show --help."""
        result = runner.invoke(cli, ["memory", "show", "--help"])
        assert result.exit_code == 0
        assert "Show details of a specific memory" in result.output


class TestMemoryDeleteCommand:
    """Tests for gobby memory delete command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @patch("gobby.cli.memory.resolve_memory_id")
    @patch("gobby.cli.memory.get_memory_manager")
    def test_delete_success(
        self,
        mock_get_manager: MagicMock,
        mock_resolve: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test deleting a memory item."""
        mock_manager = MagicMock()
        mock_manager.delete_memory = AsyncMock(return_value=True)
        mock_get_manager.return_value = mock_manager
        mock_resolve.return_value = "mem-del123"

        result = runner.invoke(cli, ["memory", "delete", "mem-del123"])

        assert result.exit_code == 0
        assert "Deleted memory: mem-del123" in result.output
        mock_manager.delete_memory.assert_called_once_with("mem-del123")


class TestMemoryUpdateCommand:
    """Tests for gobby memory update command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @patch("gobby.cli.memory.resolve_memory_id")
    @patch("gobby.cli.memory.get_memory_manager")
    def test_update_success(
        self,
        mock_get_manager: MagicMock,
        mock_resolve: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test updating a memory item."""
        mock_manager = MagicMock()
        mock_mem = MagicMock()
        mock_mem.id = "mem-up123"
        mock_mem.content = "New content"
        mock_manager.update_memory = AsyncMock(return_value=mock_mem)

        mock_get_manager.return_value = mock_manager
        mock_resolve.return_value = "mem-up123"

        result = runner.invoke(cli, ["memory", "update", "mem-up123", "--content", "New content"])

        assert result.exit_code == 0
        assert "Updated memory: mem-up123" in result.output
        mock_manager.update_memory.assert_called_once_with(
            memory_id="mem-up123", content="New content", tags=None
        )

    @patch("gobby.cli.memory.resolve_memory_id")
    @patch("gobby.cli.memory.get_memory_manager")
    def test_update_with_tags(
        self,
        mock_get_manager: MagicMock,
        mock_resolve: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test updating a memory with tags."""
        mock_manager = MagicMock()
        mock_mem = MagicMock()
        mock_mem.id = "mem-up123"
        mock_mem.content = "Content"
        mock_manager.update_memory = AsyncMock(return_value=mock_mem)

        mock_get_manager.return_value = mock_manager
        mock_resolve.return_value = "mem-up123"

        result = runner.invoke(cli, ["memory", "update", "mem-up123", "--tags", "tag1, tag2, tag3"])

        assert result.exit_code == 0
        mock_manager.update_memory.assert_called_once_with(
            memory_id="mem-up123",
            content=None,
            tags=["tag1", "tag2", "tag3"],
        )
        assert mock_manager.update_memory.call_count == 1
        assert mock_manager.update_memory.call_args is not None

    @patch("gobby.cli.memory.resolve_memory_id")
    @patch("gobby.cli.memory.get_memory_manager")
    def test_update_with_empty_tags(
        self,
        mock_get_manager: MagicMock,
        mock_resolve: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test updating with empty tags string."""
        mock_manager = MagicMock()
        mock_mem = MagicMock()
        mock_mem.id = "mem-up123"
        mock_mem.content = "Content"
        mock_manager.update_memory = AsyncMock(return_value=mock_mem)

        mock_get_manager.return_value = mock_manager
        mock_resolve.return_value = "mem-up123"

        # Empty tags string should become None
        result = runner.invoke(cli, ["memory", "update", "mem-up123", "--tags", ""])

        assert result.exit_code == 0
        mock_manager.update_memory.assert_called_once_with(
            memory_id="mem-up123", content=None, tags=None
        )
        assert mock_manager.update_memory.call_count == 1
        assert mock_manager.update_memory.call_args is not None


class TestMemoryRecallCommand:
    """Tests for gobby memory recall command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @pytest.mark.unit
    @patch("gobby.cli.memory.get_memory_manager")
    def test_recall_no_results(self, mock_get_manager: MagicMock, runner: CliRunner) -> None:
        """Test recall with no results."""
        mock_manager = MagicMock()
        mock_manager.search_memories = AsyncMock(return_value=[])
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["memory", "recall", "test query"])

        assert result.exit_code == 0
        assert "No memories found" in result.output

    @patch("gobby.cli.memory.get_memory_manager")
    def test_recall_with_results(self, mock_get_manager: MagicMock, runner: CliRunner) -> None:
        """Test recall with results."""
        mock_manager = MagicMock()
        mock_mem = MagicMock()
        mock_mem.id = "mem-123456"
        mock_mem.memory_type = "fact"
        mock_mem.content = "Test content"
        mock_mem.tags = ["tag1", "tag2"]
        mock_manager.search_memories = AsyncMock(return_value=[mock_mem])
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["memory", "recall", "test"])

        assert result.exit_code == 0
        assert "[mem-1234]" in result.output
        assert "fact" in result.output
        assert "[tag1, tag2]" in result.output

    @patch("gobby.cli.memory.get_memory_manager")
    def test_recall_with_tag_filters(self, mock_get_manager: MagicMock, runner: CliRunner) -> None:
        """Test recall with tag filters."""
        mock_manager = MagicMock()
        mock_manager.search_memories = AsyncMock(return_value=[])
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(
            cli,
            [
                "memory",
                "recall",
                "query",
                "--tags-all",
                "tag1, tag2",
                "--tags-any",
                "tag3, tag4",
                "--tags-none",
                "excluded",
            ],
        )

        assert result.exit_code == 0
        mock_manager.search_memories.assert_called_once()
        call_kwargs = mock_manager.search_memories.call_args[1]
        assert call_kwargs["tags_all"] == ["tag1", "tag2"]
        assert call_kwargs["tags_any"] == ["tag3", "tag4"]
        assert call_kwargs["tags_none"] == ["excluded"]

    @patch("gobby.cli.memory.get_memory_manager")
    def test_recall_rejects_noncanonical_memory_type(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        result = runner.invoke(
            cli,
            ["memory", "recall", "query", "--type", "debugging_pattern"],
        )

        assert result.exit_code == 2
        assert "Invalid value for '--type'" in result.output
        mock_get_manager.assert_not_called()


class TestMemoryListCommand:
    """Tests for gobby memory list command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @pytest.mark.unit
    @patch("gobby.cli.memory.get_memory_manager")
    def test_list_no_results(self, mock_get_manager: MagicMock, runner: CliRunner) -> None:
        """Test list with no results."""
        mock_manager = MagicMock()
        mock_manager.list_memories.return_value = []
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["memory", "list"])

        assert result.exit_code == 0
        assert "No memories found" in result.output

    @patch("gobby.cli.memory.get_memory_manager")
    def test_list_with_results(self, mock_get_manager: MagicMock, runner: CliRunner) -> None:
        """Test list with results."""
        mock_manager = MagicMock()
        mock_mem = MagicMock()
        mock_mem.id = "mem-123456789"
        mock_mem.memory_type = "preference"
        mock_mem.content = "x" * 150  # Long content
        mock_mem.tags = []
        mock_manager.list_memories.return_value = [mock_mem]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["memory", "list"])

        assert result.exit_code == 0
        assert "[mem-1234]" in result.output
        assert "preference" in result.output
        assert "..." in result.output  # Truncated content

    @patch("gobby.cli.memory.get_memory_manager")
    def test_list_with_tags(self, mock_get_manager: MagicMock, runner: CliRunner) -> None:
        """Test list with tag display."""
        mock_manager = MagicMock()
        mock_mem = MagicMock()
        mock_mem.id = "mem-123456789"
        mock_mem.memory_type = "fact"
        mock_mem.content = "short"
        mock_mem.tags = ["important", "code"]
        mock_manager.list_memories.return_value = [mock_mem]
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["memory", "list"])

        assert result.exit_code == 0
        assert "[important, code]" in result.output

    @patch("gobby.cli.memory.get_memory_manager")
    def test_list_with_filters(self, mock_get_manager: MagicMock, runner: CliRunner) -> None:
        """Test list with all filters."""
        mock_manager = MagicMock()
        mock_manager.list_memories.return_value = []
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(
            cli,
            [
                "memory",
                "list",
                "--type",
                "fact",
                "--limit",
                "20",
                "--tags-all",
                "tag1",
            ],
        )

        assert result.exit_code == 0
        call_kwargs = mock_manager.list_memories.call_args[1]
        assert call_kwargs["memory_type"] == "fact"
        assert call_kwargs["limit"] == 20

    @patch("gobby.cli.memory.get_memory_manager")
    def test_list_rejects_noncanonical_memory_type(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        result = runner.invoke(
            cli,
            ["memory", "list", "--type", "debugging_pattern"],
        )

        assert result.exit_code == 2
        assert "Invalid value for '--type'" in result.output
        mock_get_manager.assert_not_called()


class TestMemoryStatsCommand:
    """Tests for gobby memory stats command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @pytest.mark.unit
    @patch("gobby.cli.memory.get_memory_manager")
    def test_stats_output(self, mock_get_manager: MagicMock, runner: CliRunner) -> None:
        """Test stats command output formatting."""
        mock_manager = MagicMock()
        mock_manager.get_stats = AsyncMock(
            return_value={
                "total_count": 42,
                "by_type": {"fact": 20, "preference": 15, "context": 7},
            }
        )
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["memory", "stats"])

        assert result.exit_code == 0
        assert "Total Memories: 42" in result.output
        assert "fact: 20" in result.output
        assert "preference: 15" in result.output

    @patch("gobby.cli.memory.get_memory_manager")
    def test_stats_empty_by_type(self, mock_get_manager: MagicMock, runner: CliRunner) -> None:
        """Test stats with no type breakdown."""
        mock_manager = MagicMock()
        mock_manager.get_stats = AsyncMock(
            return_value={
                "total_count": 0,
                "by_type": {},
            }
        )
        mock_get_manager.return_value = mock_manager

        result = runner.invoke(cli, ["memory", "stats"])

        assert result.exit_code == 0
        assert "Total Memories: 0" in result.output


class TestMemoryReconcileCommand:
    """Tests for gobby memory reconcile command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @patch("gobby.cli.memory._get_daemon_client")
    def test_reconcile_uses_hub_memory_count_label(
        self,
        mock_get_daemon_client: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Reconcile output should describe the hub as the memory source."""
        response = MagicMock()
        response.json.return_value = {
            "storage_count": 7,
            "qdrant": {"orphans_found": 1, "orphans_deleted": 0},
            "falkordb": {
                "orphan_memories_found": 2,
                "orphan_memories_deleted": 0,
                "orphan_entities_deleted": 0,
            },
        }
        client = MagicMock()
        client.call_http_api.return_value = response
        mock_get_daemon_client.return_value = client

        result = runner.invoke(cli, ["memory", "reconcile", "--dry-run"])

        assert result.exit_code == 0
        assert "Hub memories: 7" in result.output
        client.call_http_api.assert_called_once_with(
            "/api/memories/reconcile?dry_run=true", method="POST", timeout=600.0
        )


def _dream_client(*responses: Any) -> MagicMock:
    client = MagicMock()
    client.call_http_api.side_effect = list(responses)
    return client


def _dream_started(run_id: str = "run-1") -> _FakeHTTPResponse:
    return _FakeHTTPResponse(
        {"success": True, "run_id": run_id, "status": "running", "coalesced": False},
        status_code=202,
    )


def _dream_run_status(
    status: str,
    *,
    run_id: str = "run-1",
    checkpoint: dict[str, Any] | None = None,
    summary: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
    error: str | None = None,
) -> _FakeHTTPResponse:
    return _FakeHTTPResponse(
        {
            "success": True,
            "run": {
                "id": run_id,
                "status": status,
                "checkpoint": checkpoint,
                "summary": summary,
                "plan": plan,
                "error": error,
            },
        }
    )


class TestMemoryDreamCommand:
    """Tests for gobby memory dream command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def _invoke(
        self, runner: CliRunner, client: MagicMock, args: list[str]
    ) -> tuple[Result, MagicMock]:
        with (
            patch("gobby.cli.memory.dream._get_daemon_client", return_value=client),
            patch("gobby.cli.memory.dream.time.sleep") as mock_sleep,
        ):
            result = runner.invoke(
                memory_cli,
                ["dream", *args],
                obj={"config": MagicMock(daemon_port=60887)},
            )
        return result, mock_sleep

    def test_dream_starts_async_and_polls_to_completed(self, runner: CliRunner) -> None:
        """3.2.1: prints the run ID immediately, polls changed progress, renders summary."""
        checkpoint = {
            "phase": "sweep",
            "scope": "proj-1",
            "pass_number": 1,
            "batch_number": 2,
            "completed": 10,
            "remaining": None,
            "mutations": 1,
            "backlog": {},
            "stop_reason": None,
            "last_dependency_failure": None,
        }
        done_checkpoint = {**checkpoint, "batch_number": 3, "completed": 25, "mutations": 4}
        client = _dream_client(
            _dream_started(),
            _dream_run_status("running", checkpoint=checkpoint),
            _dream_run_status("running", checkpoint=checkpoint),
            _dream_run_status(
                "completed",
                checkpoint=done_checkpoint,
                summary={"candidates_reviewed": 25, "mutations": 4, "snapshots": 4, "errors": 0},
                plan={"dry_run": False},
            ),
        )

        result, mock_sleep = self._invoke(runner, client, [])

        assert result.exit_code == 0
        assert "Started dream run: run-1" in result.output
        # An unchanged checkpoint is rendered once, not once per poll.
        progress = "[sweep] scope=proj-1 pass=1 batch=2 completed=10 mutations=1"
        assert result.output.count(progress) == 1
        assert "[sweep] scope=proj-1 pass=1 batch=3 completed=25 mutations=4" in result.output
        assert "Dream run run-1 completed" in result.output
        assert "Candidates reviewed: 25" in result.output
        # Default --timeout 0 imposes no client deadline: the wait never expires.
        assert "still running" not in result.output
        post = client.call_http_api.call_args_list[0]
        assert post.args == ("/memory/dream",)
        assert post.kwargs["method"] == "POST"
        # The trigger is asynchronous: short network timeout, no wait parameter.
        assert post.kwargs["timeout"] == 30.0
        assert "wait" not in post.kwargs["json_data"]
        assert "project_id" not in post.kwargs["json_data"]
        for poll in client.call_http_api.call_args_list[1:]:
            assert poll.args == ("/memory/dream/run-1",)
            assert poll.kwargs["method"] == "GET"
        mock_sleep.assert_called_with(2.0)

    def test_dream_passes_flags_to_the_trigger(self, runner: CliRunner) -> None:
        client = _dream_client(
            _dream_started(),
            _dream_run_status(
                "completed",
                summary={"candidates_reviewed": 0, "mutations": 0, "snapshots": 0, "errors": 0},
            ),
        )

        result, _ = self._invoke(runner, client, ["--full", "--memory-type", "fact", "--dry-run"])

        assert result.exit_code == 0
        post = client.call_http_api.call_args_list[0]
        assert post.kwargs["json_data"] == {
            "dry_run": True,
            "skip_consolidation": False,
            "memory_type": "fact",
            "full_sweep": True,
        }

    def test_dream_explicit_timeout_stops_wait_and_prints_resume_command(
        self, runner: CliRunner
    ) -> None:
        """3.2.2: an explicit deadline stops only the CLI wait and leaves the run active."""
        client = _dream_client(
            _dream_started(),
            _dream_run_status("running"),
        )

        with (
            patch("gobby.cli.memory.dream._get_daemon_client", return_value=client),
            patch("gobby.cli.memory.dream.time.sleep") as mock_sleep,
            patch("gobby.cli.memory.dream.time.monotonic", side_effect=[0.0, 100.0]),
        ):
            result = runner.invoke(
                memory_cli,
                ["dream", "--timeout", "5"],
                obj={"config": MagicMock(daemon_port=60887)},
            )

        assert result.exit_code == 0
        assert "Dream run run-1 is still running" in result.output
        assert "Resume observation with: gobby memory dream status run-1" in result.output
        # POST plus a single poll; the daemon run was never cancelled.
        assert client.call_http_api.call_count == 2
        mock_sleep.assert_not_called()

    def test_dream_ctrl_c_preserves_run_and_prints_resume_command(self, runner: CliRunner) -> None:
        """3.2.2: Ctrl-C stops only the CLI wait and prints the resume command."""
        client = _dream_client(
            _dream_started(),
            _dream_run_status("running"),
            KeyboardInterrupt(),
        )

        result, _ = self._invoke(runner, client, [])

        assert result.exit_code != 0
        assert "Stopped watching; dream run run-1 keeps running in the daemon." in result.output
        assert "Resume observation with: gobby memory dream status run-1" in result.output
        assert client.call_http_api.call_count == 3

    def test_dream_partial_outcome_renders_stop_and_backlog_nonzero_exit(
        self, runner: CliRunner
    ) -> None:
        """3.2.3: partial window/dependency outcomes render stop semantics, exit non-zero."""
        checkpoint = {
            "phase": "coordinator",
            "scope": "all-due",
            "pass_number": 2,
            "batch_number": 5,
            "completed": 37,
            "remaining": 88,
            "mutations": 4,
            "backlog": {"proj-1": 60, "proj-2": 28},
            "stop_reason": "window_exhausted",
            "last_dependency_failure": "planner timed out",
        }
        client = _dream_client(
            _dream_started(),
            _dream_run_status(
                "partial",
                checkpoint=checkpoint,
                summary={
                    "targets": 3,
                    "completed": 1,
                    "failed": 0,
                    "mutations": 4,
                    "stop_reason": "window_exhausted",
                },
                plan={"aggregate": True, "runs": []},
            ),
        )

        result, _ = self._invoke(runner, client, [])

        assert result.exit_code == 1
        assert "Dream run run-1 partial" in result.output
        assert "Stop reason: window_exhausted" in result.output
        assert "Completed: 37 candidate(s), 4 mutation(s)" in result.output
        assert "Remaining: 88 candidate(s)" in result.output
        assert '"proj-1": 60' in result.output
        assert "retry=planner timed out" in result.output

    @pytest.mark.parametrize("status", ["failed", "interrupted"])
    def test_dream_failure_statuses_exit_nonzero(self, runner: CliRunner, status: str) -> None:
        """3.2.3: failed and interrupted terminal runs exit non-zero with the error."""
        client = _dream_client(
            _dream_started(),
            _dream_run_status(status, error="boom"),
        )

        result, _ = self._invoke(runner, client, [])

        assert result.exit_code == 1
        assert f"Dream run run-1 {status}" in result.output
        assert "Error: boom" in result.output

    def test_dream_inventory_only_renders_candidates_remain_due(self, runner: CliRunner) -> None:
        """3.2.3: inventory-only runs render candidate IDs/counts and state they stay due."""
        client = _dream_client(
            _dream_started(),
            _dream_run_status(
                "completed",
                summary={
                    "skip_consolidation": True,
                    "candidates_eligible": 3,
                    "mutations": 0,
                    "snapshots": 0,
                    "errors": 0,
                },
                plan={
                    "skip_consolidation": True,
                    "candidate_count": 3,
                    "candidate_ids": ["21000000-0000-4000-8000-000000000005", "m2", "m3"],
                    "candidate_ids_truncated": False,
                },
            ),
        )

        result, _ = self._invoke(runner, client, ["--skip-consolidation"])

        assert result.exit_code == 0
        assert "Inventory-only run: 3 candidate(s) eligible" in result.output
        assert "candidates remain due" in result.output
        assert "Candidate IDs: 21000000-0000-4000-8000-000000000005, m2, m3" in result.output
        post = client.call_http_api.call_args_list[0]
        assert post.kwargs["json_data"]["skip_consolidation"] is True

    def test_dream_coalesced_trigger_polls_the_active_run(self, runner: CliRunner) -> None:
        active_checkpoint = {
            "phase": "sweep",
            "scope": "proj-1",
            "pass_number": 1,
            "batch_number": 1,
            "completed": 5,
            "remaining": None,
            "mutations": 0,
        }
        client = _dream_client(
            _FakeHTTPResponse(
                {
                    "success": True,
                    "run_id": "run-9",
                    "coalesced": True,
                    "status": "running",
                    "active": {"run_id": "run-9", "checkpoint": active_checkpoint},
                }
            ),
            _dream_run_status(
                "completed",
                run_id="run-9",
                summary={"candidates_reviewed": 5, "mutations": 0, "snapshots": 0, "errors": 0},
            ),
        )

        result, _ = self._invoke(runner, client, [])

        assert result.exit_code == 0
        assert "Coalesced onto active dream run: run-9" in result.output
        assert "[sweep] scope=proj-1 pass=1 batch=1 completed=5 mutations=0" in result.output
        poll = client.call_http_api.call_args_list[1]
        assert poll.args == ("/memory/dream/run-9",)

    def test_dream_aggregate_completed_renders_per_project_summary(self, runner: CliRunner) -> None:
        client = _dream_client(
            _dream_started(),
            _dream_run_status(
                "completed",
                summary={
                    "targets": 2,
                    "completed": 2,
                    "failed": 0,
                    "mutations": 3,
                    "passes": 1,
                    "stop_reason": "drained",
                },
                plan={
                    "aggregate": True,
                    "runs": [
                        {"project_id": "proj-1", "success": True, "run_id": "r1", "mutations": 1},
                        {"project_id": None, "success": True, "run_id": "r2", "mutations": 2},
                    ],
                },
            ),
        )

        result, _ = self._invoke(runner, client, [])

        assert result.exit_code == 0
        assert "Swept 2/2 project(s): 3 mutation(s) total" in result.output
        assert "proj-1: 1 mutation(s) (run r1)" in result.output
        assert "global: 2 mutation(s) (run r2)" in result.output

    def test_dream_raises_on_unsuccessful_response(self, runner: CliRunner) -> None:
        client = _dream_client(
            _FakeHTTPResponse(
                {"success": False, "error": "memory dream is disabled"}, status_code=400
            ),
        )

        result, _ = self._invoke(runner, client, [])

        assert result.exit_code != 0
        assert "memory dream is disabled" in result.output

    def test_dream_status_renders_checkpoint_progress(self, runner: CliRunner) -> None:
        client = _dream_client(
            _dream_run_status(
                "running",
                checkpoint={
                    "phase": "sweep",
                    "scope": "proj-1",
                    "pass_number": 1,
                    "batch_number": 2,
                    "completed": 10,
                    "remaining": None,
                    "mutations": 1,
                },
            ),
        )

        with patch("gobby.cli.memory.dream._get_daemon_client", return_value=client):
            result = runner.invoke(
                memory_cli,
                ["dream", "status", "run-1"],
                obj={"config": MagicMock(daemon_port=60887)},
            )

        assert result.exit_code == 0
        assert "Dream run: run-1" in result.output
        assert "Status: running" in result.output
        assert "[sweep] scope=proj-1 pass=1 batch=2 completed=10 mutations=1" in result.output


class TestMemoryBackupCommand:
    """Tests for gobby memory backup command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @patch("gobby.sync.memories.MemoryBackupManager")
    @patch("gobby.cli.memory.get_memory_manager")
    def test_default_backup_runs_without_export_context(
        self,
        mock_get_manager: MagicMock,
        mock_backup_manager_cls: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_manager = MagicMock()
        mock_manager.db = MagicMock()
        mock_get_manager.return_value = mock_manager
        mock_backup_manager = MagicMock()
        mock_backup_manager.backup_sync.return_value = 4
        mock_backup_manager_cls.return_value = mock_backup_manager

        result = runner.invoke(cli, ["memory", "backup"])

        assert result.exit_code == 0
        assert "Backed up 4 memories" in result.output
        mock_backup_manager.backup_sync.assert_called_once()

    @patch("gobby.sync.memories.MemoryBackupManager")
    @patch("gobby.cli.memory.get_memory_manager")
    def test_backup_quiet_mode(
        self,
        mock_get_manager: MagicMock,
        mock_backup_manager_cls: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_manager = MagicMock()
        mock_manager.db = MagicMock()
        mock_get_manager.return_value = mock_manager
        mock_backup_manager = MagicMock()
        mock_backup_manager.backup_sync.return_value = 4
        mock_backup_manager_cls.return_value = mock_backup_manager

        result = runner.invoke(cli, ["memory", "backup", "--quiet"])

        assert result.exit_code == 0
        assert result.output == ""
        mock_backup_manager.backup_sync.assert_called_once()

    def test_backup_force_option_is_removed(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["memory", "backup", "--force"])

        assert result.exit_code != 0
        assert "No such option '--force'" in result.output

    @patch("gobby.sync.memories.MemoryBackupManager")
    @patch("gobby.cli.memory.get_memory_manager")
    def test_backup_export_error_fails(
        self,
        mock_get_manager: MagicMock,
        mock_backup_manager_cls: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_manager = MagicMock()
        mock_manager.db = MagicMock()
        mock_get_manager.return_value = mock_manager
        mock_backup_manager = MagicMock()
        mock_backup_manager.backup_sync.side_effect = MemoryBackupError("backup failed")
        mock_backup_manager_cls.return_value = mock_backup_manager

        result = runner.invoke(cli, ["memory", "backup", "--output", "memories.jsonl"])

        assert result.exit_code == 1
        assert "backup failed" in result.output


class TestMemoryRestoreCommand:
    """Tests for gobby memory restore command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @patch("gobby.sync.memories.MemoryBackupManager")
    @patch("gobby.cli.memory.get_memory_manager")
    def test_restore_default_path(
        self,
        mock_get_manager: MagicMock,
        mock_backup_manager_cls: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Default restore imports from .gobby/memories.jsonl."""
        mock_manager = MagicMock()
        mock_manager.db = MagicMock()
        mock_get_manager.return_value = mock_manager
        mock_backup_manager = MagicMock()
        mock_backup_manager.restore_sync.return_value = 3
        mock_backup_manager_cls.return_value = mock_backup_manager

        with runner.isolated_filesystem():
            restore_path = paths.get_gobby_home() / "backups" / "memories.jsonl"
            restore_path.parent.mkdir(parents=True, exist_ok=True)
            restore_path.write_text("{}", encoding="utf-8")
            # The default path is used as-is; only an explicit --input is resolved.
            expected_path = restore_path
            result = runner.invoke(cli, ["memory", "restore"])

        assert result.exit_code == 0
        assert "Restored 3 memories" in result.output
        mock_backup_manager.restore_sync.assert_called_once_with()
        config = mock_backup_manager_cls.call_args.kwargs["config"]
        assert config.backup_path == expected_path

    @patch("gobby.sync.memories.MemoryBackupManager")
    @patch("gobby.cli.memory.get_memory_manager")
    def test_restore_custom_input_quiet(
        self,
        mock_get_manager: MagicMock,
        mock_backup_manager_cls: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Custom restore path is passed through and quiet suppresses output."""
        mock_manager = MagicMock()
        mock_manager.db = MagicMock()
        mock_get_manager.return_value = mock_manager
        mock_backup_manager = MagicMock()
        mock_backup_manager.restore_sync.return_value = 0
        mock_backup_manager_cls.return_value = mock_backup_manager
        restore_path = Path("/tmp/memories.jsonl")

        with runner.isolated_filesystem():
            restore_path = Path("memories.jsonl")
            restore_path.write_text("{}", encoding="utf-8")
            expected_path = restore_path.resolve()
            result = runner.invoke(
                cli,
                ["memory", "restore", "--input", str(restore_path), "--quiet"],
            )

        assert result.exit_code == 0
        assert result.output == ""
        mock_backup_manager.restore_sync.assert_called_once_with()
        config = mock_backup_manager_cls.call_args.kwargs["config"]
        assert config.backup_path == expected_path

    def test_restore_missing_explicit_input_fails(self, runner: CliRunner) -> None:
        """Explicit --input paths should fail when missing."""
        with runner.isolated_filesystem():
            result = runner.invoke(cli, ["memory", "restore", "--input", "missing.jsonl"])

        assert result.exit_code != 0
        assert "Memory backup not found:" in result.output
        assert "missing.jsonl" in result.output

    @patch("gobby.sync.memories.MemoryBackupManager")
    @patch("gobby.cli.memory.get_memory_manager")
    def test_restore_import_error_fails(
        self,
        mock_get_manager: MagicMock,
        mock_backup_manager_cls: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Restore exits nonzero when backup import fails."""
        mock_manager = MagicMock()
        mock_manager.db = MagicMock()
        mock_get_manager.return_value = mock_manager
        mock_backup_manager = MagicMock()
        mock_backup_manager.restore_sync.side_effect = MemoryRestoreError("corrupt JSONL")
        mock_backup_manager_cls.return_value = mock_backup_manager

        with runner.isolated_filesystem():
            restore_path = Path("memories.jsonl")
            restore_path.write_text("{", encoding="utf-8")
            result = runner.invoke(cli, ["memory", "restore", "--input", str(restore_path)])

        assert result.exit_code == 1
        assert "corrupt JSONL" in result.output


class TestResolveMemoryId:
    """resolve_memory_id delegates to the facade resolver and owns the CLI messages."""

    def test_resolve_memory_id_uses_facade_resolver(self) -> None:
        from gobby.cli.memory import resolve_memory_id

        mock_manager = MagicMock()
        mock_manager.resolve_memory_id.return_value = "12345678-1234-1234-1234-123456789012"

        result = resolve_memory_id(mock_manager, "12345678", project_id="project-a")

        assert result == "12345678-1234-1234-1234-123456789012"
        mock_manager.resolve_memory_id.assert_called_once_with("12345678", project_id="project-a")
        mock_manager.get_memory.assert_not_called()
        mock_manager.find_by_prefix.assert_not_called()

    def test_resolve_not_found(self) -> None:
        """A miss keeps the ClickException message."""
        import click

        from gobby.cli.memory import resolve_memory_id

        mock_manager = MagicMock()
        mock_manager.resolve_memory_id.return_value = None

        with pytest.raises(click.ClickException) as exc_info:
            resolve_memory_id(mock_manager, "nonexistent")
        assert str(exc_info.value) == "Memory not found: nonexistent"

    def test_resolve_ambiguous(self, capsys: pytest.CaptureFixture[str]) -> None:
        """An ambiguous prefix echoes the candidates and keeps the ClickException message."""
        import click

        from gobby.cli.memory import resolve_memory_id
        from gobby.memory.facade import AmbiguousMemoryReferenceError

        mock_manager = MagicMock()
        mock_manager.resolve_memory_id.side_effect = AmbiguousMemoryReferenceError(
            "mem-123", ["mem-123a", "mem-123b"]
        )

        with pytest.raises(click.ClickException) as exc_info:
            resolve_memory_id(mock_manager, "mem-123")

        assert str(exc_info.value) == "Ambiguous memory reference: mem-123"
        captured = capsys.readouterr()
        assert "mem-123a" in captured.err
        assert "mem-123b" in captured.err


class TestMemoryDeleteNotFound:
    """Additional delete tests."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @patch("gobby.cli.memory.resolve_memory_id")
    @patch("gobby.cli.memory.get_memory_manager")
    def test_delete_not_found(
        self,
        mock_get_manager: MagicMock,
        mock_resolve: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test deleting a non-existent memory."""
        mock_manager = MagicMock()
        mock_manager.delete_memory = AsyncMock(return_value=False)
        mock_get_manager.return_value = mock_manager
        mock_resolve.return_value = "nonexistent"

        result = runner.invoke(cli, ["memory", "delete", "nonexistent"])

        assert result.exit_code == 1
        assert "Memory not found" in result.output


class TestMemoryShowNotFound:
    """Additional show tests."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @patch("gobby.cli.memory.resolve_memory_id")
    @patch("gobby.cli.memory.get_memory_manager")
    def test_show_not_found(
        self,
        mock_get_manager: MagicMock,
        mock_resolve: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test showing a non-existent memory."""
        mock_manager = MagicMock()
        mock_manager.get_memory.return_value = None
        mock_get_manager.return_value = mock_manager
        mock_resolve.return_value = "nonexistent"

        result = runner.invoke(cli, ["memory", "show", "nonexistent"])

        assert result.exit_code == 1
        assert "Memory not found" in result.output

    @patch("gobby.cli.memory.resolve_memory_id")
    @patch("gobby.cli.memory.get_memory_manager")
    def test_show_with_tags(
        self,
        mock_get_manager: MagicMock,
        mock_resolve: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test showing a memory with tags."""
        mock_manager = MagicMock()
        mock_item = MagicMock()
        mock_item.id = "mem-123"
        mock_item.content = "Content"
        mock_item.memory_type = "fact"
        mock_item.created_at = "2024-01-01"
        mock_item.updated_at = "2024-01-01"
        mock_item.source_type = "user"
        mock_item.access_count = 5
        mock_item.tags = ["tag1", "tag2"]

        mock_manager.get_memory.return_value = mock_item
        mock_get_manager.return_value = mock_manager
        mock_resolve.return_value = "mem-123"

        result = runner.invoke(cli, ["memory", "show", "mem-123"])

        assert result.exit_code == 0
        assert "Tags: tag1, tag2" in result.output
        assert "Access Count: 5" in result.output


class TestMemoryReindexCommand:
    """Tests for gobby memory reindex-embeddings command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    @patch("gobby.cli.memory._get_daemon_client")
    def test_reindex_success(
        self,
        mock_get_client: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test reindex command with successful result."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "success": True,
            "total_memories": 42,
            "embeddings_generated": 42,
        }
        mock_client.call_http_api.return_value = mock_response
        mock_get_client.return_value = mock_client

        result = runner.invoke(cli, ["memory", "reindex-embeddings"])

        assert result.exit_code == 0
        assert "42" in result.output
        mock_client.call_http_api.assert_called_once()

    @patch("gobby.cli.memory._get_daemon_client")
    def test_reindex_unavailable(
        self,
        mock_get_client: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test reindex command when embeddings are unavailable."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "success": False,
            "error": "Embedding unavailable — no API key configured",
        }
        mock_client.call_http_api.return_value = mock_response
        mock_get_client.return_value = mock_client

        result = runner.invoke(cli, ["memory", "reindex-embeddings"])

        assert result.exit_code == 0
        assert "unavailable" in result.output.lower() or "Error" in result.output

    @patch("gobby.cli.memory._get_daemon_client")
    def test_reindex_empty(
        self,
        mock_get_client: MagicMock,
        runner: CliRunner,
    ) -> None:
        """Test reindex command with no memories."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "success": True,
            "total_memories": 0,
            "embeddings_generated": 0,
        }
        mock_client.call_http_api.return_value = mock_response
        mock_get_client.return_value = mock_client

        result = runner.invoke(cli, ["memory", "reindex-embeddings"])

        assert result.exit_code == 0
        assert "0" in result.output

    def test_reindex_help(self, runner: CliRunner) -> None:
        """Test reindex-embeddings --help."""
        result = runner.invoke(cli, ["memory", "reindex-embeddings", "--help"])
        assert result.exit_code == 0
        assert "embedding" in result.output.lower()
