"""Tests for CLI pipeline commands.

TDD tests for the pipelines CLI group.
"""

from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli import cli
from gobby.workflows.definitions import PipelineDefinition, PipelineStep
from gobby.workflows.loader_cache import DiscoveredWorkflow

pytestmark = pytest.mark.unit


def pipelines_available() -> bool:
    """Check if pipelines command is registered in CLI."""
    try:
        return "pipelines" in cli.commands
    except Exception:
        return False


@pytest.fixture
def runner() -> CliRunner:
    """Create a CLI test runner."""
    return CliRunner()


@pytest.fixture
def mock_pipeline():
    """Create a mock pipeline definition."""
    return PipelineDefinition(
        name="deploy",
        description="Deploy to production",
        steps=[
            PipelineStep(id="build", exec="npm run build"),
            PipelineStep(id="test", exec="npm test"),
            PipelineStep(id="deploy", exec="npm run deploy"),
        ],
    )


@pytest.fixture
def mock_discovered_pipelines(mock_pipeline):
    """Create mock discovered pipelines."""
    pipeline2 = PipelineDefinition(
        name="test",
        description="Run test suite",
        steps=[PipelineStep(id="test", exec="pytest")],
    )
    return [
        DiscoveredWorkflow(
            name="deploy",
            definition=mock_pipeline,
            priority=100,
            is_project=True,
            path=Path("/project/.gobby/workflows/deploy.yaml"),
        ),
        DiscoveredWorkflow(
            name="test",
            definition=pipeline2,
            priority=50,
            is_project=False,
            path=Path("/home/user/.gobby/workflows/test.yaml"),
        ),
    ]


class TestPipelinesCLIRegistration:
    """Tests for pipelines CLI command registration."""

    def test_pipelines_command_exists(self, runner) -> None:
        """Verify pipelines command is registered."""
        result = runner.invoke(cli, ["pipelines", "--help"])

        if result.exit_code == 2 and "No such command 'pipelines'" in result.output:
            pytest.skip("pipelines CLI command not yet registered")

        assert result.exit_code == 0
        assert "pipelines" in result.output.lower()

    def test_pipelines_subcommands_exist(self, runner) -> None:
        """Verify expected subcommands are registered."""
        result = runner.invoke(cli, ["pipelines", "--help"])

        if result.exit_code == 2 and "No such command 'pipelines'" in result.output:
            pytest.skip("pipelines CLI command not yet registered")

        assert result.exit_code == 0
        assert "list" in result.output
        assert "show" in result.output
        assert "runs" in result.output


