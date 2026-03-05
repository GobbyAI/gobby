"""Tests for the TestRunner (subprocess + LLM summarization)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.test_run_models import TestRun
from gobby.storage.test_runs import TestRunStorage
from gobby.testing.runner import TestRunner

pytestmark = pytest.mark.unit


@pytest.fixture
def temp_db(tmp_path: Path) -> Iterator[LocalDatabase]:
    """Create a temporary database with schema."""
    db_path = tmp_path / "test.db"
    db = LocalDatabase(db_path)
    run_migrations(db)
    yield db
    db.close()


@pytest.fixture
def storage(temp_db: LocalDatabase) -> TestRunStorage:
    return TestRunStorage(temp_db)


@pytest.fixture
def runner(storage: TestRunStorage, tmp_path: Path) -> TestRunner:
    return TestRunner(storage=storage, output_dir=tmp_path / "output")


class TestRunnerSuccess:
    """Tests for successful command execution."""

    @pytest.mark.asyncio
    async def test_run_check_success(self, runner: TestRunner) -> None:
        """Test running a command that succeeds."""
        result = await runner.run_check(
            category="lint",
            command="echo 'All checks passed'",
        )

        assert result.status == "completed"
        assert result.exit_code == 0
        assert "All checks passed" in (result.summary or "")
        assert result.output_file is not None
        assert Path(result.output_file).exists()

    @pytest.mark.asyncio
    async def test_brief_success_extracts_tail(self, runner: TestRunner) -> None:
        """Test that success summary takes the last few lines."""
        result = await runner.run_check(
            category="unit_tests",
            command="printf 'line1\nline2\nline3\nline4\nline5\n42 passed in 3.2s'",
        )

        assert result.status == "completed"
        assert "42 passed in 3.2s" in (result.summary or "")

    @pytest.mark.asyncio
    async def test_paths_appended(self, runner: TestRunner) -> None:
        """Test that paths are appended to command."""
        result = await runner.run_check(
            category="lint",
            command="echo",
            paths="src/foo.py",
        )

        assert result.status == "completed"
        assert result.command == "echo src/foo.py"

    @pytest.mark.asyncio
    async def test_extra_args_appended(self, runner: TestRunner) -> None:
        """Test that extra_args are appended."""
        result = await runner.run_check(
            category="lint",
            command="echo hello",
            extra_args="--verbose",
        )

        assert result.status == "completed"
        assert result.command == "echo hello --verbose"


class TestRunnerFailure:
    """Tests for failed command execution."""

    @pytest.mark.asyncio
    async def test_run_check_failure_without_llm(self, runner: TestRunner) -> None:
        """Test running a command that fails (no LLM, fallback to raw tail)."""
        result = await runner.run_check(
            category="unit_tests",
            command="echo 'FAILED test_foo.py::test_bar' && exit 1",
        )

        assert result.status == "failed"
        assert result.exit_code == 1
        assert "FAILED" in (result.summary or "")

    @pytest.mark.asyncio
    async def test_run_check_failure_with_llm(
        self, storage: TestRunStorage, tmp_path: Path
    ) -> None:
        """Test that LLM summarization is called on failure."""
        mock_provider = MagicMock()
        mock_provider.generate_text = AsyncMock(return_value="test_foo.py:42 - AssertionError")

        mock_llm = MagicMock()
        mock_llm.get_provider.return_value = mock_provider

        from gobby.config.features import TestSummarizerConfig

        config = TestSummarizerConfig(enabled=True, provider="claude", model="haiku")

        runner = TestRunner(
            storage=storage,
            llm_service=mock_llm,
            config=config,
            output_dir=tmp_path / "output",
        )

        result = await runner.run_check(
            category="unit_tests",
            command="echo 'error output' && exit 1",
        )

        assert result.status == "failed"
        assert "AssertionError" in (result.summary or "")
        mock_provider.generate_text.assert_called_once()

    @pytest.mark.asyncio
    async def test_llm_failure_falls_back_to_raw(
        self, storage: TestRunStorage, tmp_path: Path
    ) -> None:
        """Test that LLM failure falls back to raw output tail."""
        mock_provider = MagicMock()
        mock_provider.generate_text = AsyncMock(side_effect=RuntimeError("LLM down"))

        mock_llm = MagicMock()
        mock_llm.get_provider.return_value = mock_provider

        from gobby.config.features import TestSummarizerConfig

        config = TestSummarizerConfig(enabled=True)

        runner = TestRunner(
            storage=storage,
            llm_service=mock_llm,
            config=config,
            output_dir=tmp_path / "output",
        )

        result = await runner.run_check(
            category="lint",
            command="echo 'raw error output' && exit 1",
        )

        assert result.status == "failed"
        assert "raw error output" in (result.summary or "")


class TestRunnerTimeout:
    """Tests for command timeout handling."""

    @pytest.mark.asyncio
    async def test_timeout_kills_process(self, runner: TestRunner) -> None:
        """Test that a timed-out command is killed and marked."""
        result = await runner.run_check(
            category="unit_tests",
            command="sleep 60",
            timeout=1,
        )

        assert result.status == "timeout"
        assert result.exit_code == -1
        assert "timed out" in (result.summary or "").lower()


class TestRunnerOutput:
    """Tests for raw output pagination."""

    @pytest.mark.asyncio
    async def test_get_output_pagination(self, runner: TestRunner) -> None:
        """Test paginated output retrieval."""
        # Generate output with known lines
        lines = "\\n".join(f"line{i}" for i in range(100))
        result = await runner.run_check(
            category="lint",
            command=f"printf '{lines}'",
        )

        output = runner.get_output(result, offset=0, limit=10)
        assert len(output["lines"]) == 10
        assert output["total_lines"] == 100
        assert output["has_more"] is True

        output2 = runner.get_output(result, offset=90, limit=20)
        assert len(output2["lines"]) == 10
        assert output2["has_more"] is False

    def test_get_output_missing_file(self, runner: TestRunner) -> None:
        """Test output retrieval when file doesn't exist."""
        run = TestRun(
            id="tr-missing",
            category="lint",
            command="echo",
            status="completed",
            started_at="2024-01-01",
            created_at="2024-01-01",
            output_file="/nonexistent/path.log",
        )

        output = runner.get_output(run)
        assert output["lines"] == []
        assert output["total_lines"] == 0
