"""Tests for the memory CLI module."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

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

        result = runner.invoke(cli, ["memory", "create", "remember this"])

        assert result.exit_code == 0
        assert "Created memory: mem-1 - remember this" in result.output
        mock_resolve_project.assert_called_once_with(None)
        mock_manager.create_memory.assert_awaited_once_with(
            content="remember this",
            memory_type="fact",
            project_id="proj-current",
            source_type="user",
        )

    @patch("gobby.cli.memory.get_memory_manager")
    def test_create_rejects_noncanonical_memory_type(
        self,
        mock_get_manager: MagicMock,
        runner: CliRunner,
    ) -> None:
        result = runner.invoke(
            cli,
            ["memory", "create", "Bad type", "--type", "debugging_pattern"],
        )

        assert result.exit_code == 2
        assert "Invalid value for '--type'" in result.output
        mock_get_manager.assert_not_called()


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


class TestMemoryDreamCommand:
    """Tests for gobby memory dream command."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_dream_prints_aggregate_summary(
        self,
        runner: CliRunner,
    ) -> None:
        """An unscoped dream sweeps every due project and prints a per-project summary."""
        mock_client = MagicMock()
        mock_client.call_http_api.return_value = _FakeHTTPResponse(
            {
                "success": True,
                "targets": 2,
                "completed": 2,
                "failed": 0,
                "mutations": 3,
                "runs": [
                    {"project_id": "proj-1", "success": True, "run_id": "r1", "mutations": 1},
                    {"project_id": None, "success": True, "run_id": "r2", "mutations": 2},
                ],
            }
        )

        with patch("gobby.cli.memory.dream._get_daemon_client", return_value=mock_client):
            result = runner.invoke(
                memory_cli,
                ["dream", "--dry-run"],
                obj={"config": MagicMock(daemon_port=60887)},
            )

        assert result.exit_code == 0
        assert "Swept 2/2 project(s): 3 mutation(s) total" in result.output
        assert "proj-1: 1 mutation(s) (run r1)" in result.output
        assert "global: 2 mutation(s) (run r2)" in result.output
        mock_client.call_http_api.assert_called_once()
        call = mock_client.call_http_api.call_args
        assert call.args == ("/memory/dream",)
        assert call.kwargs["method"] == "POST"
        # The sweep is synchronous; the POST waits for the full --timeout (default 900s).
        assert call.kwargs["timeout"] == 900.0
        assert call.kwargs["json_data"]["dry_run"] is True
        assert "wait" not in call.kwargs["json_data"]
        assert "project_id" not in call.kwargs["json_data"]

    def test_dream_passes_flags_and_renders_failed_target(
        self,
        runner: CliRunner,
    ) -> None:
        mock_client = MagicMock()
        mock_client.call_http_api.return_value = _FakeHTTPResponse(
            {
                "success": True,
                "targets": 2,
                "completed": 1,
                "failed": 1,
                "mutations": 2,
                "runs": [
                    {"project_id": "proj-ok", "success": True, "run_id": "r1", "mutations": 2},
                    {"project_id": "proj-bad", "success": False, "error": "boom"},
                ],
            }
        )

        with patch("gobby.cli.memory.dream._get_daemon_client", return_value=mock_client):
            result = runner.invoke(
                memory_cli,
                ["dream", "--full", "--memory-type", "fact", "--timeout", "30"],
                obj={"config": MagicMock(daemon_port=60887)},
            )

        assert result.exit_code == 0
        assert "Swept 1/2 project(s): 2 mutation(s) total, 1 failed" in result.output
        assert "proj-bad: failed — boom" in result.output
        call = mock_client.call_http_api.call_args
        assert call.kwargs["timeout"] == 30.0
        assert call.kwargs["json_data"]["full_sweep"] is True
        assert call.kwargs["json_data"]["memory_type"] == "fact"

    def test_dream_raises_on_unsuccessful_response(
        self,
        runner: CliRunner,
    ) -> None:
        mock_client = MagicMock()
        mock_client.call_http_api.return_value = _FakeHTTPResponse(
            {"success": False, "error": "memory dream is disabled"}
        )

        with patch("gobby.cli.memory.dream._get_daemon_client", return_value=mock_client):
            result = runner.invoke(
                memory_cli,
                ["dream"],
                obj={"config": MagicMock(daemon_port=60887)},
            )

        assert result.exit_code != 0
        assert "memory dream is disabled" in result.output


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
            restore_path = Path(".gobby/memories.jsonl")
            restore_path.parent.mkdir()
            restore_path.write_text("{}", encoding="utf-8")
            expected_path = restore_path.resolve()
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
    """Tests for resolve_memory_id function."""

    @patch("gobby.cli.memory.get_memory_manager")
    def test_resolve_exact_match(self, mock_get_manager: MagicMock) -> None:
        """Test resolving exact UUID match."""
        from gobby.cli.memory import resolve_memory_id

        mock_manager = MagicMock()
        mock_mem = MagicMock()
        mock_mem.id = "12345678-1234-1234-1234-123456789012"
        mock_manager.get_memory.return_value = mock_mem
        mock_get_manager.return_value = mock_manager

        result = resolve_memory_id(mock_manager, "12345678-1234-1234-1234-123456789012")
        assert result == "12345678-1234-1234-1234-123456789012"

    @patch("gobby.cli.memory.get_memory_manager")
    def test_resolve_prefix_match(self, mock_get_manager: MagicMock) -> None:
        """Test resolving prefix match."""
        from gobby.cli.memory import resolve_memory_id

        mock_manager = MagicMock()
        mock_manager.get_memory.return_value = None  # Not exact match
        mock_mem = MagicMock()
        mock_mem.id = "mem-123456789"
        mock_manager.find_by_prefix.return_value = [mock_mem]
        mock_get_manager.return_value = mock_manager

        result = resolve_memory_id(mock_manager, "mem-12")
        assert result == "mem-123456789"

    def test_resolve_not_found(self) -> None:
        """Test resolving non-existent memory."""
        import click

        from gobby.cli.memory import resolve_memory_id

        mock_manager = MagicMock()
        mock_manager.get_memory.return_value = None
        mock_manager.find_by_prefix.return_value = []

        with pytest.raises(click.ClickException) as exc_info:
            resolve_memory_id(mock_manager, "nonexistent")
        assert "Memory not found" in str(exc_info.value)

    def test_resolve_ambiguous(self) -> None:
        """Test resolving ambiguous prefix."""
        import click

        from gobby.cli.memory import resolve_memory_id

        mock_manager = MagicMock()
        mock_manager.get_memory.return_value = None
        mock_mem1 = MagicMock()
        mock_mem1.id = "mem-123a"
        mock_mem2 = MagicMock()
        mock_mem2.id = "mem-123b"
        mock_manager.find_by_prefix.return_value = [mock_mem1, mock_mem2]

        with pytest.raises(click.ClickException) as exc_info:
            resolve_memory_id(mock_manager, "mem-123")
        assert "Ambiguous memory reference" in str(exc_info.value)


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