class TestPipelinesList:
    """Tests for gobby pipelines list command."""

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_list_discovers_pipelines(self, runner, mock_discovered_pipelines) -> None:
        """Verify 'gobby pipelines list' calls discover_pipeline_workflows."""
        mock_loader = MagicMock()
        mock_loader.discover_pipeline_workflows_sync.return_value = mock_discovered_pipelines

        with patch("gobby.cli.pipelines.get_workflow_loader", return_value=mock_loader):
            result = runner.invoke(cli, ["pipelines", "list"])

            assert result.exit_code == 0
            mock_loader.discover_pipeline_workflows_sync.assert_called_once()

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_list_outputs_pipeline_names(self, runner, mock_discovered_pipelines) -> None:
        """Verify list command outputs pipeline names."""
        mock_loader = MagicMock()
        mock_loader.discover_pipeline_workflows_sync.return_value = mock_discovered_pipelines

        with patch("gobby.cli.pipelines.get_workflow_loader", return_value=mock_loader):
            result = runner.invoke(cli, ["pipelines", "list"])

            assert result.exit_code == 0
            assert "deploy" in result.output
            assert "test" in result.output

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_list_shows_descriptions(self, runner, mock_discovered_pipelines) -> None:
        """Verify list command shows pipeline descriptions."""
        mock_loader = MagicMock()
        mock_loader.discover_pipeline_workflows_sync.return_value = mock_discovered_pipelines

        with patch("gobby.cli.pipelines.get_workflow_loader", return_value=mock_loader):
            result = runner.invoke(cli, ["pipelines", "list"])

            assert result.exit_code == 0
            assert "Deploy to production" in result.output

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_list_shows_source(self, runner, mock_discovered_pipelines) -> None:
        """Verify list command indicates project vs global source."""
        mock_loader = MagicMock()
        mock_loader.discover_pipeline_workflows_sync.return_value = mock_discovered_pipelines

        with patch("gobby.cli.pipelines.get_workflow_loader", return_value=mock_loader):
            result = runner.invoke(cli, ["pipelines", "list"])

            assert result.exit_code == 0
            assert "project" in result.output.lower()

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_list_empty_result(self, runner) -> None:
        """Verify list handles no pipelines found."""
        mock_loader = MagicMock()
        mock_loader.discover_pipeline_workflows_sync.return_value = []

        with patch("gobby.cli.pipelines.get_workflow_loader", return_value=mock_loader):
            result = runner.invoke(cli, ["pipelines", "list"])

            assert result.exit_code == 0
            assert "no pipeline" in result.output.lower() or result.output.strip() == ""

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_list_json_format(self, runner, mock_discovered_pipelines) -> None:
        """Verify list command supports --json output."""
        import json

        mock_loader = MagicMock()
        mock_loader.discover_pipeline_workflows_sync.return_value = mock_discovered_pipelines

        with patch("gobby.cli.pipelines.get_workflow_loader", return_value=mock_loader):
            result = runner.invoke(cli, ["pipelines", "list", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert "pipelines" in data
            assert len(data["pipelines"]) == 2


class TestPipelinesShow:
    """Tests for gobby pipelines show command."""

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_show_loads_pipeline(self, runner, mock_pipeline) -> None:
        """Verify 'gobby pipelines show <name>' loads the pipeline."""
        mock_loader = MagicMock()
        mock_loader.load_pipeline_sync.return_value = mock_pipeline

        with patch("gobby.cli.pipelines.get_workflow_loader", return_value=mock_loader):
            result = runner.invoke(cli, ["pipelines", "show", "deploy"])

            assert result.exit_code == 0
            mock_loader.load_pipeline_sync.assert_called_once_with("deploy", project_path=ANY)

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_show_outputs_pipeline_details(self, runner, mock_pipeline) -> None:
        """Verify show command outputs pipeline definition details."""
        mock_loader = MagicMock()
        mock_loader.load_pipeline_sync.return_value = mock_pipeline

        with patch("gobby.cli.pipelines.get_workflow_loader", return_value=mock_loader):
            result = runner.invoke(cli, ["pipelines", "show", "deploy"])

            assert result.exit_code == 0
            assert "deploy" in result.output
            assert "Deploy to production" in result.output

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_show_outputs_steps(self, runner, mock_pipeline) -> None:
        """Verify show command outputs step information."""
        mock_loader = MagicMock()
        mock_loader.load_pipeline_sync.return_value = mock_pipeline

        with patch("gobby.cli.pipelines.get_workflow_loader", return_value=mock_loader):
            result = runner.invoke(cli, ["pipelines", "show", "deploy"])

            assert result.exit_code == 0
            assert "build" in result.output
            assert "test" in result.output

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_show_not_found(self, runner) -> None:
        """Verify show returns error for nonexistent pipeline."""
        mock_loader = MagicMock()
        mock_loader.load_pipeline_sync.return_value = None

        with patch("gobby.cli.pipelines.get_workflow_loader", return_value=mock_loader):
            result = runner.invoke(cli, ["pipelines", "show", "nonexistent"])

            assert result.exit_code != 0 or "not found" in result.output.lower()

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_show_json_format(self, runner, mock_pipeline) -> None:
        """Verify show command supports --json output."""
        import json

        mock_loader = MagicMock()
        mock_loader.load_pipeline_sync.return_value = mock_pipeline

        with patch("gobby.cli.pipelines.get_workflow_loader", return_value=mock_loader):
            result = runner.invoke(cli, ["pipelines", "show", "deploy", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["name"] == "deploy"
            assert "steps" in data


class TestPipelinesRun:
    """Tests for gobby pipelines run command."""

    @pytest.fixture
    def mock_execution(self):
        """Create a mock pipeline execution."""
        from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

        return PipelineExecution(
            id="pe-abc123",
            pipeline_name="deploy",
            project_id="proj-1",
            status=ExecutionStatus.COMPLETED,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:01:00Z",
            outputs_json='{"result": "success"}',
        )

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_run_subcommand_exists(self, runner) -> None:
        """Verify 'run' subcommand is registered."""
        result = runner.invoke(cli, ["pipelines", "--help"])
        assert "run" in result.output

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_run_loads_and_executes(self, runner, mock_pipeline, mock_execution) -> None:
        """Verify 'gobby pipelines run <name>' loads and executes pipeline."""
        from unittest.mock import AsyncMock

        mock_loader = MagicMock()
        mock_loader.load_pipeline_sync.return_value = mock_pipeline

        mock_executor = MagicMock()
        mock_executor.execute = AsyncMock(return_value=mock_execution)

        with (
            patch("gobby.cli.pipelines.get_workflow_loader", return_value=mock_loader),
            patch("gobby.cli.pipelines.get_pipeline_executor", return_value=mock_executor),
        ):
            result = runner.invoke(cli, ["pipelines", "run", "deploy"])

            assert result.exit_code == 0
            mock_loader.load_pipeline_sync.assert_called_once_with("deploy", project_path=ANY)
            mock_executor.execute.assert_called_once()

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_run_parses_inputs(self, runner, mock_pipeline, mock_execution) -> None:
        """Verify '-i key=value' parses inputs correctly."""
        from unittest.mock import AsyncMock

        mock_loader = MagicMock()
        mock_loader.load_pipeline_sync.return_value = mock_pipeline

        mock_executor = MagicMock()
        mock_executor.execute = AsyncMock(return_value=mock_execution)

        with (
            patch("gobby.cli.pipelines.get_workflow_loader", return_value=mock_loader),
            patch("gobby.cli.pipelines.get_pipeline_executor", return_value=mock_executor),
        ):
            result = runner.invoke(
                cli,
                ["pipelines", "run", "deploy", "-i", "env=prod", "-i", "version=1.0"],
            )

            assert result.exit_code == 0
            # Verify inputs were passed to execute
            call_kwargs = mock_executor.execute.call_args
            inputs = call_kwargs.kwargs.get("inputs", {})
            assert inputs.get("env") == "prod"
            assert inputs.get("version") == "1.0"

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_run_outputs_execution_id(self, runner, mock_pipeline, mock_execution) -> None:
        """Verify run command outputs execution_id and status."""
        from unittest.mock import AsyncMock

        mock_loader = MagicMock()
        mock_loader.load_pipeline_sync.return_value = mock_pipeline

        mock_executor = MagicMock()
        mock_executor.execute = AsyncMock(return_value=mock_execution)

        with (
            patch("gobby.cli.pipelines.get_workflow_loader", return_value=mock_loader),
            patch("gobby.cli.pipelines.get_pipeline_executor", return_value=mock_executor),
        ):
            result = runner.invoke(cli, ["pipelines", "run", "deploy"])

            assert result.exit_code == 0
            assert "pe-abc123" in result.output
            assert "completed" in result.output.lower()

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_run_handles_approval_required(self, runner, mock_pipeline) -> None:
        """Verify run command handles ApprovalRequired with token display."""
        from unittest.mock import AsyncMock

        from gobby.workflows.pipeline_state import ApprovalRequired

        mock_loader = MagicMock()
        mock_loader.load_pipeline_sync.return_value = mock_pipeline

        mock_executor = MagicMock()
        mock_executor.execute = AsyncMock(
            side_effect=ApprovalRequired(
                execution_id="pe-abc123",
                step_id="deploy-step",
                token="approval-token-xyz",
                message="Manual approval required",
            )
        )

        with (
            patch("gobby.cli.pipelines.get_workflow_loader", return_value=mock_loader),
            patch("gobby.cli.pipelines.get_pipeline_executor", return_value=mock_executor),
        ):
            result = runner.invoke(cli, ["pipelines", "run", "deploy"])

            assert result.exit_code == 0
            assert "approval" in result.output.lower()
            assert "approval-token-xyz" in result.output

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_run_pipeline_not_found(self, runner) -> None:
        """Verify run returns error for nonexistent pipeline."""
        mock_loader = MagicMock()
        mock_loader.load_pipeline_sync.return_value = None

        with patch("gobby.cli.pipelines.get_workflow_loader", return_value=mock_loader):
            result = runner.invoke(cli, ["pipelines", "run", "nonexistent"])

            assert result.exit_code != 0 or "not found" in result.output.lower()

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_run_json_format(self, runner, mock_pipeline, mock_execution) -> None:
        """Verify run command supports --json output."""
        import json
        from unittest.mock import AsyncMock

        mock_loader = MagicMock()
        mock_loader.load_pipeline_sync.return_value = mock_pipeline

        mock_executor = MagicMock()
        mock_executor.execute = AsyncMock(return_value=mock_execution)

        with (
            patch("gobby.cli.pipelines.get_workflow_loader", return_value=mock_loader),
            patch("gobby.cli.pipelines.get_pipeline_executor", return_value=mock_executor),
        ):
            result = runner.invoke(cli, ["pipelines", "run", "deploy", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["execution_id"] == "pe-abc123"
            assert data["status"] == "completed"


class TestPipelineRunsShow:
    """Tests for gobby pipelines runs show command."""

    @pytest.fixture
    def mock_execution(self):
        """Create a mock pipeline execution."""
        from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

        return PipelineExecution(
            id="pe-abc123",
            pipeline_name="deploy",
            project_id="proj-1",
            status=ExecutionStatus.RUNNING,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:01:00Z",
            inputs_json='{"env": "prod"}',
        )

    @pytest.fixture
    def mock_step_executions(self):
        """Create mock step executions."""
        from gobby.workflows.pipeline_state import StepExecution, StepStatus

        return [
            StepExecution(
                id=1,
                execution_id="pe-abc123",
                step_id="build",
                status=StepStatus.COMPLETED,
            ),
            StepExecution(
                id=2,
                execution_id="pe-abc123",
                step_id="test",
                status=StepStatus.RUNNING,
            ),
        ]

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_runs_subcommand_exists(self, runner) -> None:
        """Verify 'runs' subcommand is registered."""
        result = runner.invoke(cli, ["pipelines", "--help"])
        assert "runs" in result.output

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_old_status_subcommand_is_removed(self, runner) -> None:
        """Verify old 'status' subcommand is not registered."""
        result = runner.invoke(cli, ["pipelines", "status", "pe-abc123"])
        assert result.exit_code == 2
        assert "No such command" in result.output

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_status_fetches_execution(self, runner, mock_execution, mock_step_executions) -> None:
        """Verify 'gobby pipelines runs show <id>' fetches execution."""
        mock_manager = MagicMock()
        mock_manager.get_execution.return_value = mock_execution
        mock_manager.get_steps_for_execution.return_value = mock_step_executions

        with patch("gobby.cli.pipelines.get_execution_manager", return_value=mock_manager):
            result = runner.invoke(cli, ["pipelines", "runs", "show", "pe-abc123"])

            assert result.exit_code == 0
            mock_manager.get_execution.assert_called_once_with("pe-abc123")

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_status_displays_execution_details(
        self, runner, mock_execution, mock_step_executions
    ) -> None:
        """Verify status command displays execution details."""
        mock_manager = MagicMock()
        mock_manager.get_execution.return_value = mock_execution
        mock_manager.get_steps_for_execution.return_value = mock_step_executions

        with patch("gobby.cli.pipelines.get_execution_manager", return_value=mock_manager):
            result = runner.invoke(cli, ["pipelines", "runs", "show", "pe-abc123"])

            assert result.exit_code == 0
            assert "pe-abc123" in result.output
            assert "deploy" in result.output
            assert "running" in result.output.lower()

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_status_shows_step_statuses(self, runner, mock_execution, mock_step_executions) -> None:
        """Verify status command shows step statuses."""
        mock_manager = MagicMock()
        mock_manager.get_execution.return_value = mock_execution
        mock_manager.get_steps_for_execution.return_value = mock_step_executions

        with patch("gobby.cli.pipelines.get_execution_manager", return_value=mock_manager):
            result = runner.invoke(cli, ["pipelines", "runs", "show", "pe-abc123"])

            assert result.exit_code == 0
            assert "build" in result.output
            assert "test" in result.output
            assert "completed" in result.output.lower()

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_status_not_found(self, runner) -> None:
        """Verify status returns error for nonexistent execution."""
        mock_manager = MagicMock()
        mock_manager.get_execution.return_value = None

        with patch("gobby.cli.pipelines.get_execution_manager", return_value=mock_manager):
            result = runner.invoke(cli, ["pipelines", "runs", "show", "pe-nonexistent"])

            assert result.exit_code != 0 or "not found" in result.output.lower()

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_status_json_format(self, runner, mock_execution, mock_step_executions) -> None:
        """Verify status command supports --json output."""
        import json

        mock_manager = MagicMock()
        mock_manager.get_execution.return_value = mock_execution
        mock_manager.get_steps_for_execution.return_value = mock_step_executions

        with patch("gobby.cli.pipelines.get_execution_manager", return_value=mock_manager):
            result = runner.invoke(cli, ["pipelines", "runs", "show", "pe-abc123", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["execution"]["id"] == "pe-abc123"
            assert data["execution"]["status"] == "running"
            assert len(data["steps"]) == 2


class TestPipelinesApprove:
    """Tests for gobby pipelines approve command."""

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_approve_subcommand_exists(self, runner) -> None:
        """Verify 'approve' subcommand is registered."""
        result = runner.invoke(cli, ["pipelines", "--help"])
        assert "approve" in result.output

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_approve_calls_executor(self, runner) -> None:
        """Verify 'gobby pipelines approve <token>' calls executor.approve()."""
        from unittest.mock import AsyncMock

        from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

        mock_executor = MagicMock()
        mock_execution = PipelineExecution(
            id="pe-abc123",
            pipeline_name="deploy",
            project_id="proj-1",
            status=ExecutionStatus.COMPLETED,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:01:00Z",
        )
        mock_executor.approve = AsyncMock(return_value=mock_execution)

        with patch("gobby.cli.pipelines.get_pipeline_executor", return_value=mock_executor):
            result = runner.invoke(cli, ["pipelines", "approve", "approval-token-xyz"])

            assert result.exit_code == 0
            mock_executor.approve.assert_called_once_with("approval-token-xyz", approved_by=None)

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_approve_shows_result(self, runner) -> None:
        """Verify approve command shows execution result."""
        from unittest.mock import AsyncMock

        from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

        mock_executor = MagicMock()
        mock_execution = PipelineExecution(
            id="pe-abc123",
            pipeline_name="deploy",
            project_id="proj-1",
            status=ExecutionStatus.COMPLETED,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:01:00Z",
        )
        mock_executor.approve = AsyncMock(return_value=mock_execution)

        with patch("gobby.cli.pipelines.get_pipeline_executor", return_value=mock_executor):
            result = runner.invoke(cli, ["pipelines", "approve", "approval-token-xyz"])

            assert result.exit_code == 0
            assert "pe-abc123" in result.output
            assert "completed" in result.output.lower()

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_approve_invalid_token(self, runner) -> None:
        """Verify approve handles invalid token."""
        from unittest.mock import AsyncMock

        mock_executor = MagicMock()
        mock_executor.approve = AsyncMock(side_effect=ValueError("Invalid token"))

        with patch("gobby.cli.pipelines.get_pipeline_executor", return_value=mock_executor):
            result = runner.invoke(cli, ["pipelines", "approve", "invalid-token"])

            assert result.exit_code != 0 or "invalid" in result.output.lower()

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_approve_json_format(self, runner) -> None:
        """Verify approve command supports --json output."""
        import json
        from unittest.mock import AsyncMock

        from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

        mock_executor = MagicMock()
        mock_execution = PipelineExecution(
            id="pe-abc123",
            pipeline_name="deploy",
            project_id="proj-1",
            status=ExecutionStatus.COMPLETED,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:01:00Z",
        )
        mock_executor.approve = AsyncMock(return_value=mock_execution)

        with patch("gobby.cli.pipelines.get_pipeline_executor", return_value=mock_executor):
            result = runner.invoke(cli, ["pipelines", "approve", "approval-token-xyz", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["execution_id"] == "pe-abc123"
            assert data["status"] == "completed"


class TestPipelinesReject:
    """Tests for gobby pipelines reject command."""

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_reject_subcommand_exists(self, runner) -> None:
        """Verify 'reject' subcommand is registered."""
        result = runner.invoke(cli, ["pipelines", "--help"])
        assert "reject" in result.output

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_reject_calls_executor(self, runner) -> None:
        """Verify 'gobby pipelines reject <token>' calls executor.reject()."""
        from unittest.mock import AsyncMock

        from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

        mock_executor = MagicMock()
        mock_execution = PipelineExecution(
            id="pe-abc123",
            pipeline_name="deploy",
            project_id="proj-1",
            status=ExecutionStatus.FAILED,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:01:00Z",
        )
        mock_executor.reject = AsyncMock(return_value=mock_execution)

        with patch("gobby.cli.pipelines.get_pipeline_executor", return_value=mock_executor):
            result = runner.invoke(cli, ["pipelines", "reject", "approval-token-xyz"])

            assert result.exit_code == 0
            mock_executor.reject.assert_called_once_with("approval-token-xyz", rejected_by=None)

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_reject_shows_result(self, runner) -> None:
        """Verify reject command shows execution result."""
        from unittest.mock import AsyncMock

        from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

        mock_executor = MagicMock()
        mock_execution = PipelineExecution(
            id="pe-abc123",
            pipeline_name="deploy",
            project_id="proj-1",
            status=ExecutionStatus.FAILED,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:01:00Z",
        )
        mock_executor.reject = AsyncMock(return_value=mock_execution)

        with patch("gobby.cli.pipelines.get_pipeline_executor", return_value=mock_executor):
            result = runner.invoke(cli, ["pipelines", "reject", "approval-token-xyz"])

            assert result.exit_code == 0
            assert "pe-abc123" in result.output

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_reject_invalid_token(self, runner) -> None:
        """Verify reject handles invalid token."""
        from unittest.mock import AsyncMock

        mock_executor = MagicMock()
        mock_executor.reject = AsyncMock(side_effect=ValueError("Invalid token"))

        with patch("gobby.cli.pipelines.get_pipeline_executor", return_value=mock_executor):
            result = runner.invoke(cli, ["pipelines", "reject", "invalid-token"])

            assert result.exit_code != 0 or "invalid" in result.output.lower()

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_reject_json_format(self, runner) -> None:
        """Verify reject command supports --json output."""
        import json
        from unittest.mock import AsyncMock

        from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

        mock_executor = MagicMock()
        mock_execution = PipelineExecution(
            id="pe-abc123",
            pipeline_name="deploy",
            project_id="proj-1",
            status=ExecutionStatus.FAILED,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:01:00Z",
        )
        mock_executor.reject = AsyncMock(return_value=mock_execution)

        with patch("gobby.cli.pipelines.get_pipeline_executor", return_value=mock_executor):
            result = runner.invoke(cli, ["pipelines", "reject", "approval-token-xyz", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["execution_id"] == "pe-abc123"
            assert data["status"] == "failed"


class TestPipelinesHistory:
    """Tests for gobby pipelines history command."""

    @pytest.fixture
    def mock_executions(self):
        """Create mock pipeline executions."""
        from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

        return [
            PipelineExecution(
                id="pe-abc123",
                pipeline_name="deploy",
                project_id="proj-1",
                status=ExecutionStatus.COMPLETED,
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:01:00Z",
            ),
            PipelineExecution(
                id="pe-def456",
                pipeline_name="deploy",
                project_id="proj-1",
                status=ExecutionStatus.FAILED,
                created_at="2026-01-02T00:00:00Z",
                updated_at="2026-01-02T00:01:00Z",
            ),
            PipelineExecution(
                id="pe-ghi789",
                pipeline_name="deploy",
                project_id="proj-1",
                status=ExecutionStatus.RUNNING,
                created_at="2026-01-03T00:00:00Z",
                updated_at="2026-01-03T00:01:00Z",
            ),
        ]

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_history_subcommand_exists(self, runner) -> None:
        """Verify 'history' subcommand is registered."""
        result = runner.invoke(cli, ["pipelines", "--help"])
        assert "history" in result.output

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_history_lists_executions(self, runner, mock_executions) -> None:
        """Verify 'gobby pipelines history <name>' lists executions."""
        mock_manager = MagicMock()
        mock_manager.list_executions.return_value = mock_executions

        with patch("gobby.cli.pipelines.get_execution_manager", return_value=mock_manager):
            result = runner.invoke(cli, ["pipelines", "history", "deploy"])

            assert result.exit_code == 0
            mock_manager.list_executions.assert_called_once()
            call_kwargs = mock_manager.list_executions.call_args
            assert call_kwargs.kwargs.get("pipeline_name") == "deploy"

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_history_shows_id_status_created(self, runner, mock_executions) -> None:
        """Verify history shows id, status, and created_at."""
        mock_manager = MagicMock()
        mock_manager.list_executions.return_value = mock_executions

        with patch("gobby.cli.pipelines.get_execution_manager", return_value=mock_manager):
            result = runner.invoke(cli, ["pipelines", "history", "deploy"])

            assert result.exit_code == 0
            assert "pe-abc123" in result.output
            assert "pe-def456" in result.output
            assert "completed" in result.output.lower()
            assert "failed" in result.output.lower()

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_history_supports_limit(self, runner, mock_executions) -> None:
        """Verify history supports --limit flag."""
        mock_manager = MagicMock()
        mock_manager.list_executions.return_value = mock_executions[:2]

        with patch("gobby.cli.pipelines.get_execution_manager", return_value=mock_manager):
            result = runner.invoke(cli, ["pipelines", "history", "deploy", "--limit", "2"])

            assert result.exit_code == 0
            call_kwargs = mock_manager.list_executions.call_args
            assert call_kwargs.kwargs.get("limit") == 2

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_history_empty_result(self, runner) -> None:
        """Verify history handles no executions gracefully."""
        mock_manager = MagicMock()
        mock_manager.list_executions.return_value = []

        with patch("gobby.cli.pipelines.get_execution_manager", return_value=mock_manager):
            result = runner.invoke(cli, ["pipelines", "history", "deploy"])

            assert result.exit_code == 0
            assert "no executions" in result.output.lower() or "0" in result.output

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_history_json_format(self, runner, mock_executions) -> None:
        """Verify history command supports --json output."""
        import json

        mock_manager = MagicMock()
        mock_manager.list_executions.return_value = mock_executions

        with patch("gobby.cli.pipelines.get_execution_manager", return_value=mock_manager):
            result = runner.invoke(cli, ["pipelines", "history", "deploy", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert "executions" in data
            assert len(data["executions"]) == 3
            assert data["executions"][0]["id"] == "pe-abc123"


class TestPipelineRunsList:
    """Tests for gobby pipelines runs list command."""

    @pytest.fixture
    def mock_executions(self):
        """Create mock pipeline executions for list tests."""
        from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

        return [
            PipelineExecution(
                id="pe-aaa111",
                pipeline_name="deploy",
                project_id="proj-1",
                status=ExecutionStatus.COMPLETED,
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:01:00Z",
            ),
            PipelineExecution(
                id="pe-bbb222",
                pipeline_name="test-suite",
                project_id="proj-1",
                status=ExecutionStatus.RUNNING,
                created_at="2026-01-02T00:00:00Z",
                updated_at="2026-01-02T00:01:00Z",
            ),
        ]

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_runs_subcommand_exists(self, runner) -> None:
        """Verify 'runs' subcommand is registered."""
        result = runner.invoke(cli, ["pipelines", "--help"])
        assert "runs" in result.output

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_old_executions_subcommand_is_removed(self, runner) -> None:
        """Verify old 'executions' subcommand is not registered."""
        result = runner.invoke(cli, ["pipelines", "executions"])
        assert result.exit_code == 2
        assert "No such command" in result.output

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_executions_lists_all(self, runner, mock_executions) -> None:
        """Verify 'gobby pipelines runs list' lists executions."""
        mock_manager = MagicMock()
        mock_manager.list_executions.return_value = mock_executions
        mock_manager.count_by_status.return_value = {"completed": 1, "running": 1}

        with patch("gobby.cli.pipelines.get_execution_manager", return_value=mock_manager):
            result = runner.invoke(cli, ["pipelines", "runs", "list"])

            assert result.exit_code == 0
            assert "pe-aaa111" in result.output
            assert "pe-bbb222" in result.output
            assert "deploy" in result.output

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_executions_status_filter(self, runner, mock_executions) -> None:
        """Verify --status flag filters executions."""
        mock_manager = MagicMock()
        mock_manager.list_executions.return_value = [mock_executions[0]]
        mock_manager.count_by_status.return_value = {"completed": 1}

        with patch("gobby.cli.pipelines.get_execution_manager", return_value=mock_manager):
            result = runner.invoke(cli, ["pipelines", "runs", "list", "--status", "completed"])

            assert result.exit_code == 0
            call_kwargs = mock_manager.list_executions.call_args
            from gobby.workflows.pipeline_state import ExecutionStatus

            assert call_kwargs.kwargs.get("status") == ExecutionStatus.COMPLETED

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_executions_name_filter(self, runner, mock_executions) -> None:
        """Verify --name filters by pipeline definition name."""
        mock_manager = MagicMock()
        mock_manager.list_executions.return_value = [mock_executions[0]]
        mock_manager.count_by_status.return_value = {"completed": 1}

        with patch("gobby.cli.pipelines.get_execution_manager", return_value=mock_manager):
            result = runner.invoke(cli, ["pipelines", "runs", "list", "--name", "deploy"])

            assert result.exit_code == 0
            call_kwargs = mock_manager.list_executions.call_args
            assert call_kwargs.kwargs.get("pipeline_name") == "deploy"

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_executions_json_output(self, runner, mock_executions) -> None:
        """Verify --json flag produces JSON output."""
        import json

        mock_manager = MagicMock()
        mock_manager.list_executions.return_value = mock_executions
        mock_manager.count_by_status.return_value = {"completed": 1, "running": 1}

        with patch("gobby.cli.pipelines.get_execution_manager", return_value=mock_manager):
            result = runner.invoke(cli, ["pipelines", "runs", "list", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert "executions" in data
            assert data["count"] == 2
            assert "status_summary" in data

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_executions_empty(self, runner) -> None:
        """Verify executions handles no results."""
        mock_manager = MagicMock()
        mock_manager.list_executions.return_value = []
        mock_manager.count_by_status.return_value = {}

        with patch("gobby.cli.pipelines.get_execution_manager", return_value=mock_manager):
            result = runner.invoke(cli, ["pipelines", "runs", "list"])

            assert result.exit_code == 0
            assert "no executions" in result.output.lower()

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_executions_invalid_status(self, runner) -> None:
        """Verify invalid --status is rejected."""
        mock_manager = MagicMock()

        with patch("gobby.cli.pipelines.get_execution_manager", return_value=mock_manager):
            result = runner.invoke(cli, ["pipelines", "runs", "list", "--status", "bogus"])

            assert result.exit_code != 0
            assert "invalid status" in result.output.lower()


class TestPipelinesSearch:
    """Tests for gobby pipelines search command."""

    @pytest.fixture
    def mock_search_results(self):
        """Create mock search results."""
        from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

        return [
            PipelineExecution(
                id="pe-search1",
                pipeline_name="deploy-prod",
                project_id="proj-1",
                status=ExecutionStatus.COMPLETED,
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:01:00Z",
            ),
        ]

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_search_subcommand_exists(self, runner) -> None:
        """Verify 'search' subcommand is registered."""
        result = runner.invoke(cli, ["pipelines", "--help"])
        assert "search" in result.output

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_search_basic(self, runner, mock_search_results) -> None:
        """Verify basic search works."""
        mock_manager = MagicMock()
        mock_manager.search_executions.return_value = mock_search_results

        with patch("gobby.cli.pipelines.get_execution_manager", return_value=mock_manager):
            result = runner.invoke(cli, ["pipelines", "search", "deploy"])

            assert result.exit_code == 0
            assert "pe-search1" in result.output
            assert "deploy-prod" in result.output
            mock_manager.search_executions.assert_called_once()

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_search_json_output(self, runner, mock_search_results) -> None:
        """Verify --json flag produces JSON output."""
        import json

        mock_manager = MagicMock()
        mock_manager.search_executions.return_value = mock_search_results

        with patch("gobby.cli.pipelines.get_execution_manager", return_value=mock_manager):
            result = runner.invoke(cli, ["pipelines", "search", "deploy", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["count"] == 1
            assert data["query"] == "deploy"

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_search_no_results(self, runner) -> None:
        """Verify search handles no results."""
        mock_manager = MagicMock()
        mock_manager.search_executions.return_value = []

        with patch("gobby.cli.pipelines.get_execution_manager", return_value=mock_manager):
            result = runner.invoke(cli, ["pipelines", "search", "nonexistent"])

            assert result.exit_code == 0
            assert "no executions" in result.output.lower()

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_search_no_errors_flag(self, runner, mock_search_results) -> None:
        """Verify --no-errors flag is passed through."""
        mock_manager = MagicMock()
        mock_manager.search_executions.return_value = mock_search_results

        with patch("gobby.cli.pipelines.get_execution_manager", return_value=mock_manager):
            result = runner.invoke(cli, ["pipelines", "search", "deploy", "--no-errors"])

            assert result.exit_code == 0
            call_kwargs = mock_manager.search_executions.call_args
            assert call_kwargs.kwargs.get("search_errors") is False


class TestPipelinesImport:
    """Tests for gobby pipelines import command."""

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_import_subcommand_exists(self, runner) -> None:
        """Verify 'import' subcommand is registered."""
        result = runner.invoke(cli, ["pipelines", "--help"])
        assert "import" in result.output

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_import_reads_lobster_file(self, runner, tmp_path) -> None:
        """Verify 'gobby pipelines import path.lobster' reads file."""
        # Create a test .lobster file
        lobster_file = tmp_path / "test.lobster"
        lobster_file.write_text("""
name: imported-pipeline
description: Imported pipeline
steps:
  - id: build
    command: npm run build
  - id: test
    command: npm test
""")

        # Create project directory structure
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        workflows_dir = gobby_dir / "workflows"
        workflows_dir.mkdir()

        with patch("gobby.cli.pipelines.get_project_path", return_value=tmp_path):
            result = runner.invoke(cli, ["pipelines", "import", str(lobster_file)])

            assert result.exit_code == 0
            assert "imported-pipeline" in result.output

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_import_saves_to_workflows_dir(self, runner, tmp_path) -> None:
        """Verify import saves converted pipeline to .gobby/workflows/."""
        # Create a test .lobster file
        lobster_file = tmp_path / "deploy.lobster"
        lobster_file.write_text("""
name: deploy
description: Deploy pipeline
steps:
  - id: deploy
    command: deploy-app
""")

        # Create project directory structure
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        workflows_dir = gobby_dir / "workflows"
        workflows_dir.mkdir()

        with patch("gobby.cli.pipelines.get_project_path", return_value=tmp_path):
            result = runner.invoke(cli, ["pipelines", "import", str(lobster_file)])

            assert result.exit_code == 0
            # Verify file was created
            saved_file = workflows_dir / "deploy.yaml"
            assert saved_file.exists()

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_import_converts_external_format(self, runner, tmp_path) -> None:
        """Verify import converts external format to Gobby format."""
        import yaml

        # Create a test .lobster file with external format fields
        lobster_file = tmp_path / "convert.lobster"
        lobster_file.write_text("""
name: convert-test
description: Test conversion
steps:
  - id: build
    command: npm run build
  - id: process
    command: process-data
    stdin: $build.stdout
  - id: deploy
    command: deploy
    approval: true
""")

        # Create project directory structure
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        workflows_dir = gobby_dir / "workflows"
        workflows_dir.mkdir()

        with patch("gobby.cli.pipelines.get_project_path", return_value=tmp_path):
            result = runner.invoke(cli, ["pipelines", "import", str(lobster_file)])

            assert result.exit_code == 0

            # Read the saved file and verify conversion
            saved_file = workflows_dir / "convert-test.yaml"
            saved_content = yaml.safe_load(saved_file.read_text())

            # Verify command -> exec conversion
            assert saved_content["steps"][0]["exec"] == "npm run build"
            # Verify stdin -> input conversion
            assert saved_content["steps"][1]["input"] == "$build.output"
            # Verify approval: true -> approval.required: true
            assert saved_content["steps"][2]["approval"]["required"] is True

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_import_outputs_saved_path(self, runner, tmp_path) -> None:
        """Verify import outputs the saved file path."""
        # Create a test .lobster file
        lobster_file = tmp_path / "test.lobster"
        lobster_file.write_text("""
name: test-pipeline
description: Test
steps:
  - id: step1
    command: echo test
""")

        # Create project directory structure
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()
        workflows_dir = gobby_dir / "workflows"
        workflows_dir.mkdir()

        with patch("gobby.cli.pipelines.get_project_path", return_value=tmp_path):
            result = runner.invoke(cli, ["pipelines", "import", str(lobster_file)])

            assert result.exit_code == 0
            assert "test-pipeline.yaml" in result.output

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_import_file_not_found(self, runner, tmp_path) -> None:
        """Verify import handles file not found error."""
        # Create project directory structure
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()

        with patch("gobby.cli.pipelines.get_project_path", return_value=tmp_path):
            result = runner.invoke(cli, ["pipelines", "import", "/nonexistent/path.lobster"])

            assert result.exit_code != 0 or "not found" in result.output.lower()

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_import_no_project(self, runner, tmp_path) -> None:
        """Verify import handles no project context."""
        # Create a test .lobster file
        lobster_file = tmp_path / "test.lobster"
        lobster_file.write_text("""
name: test
description: Test
steps:
  - id: step1
    command: echo test
""")

        with patch("gobby.cli.pipelines.get_project_path", return_value=None):
            result = runner.invoke(cli, ["pipelines", "import", str(lobster_file)])

            # Should fail or warn when no project context
            assert result.exit_code != 0 or "project" in result.output.lower()

    @pytest.mark.skipif(not pipelines_available(), reason="pipelines CLI not yet implemented")
    def test_import_custom_output(self, runner, tmp_path) -> None:
        """Verify import supports --output flag for custom destination."""
        # Create a test .lobster file
        lobster_file = tmp_path / "test.lobster"
        lobster_file.write_text("""
name: test
description: Test
steps:
  - id: step1
    command: echo test
""")

        custom_output = tmp_path / "custom-output.yaml"

        # Create project directory structure (not needed for custom output)
        gobby_dir = tmp_path / ".gobby"
        gobby_dir.mkdir()

        with patch("gobby.cli.pipelines.get_project_path", return_value=tmp_path):
            result = runner.invoke(
                cli, ["pipelines", "import", str(lobster_file), "-o", str(custom_output)]
            )

            assert result.exit_code == 0
            assert custom_output.exists()
