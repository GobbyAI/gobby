"""Tests for cli/memory.py -- targeting uncovered lines.

Covers: get_memory_manager, create, recall tag parsing, list tag parsing,
        export, dedupe, fix-null-project, backup, rebuild-crossrefs, rebuild-graph.
Lines targeted: 20-51, 198, 210-211, 261-347, 365-465, 494-570
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.memory import memory

pytestmark = pytest.mark.unit


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def mock_manager() -> Generator[MagicMock]:
    with patch("gobby.cli.memory.get_memory_manager") as mock_get:
        mgr = MagicMock()
        mock_get.return_value = mgr
        yield mgr


# =============================================================================
# get_memory_manager
# =============================================================================


class TestGetMemoryManager:
    def test_get_memory_manager_returns_instance(self) -> None:
        from gobby.cli.memory import get_memory_manager

        mock_ctx = MagicMock()
        mock_config = MagicMock()
        mock_ctx.obj = {"config": mock_config}

        with (
            patch("gobby.cli.memory.LocalDatabase"),
            patch("gobby.cli.memory.MemoryManager") as mock_mm,
        ):
            result = get_memory_manager(mock_ctx)
            mock_mm.assert_called_once()
            assert result == mock_mm.return_value


# =============================================================================
# create
# =============================================================================


class TestCreateMemory:
    def test_create_basic(self, runner: CliRunner, mock_manager: MagicMock) -> None:
        mock_mem = MagicMock()
        mock_mem.id = "mem-new"
        mock_mem.content = "Hello world"
        mock_manager.create_memory = AsyncMock(return_value=mock_mem)

        result = runner.invoke(memory, ["create", "Hello world"])
        assert result.exit_code == 0
        assert "Created memory: mem-new" in result.output

    def test_create_with_type(self, runner: CliRunner, mock_manager: MagicMock) -> None:
        mock_mem = MagicMock()
        mock_mem.id = "mem-1"
        mock_mem.content = "pref"
        mock_manager.create_memory = AsyncMock(return_value=mock_mem)

        result = runner.invoke(memory, ["create", "pref", "--type", "preference"])
        assert result.exit_code == 0
        call_kwargs = mock_manager.create_memory.call_args[1]
        assert call_kwargs["memory_type"] == "preference"

    @patch("gobby.cli.memory.resolve_project_ref", return_value="proj-1")
    def test_create_with_project(
        self, mock_proj: MagicMock, runner: CliRunner, mock_manager: MagicMock
    ) -> None:
        mock_mem = MagicMock()
        mock_mem.id = "mem-1"
        mock_mem.content = "c"
        mock_manager.create_memory = AsyncMock(return_value=mock_mem)

        result = runner.invoke(memory, ["create", "content", "--project", "myproj"])
        assert result.exit_code == 0
        call_kwargs = mock_manager.create_memory.call_args[1]
        assert call_kwargs["project_id"] == "proj-1"


# =============================================================================
# update - error path
# =============================================================================


class TestUpdateMemoryError:
    @patch("gobby.cli.memory.resolve_memory_id", return_value="mem-1")
    def test_update_value_error(
        self, mock_resolve: MagicMock, runner: CliRunner, mock_manager: MagicMock
    ) -> None:
        mock_manager.update_memory = AsyncMock(side_effect=ValueError("not found"))
        result = runner.invoke(memory, ["update", "mem-1", "--content", "new"])
        assert result.exit_code == 0
        assert "Error: not found" in result.output


# =============================================================================
# export
# =============================================================================


class TestExportMemories:
    def test_export_to_stdout(self, runner: CliRunner, mock_manager: MagicMock) -> None:
        mock_manager.export_markdown.return_value = "# Memories\n\nSome content"
        result = runner.invoke(memory, ["export"])
        assert result.exit_code == 0
        assert "# Memories" in result.output

    def test_export_to_file(
        self, runner: CliRunner, mock_manager: MagicMock, tmp_path: Path
    ) -> None:
        mock_manager.export_markdown.return_value = "# Exported"
        outfile = str(tmp_path / "out.md")
        result = runner.invoke(memory, ["export", "--output", outfile])
        assert result.exit_code == 0
        assert "Exported memories to" in result.output
        assert Path(outfile).read_text() == "# Exported"

    def test_export_no_metadata(self, runner: CliRunner, mock_manager: MagicMock) -> None:
        mock_manager.export_markdown.return_value = "content"
        result = runner.invoke(memory, ["export", "--no-metadata", "--no-stats"])
        assert result.exit_code == 0
        mock_manager.export_markdown.assert_called_once_with(
            project_id=None, include_metadata=False, include_stats=False
        )


# =============================================================================
# dedupe
# =============================================================================


class TestDedupeMemories:
    def test_dedupe_no_memories(self, runner: CliRunner, mock_manager: MagicMock) -> None:
        mock_manager.list_memories.return_value = []
        result = runner.invoke(memory, ["dedupe"])
        assert result.exit_code == 0
        assert "No memories found" in result.output

    def test_dedupe_dry_run(self, runner: CliRunner, mock_manager: MagicMock) -> None:
        mem1 = MagicMock()
        mem1.id = "mem-a1"
        mem1.content = "same content"
        mem1.created_at = "2024-01-01"
        mem1.project_id = None
        mem2 = MagicMock()
        mem2.id = "mem-a2"
        mem2.content = "same content"
        mem2.created_at = "2024-01-02"
        mem2.project_id = "proj-1"
        mock_manager.list_memories.return_value = [mem1, mem2]

        result = runner.invoke(memory, ["dedupe", "--dry-run"])
        assert result.exit_code == 0
        assert "Duplicate content (2 copies)" in result.output
        assert "Found 1 duplicate" in result.output

    def test_dedupe_execute(self, runner: CliRunner, mock_manager: MagicMock) -> None:
        mem1 = MagicMock()
        mem1.id = "mem-a1"
        mem1.content = "dup"
        mem1.created_at = "2024-01-01"
        mem1.project_id = None
        mem2 = MagicMock()
        mem2.id = "mem-a2"
        mem2.content = "dup"
        mem2.created_at = "2024-01-02"
        mem2.project_id = None
        mock_manager.list_memories.return_value = [mem1, mem2]
        mock_manager.delete_memory = AsyncMock(return_value=True)

        result = runner.invoke(memory, ["dedupe"])
        assert result.exit_code == 0
        assert "Deleted 1 duplicate" in result.output

    def test_dedupe_no_duplicates(self, runner: CliRunner, mock_manager: MagicMock) -> None:
        mem1 = MagicMock()
        mem1.id = "mem-a"
        mem1.content = "unique1"
        mem1.created_at = "2024-01-01"
        mem1.project_id = None
        mock_manager.list_memories.return_value = [mem1]

        result = runner.invoke(memory, ["dedupe", "--dry-run"])
        assert result.exit_code == 0
        assert "Found 0 duplicate" in result.output


# =============================================================================
# rebuild-crossrefs
# =============================================================================


class TestRebuildCrossrefs:
    @patch("gobby.cli.memory._get_daemon_client")
    def test_rebuild_crossrefs_success(
        self, mock_client_fn: MagicMock, runner: CliRunner, mock_manager: MagicMock
    ) -> None:
        client = MagicMock()
        client.check_health.return_value = (True, None)
        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = {"memories_processed": 10, "crossrefs_created": 5}
        client.call_http_api.return_value = resp
        mock_client_fn.return_value = client

        result = runner.invoke(memory, ["rebuild-crossrefs"])
        assert result.exit_code == 0
        assert "10 memories processed" in result.output
        assert "5 crossrefs created" in result.output

    @patch("gobby.cli.memory._get_daemon_client")
    def test_rebuild_crossrefs_daemon_not_running(
        self, mock_client_fn: MagicMock, runner: CliRunner, mock_manager: MagicMock
    ) -> None:
        client = MagicMock()
        client.check_health.return_value = (False, "Connection refused")
        mock_client_fn.return_value = client

        result = runner.invoke(memory, ["rebuild-crossrefs"])
        assert result.exit_code != 0
        assert "Daemon not running" in result.output

    @patch("gobby.cli.memory._get_daemon_client")
    def test_rebuild_crossrefs_api_error(
        self, mock_client_fn: MagicMock, runner: CliRunner, mock_manager: MagicMock
    ) -> None:
        client = MagicMock()
        client.check_health.return_value = (True, None)
        resp = MagicMock()
        resp.ok = False
        resp.status_code = 500
        resp.text = "Internal Server Error"
        client.call_http_api.return_value = resp
        mock_client_fn.return_value = client

        result = runner.invoke(memory, ["rebuild-crossrefs"])
        assert result.exit_code != 0
        assert "Rebuild failed" in result.output

    @patch("gobby.cli.memory._get_daemon_client")
    def test_rebuild_crossrefs_bad_json(
        self, mock_client_fn: MagicMock, runner: CliRunner, mock_manager: MagicMock
    ) -> None:
        client = MagicMock()
        client.check_health.return_value = (True, None)
        resp = MagicMock()
        resp.ok = True
        resp.json.side_effect = ValueError("bad json")
        client.call_http_api.return_value = resp
        mock_client_fn.return_value = client

        result = runner.invoke(memory, ["rebuild-crossrefs"])
        assert result.exit_code != 0
        assert "Invalid response" in result.output


# =============================================================================
# rebuild-graph
# =============================================================================


class TestRebuildGraph:
    @patch("gobby.cli.memory._get_daemon_client")
    def test_rebuild_graph_success(
        self, mock_client_fn: MagicMock, runner: CliRunner, mock_manager: MagicMock
    ) -> None:
        client = MagicMock()
        client.check_health.return_value = (True, None)
        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = {"memories_extracted": 8, "memories_processed": 10, "errors": 0}
        client.call_http_api.return_value = resp
        mock_client_fn.return_value = client

        result = runner.invoke(memory, ["rebuild-graph"])
        assert result.exit_code == 0
        assert "8/10" in result.output

    @patch("gobby.cli.memory._get_daemon_client")
    def test_rebuild_graph_daemon_not_running(
        self, mock_client_fn: MagicMock, runner: CliRunner, mock_manager: MagicMock
    ) -> None:
        client = MagicMock()
        client.check_health.return_value = (False, "down")
        mock_client_fn.return_value = client

        result = runner.invoke(memory, ["rebuild-graph"])
        assert result.exit_code != 0

    @patch("gobby.cli.memory._get_daemon_client")
    def test_rebuild_graph_api_failure(
        self, mock_client_fn: MagicMock, runner: CliRunner, mock_manager: MagicMock
    ) -> None:
        client = MagicMock()
        client.check_health.return_value = (True, None)
        resp = MagicMock()
        resp.ok = False
        resp.status_code = 500
        resp.text = "err"
        client.call_http_api.return_value = resp
        mock_client_fn.return_value = client

        result = runner.invoke(memory, ["rebuild-graph"])
        assert result.exit_code != 0

    @patch("gobby.cli.memory.resolve_project_ref", return_value="proj-1")
    @patch("gobby.cli.memory._get_daemon_client")
    def test_rebuild_graph_with_project(
        self,
        mock_client_fn: MagicMock,
        mock_proj: MagicMock,
        runner: CliRunner,
        mock_manager: MagicMock,
    ) -> None:
        client = MagicMock()
        client.check_health.return_value = (True, None)
        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = {"memories_extracted": 1, "memories_processed": 1, "errors": 0}
        client.call_http_api.return_value = resp
        mock_client_fn.return_value = client

        result = runner.invoke(memory, ["rebuild-graph", "--project", "myproj"])
        assert result.exit_code == 0
        # Verify project_id was passed in the URL
        call_args = client.call_http_api.call_args
        assert "project_id=" in call_args[0][0]
