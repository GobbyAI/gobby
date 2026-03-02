from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.workflows import workflows
from gobby.workflows.definitions import WorkflowDefinition, WorkflowStep

pytestmark = pytest.mark.unit

# Mock workflow definition
MOCK_WORKFLOW = WorkflowDefinition(
    name="test-workflow",
    description="A test workflow",
    enabled=False,
    version="1.0",
    steps=[
        WorkflowStep(name="step1", description="First step"),
        WorkflowStep(name="step2", description="Second step"),
    ],
)


@pytest.fixture
def mock_loader():
    with patch("gobby.cli.workflows.common.get_workflow_loader") as mock:
        yield mock.return_value


@pytest.fixture
def mock_session_var_manager():
    with patch("gobby.cli.workflows.common.get_session_var_manager") as mock:
        yield mock.return_value


def test_list_workflows_empty(mock_loader) -> None:
    """Test 'workflows list' with no workflows."""
    mock_loader.global_dirs = []

    with patch("gobby.cli.workflows.common.Path.cwd") as mock_cwd:
        mock_cwd.return_value = MagicMock()
        mock_cwd.return_value.__truediv__.return_value.exists.return_value = False

        runner = CliRunner()
        result = runner.invoke(workflows, ["list"])

    assert result.exit_code == 0
    assert "No workflows found" in result.output


@patch("gobby.cli.workflows.common.get_project_path", return_value=None)
@patch("gobby.cli.workflows.inspect.yaml.safe_load")
@patch("gobby.cli.workflows.inspect.open", new_callable=MagicMock)
def test_list_workflows_found(mock_open, mock_yaml, mock_project_path, mock_loader):
    """Test 'workflows list' finding files."""
    mock_dir = MagicMock()
    mock_dir.exists.return_value = True

    mock_file = MagicMock()
    mock_file.stem = "test-workflow"
    mock_dir.glob.return_value = [mock_file]

    mock_loader.global_dirs = [mock_dir]

    mock_open.return_value.__enter__.return_value.read.return_value = (
        "name: test-workflow\ntype: step\n"
    )
    mock_yaml.return_value = {
        "name": "test-workflow",
        "type": "step",
        "description": "desc",
    }

    runner = CliRunner()
    result = runner.invoke(workflows, ["list"])

    assert result.exit_code == 0
    assert "test-workflow" in result.output
    assert "(enabled)" in result.output
    assert "desc" in result.output


def test_show_workflow(mock_loader) -> None:
    """Test 'workflows show' with valid name."""
    mock_loader.load_workflow_sync.return_value = MOCK_WORKFLOW

    runner = CliRunner()
    result = runner.invoke(workflows, ["show", "test-workflow"])

    assert result.exit_code == 0
    assert "Workflow: test-workflow" in result.output
    assert "Steps (2):" in result.output
    assert "- step1" in result.output


def test_show_workflow_not_found(mock_loader) -> None:
    """Test 'workflows show' with invalid name."""
    mock_loader.load_workflow_sync.return_value = None

    runner = CliRunner()
    result = runner.invoke(workflows, ["show", "invalid"])

    assert result.exit_code == 1
    assert "not found" in result.output


def test_status_no_variables(mock_session_var_manager) -> None:
    """Test 'workflows status' with no variables set."""
    mock_session_var_manager.get_variables.return_value = {}

    runner = CliRunner()
    with patch("gobby.cli.workflows.common.resolve_session_id", return_value="sess-123"):
        result = runner.invoke(workflows, ["status"])

    assert result.exit_code == 0
    assert "No variables set" in result.output
