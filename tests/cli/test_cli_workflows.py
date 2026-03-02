import json
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.workflows import workflows
from gobby.workflows.definitions import WorkflowDefinition, WorkflowStep

pytestmark = pytest.mark.unit


@pytest.fixture
def mock_loader():
    loader = MagicMock()
    loader.global_dirs = []
    return loader


@pytest.fixture
def mock_session_var_manager():
    return MagicMock()


@pytest.fixture
def cli_runner():
    return CliRunner()


# ==============================================================================
# Tests for list_workflows
# ==============================================================================


def test_list_workflows_empty(cli_runner, mock_loader) -> None:
    with patch("gobby.cli.workflows.common.get_workflow_loader", return_value=mock_loader):
        with patch("gobby.cli.workflows.common.get_project_path", return_value=None):
            result = cli_runner.invoke(workflows, ["list"])
            assert result.exit_code == 0
            assert "No workflows found" in result.output


def test_list_workflows_found(cli_runner, mock_loader, tmp_path) -> None:
    with patch("gobby.cli.workflows.common.get_workflow_loader", return_value=mock_loader):
        with patch("gobby.cli.workflows.common.get_project_path", return_value=tmp_path):
            wf_file = tmp_path / "test_wf.yaml"
            wf_file.write_text("name: Test Workflow\ntype: step\ndescription: A test")

            mock_loader.global_dirs = [tmp_path]

            result = cli_runner.invoke(workflows, ["list", "--global"])

            assert result.exit_code == 0
            assert "test_wf" in result.output
            assert "(enabled)" in result.output


def test_list_workflows_json_format(cli_runner, mock_loader, tmp_path) -> None:
    """Test list workflows with JSON output format."""
    with patch("gobby.cli.workflows.common.get_workflow_loader", return_value=mock_loader):
        with patch("gobby.cli.workflows.common.get_project_path", return_value=tmp_path):
            wf_file = tmp_path / "my_workflow.yaml"
            wf_file.write_text("name: My Workflow\ntype: step\ndescription: Test")
            mock_loader.global_dirs = [tmp_path]

            result = cli_runner.invoke(workflows, ["list", "--json", "--global"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert "workflows" in data
            assert "count" in data


def test_list_workflows_with_all_flag(cli_runner, mock_loader, tmp_path) -> None:
    """Test list workflows with --all flag."""
    with patch("gobby.cli.workflows.common.get_workflow_loader", return_value=mock_loader):
        with patch("gobby.cli.workflows.common.get_project_path", return_value=tmp_path):
            wf_file = tmp_path / "lifecycle_wf.yaml"
            wf_file.write_text(
                "name: Lifecycle\ntype: lifecycle\ndescription: A lifecycle workflow"
            )
            mock_loader.global_dirs = [tmp_path]

            result = cli_runner.invoke(workflows, ["list", "--all", "--global"])

            assert result.exit_code == 0
            assert "lifecycle_wf" in result.output
            assert "(enabled)" in result.output


# ==============================================================================
# Tests for show_workflow
# ==============================================================================


def test_show_workflow(cli_runner, mock_loader) -> None:
    with patch("gobby.cli.workflows.common.get_workflow_loader", return_value=mock_loader):
        with patch("gobby.cli.workflows.common.get_project_path", return_value=None):
            defi = WorkflowDefinition(
                name="test", steps=[WorkflowStep(name="step1", description="step desc")]
            )
            mock_loader.load_workflow_sync.return_value = defi

            result = cli_runner.invoke(workflows, ["show", "test"])

            assert result.exit_code == 0
            assert "Workflow: test" in result.output
            assert "step1" in result.output
            assert "step desc" in result.output


def test_show_workflow_not_found(cli_runner, mock_loader) -> None:
    """Test show workflow when workflow not found."""
    with patch("gobby.cli.workflows.common.get_workflow_loader", return_value=mock_loader):
        with patch("gobby.cli.workflows.common.get_project_path", return_value=None):
            mock_loader.load_workflow_sync.return_value = None

            result = cli_runner.invoke(workflows, ["show", "nonexistent"])

            assert result.exit_code == 1
            assert "not found" in result.output


def test_show_workflow_json_format(cli_runner, mock_loader) -> None:
    """Test show workflow with JSON output."""
    with patch("gobby.cli.workflows.common.get_workflow_loader", return_value=mock_loader):
        with patch("gobby.cli.workflows.common.get_project_path", return_value=None):
            defi = WorkflowDefinition(
                name="test",
                steps=[WorkflowStep(name="step1", description="step desc")],
            )
            mock_loader.load_workflow_sync.return_value = defi

            result = cli_runner.invoke(workflows, ["show", "test", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["name"] == "test"


def test_show_workflow_with_tools(cli_runner, mock_loader) -> None:
    """Test show workflow with allowed/blocked tools."""
    with patch("gobby.cli.workflows.common.get_workflow_loader", return_value=mock_loader):
        with patch("gobby.cli.workflows.common.get_project_path", return_value=None):
            defi = WorkflowDefinition(
                name="test",
                steps=[
                    WorkflowStep(
                        name="plan",
                        description="Planning step",
                        allowed_tools=["Read", "Glob", "Grep"],
                        blocked_tools=["Edit", "Write"],
                    )
                ],
            )
            mock_loader.load_workflow_sync.return_value = defi

            result = cli_runner.invoke(workflows, ["show", "test"])

            assert result.exit_code == 0
            assert "Allowed tools:" in result.output
            assert "Blocked tools:" in result.output


# ==============================================================================
# Tests for status command
# ==============================================================================


def test_status_no_session(cli_runner) -> None:
    import click

    with patch(
        "gobby.cli.workflows.common.get_session_var_manager", return_value=MagicMock()
    ):
        with patch(
            "gobby.cli.workflows.common.resolve_session_id",
            side_effect=click.ClickException("No active session found"),
        ):
            result = cli_runner.invoke(workflows, ["status"])
            assert result.exit_code == 1
            assert "No active session found" in result.output


def test_status_with_variables(cli_runner, mock_session_var_manager) -> None:
    """Test status showing session variables."""
    with patch(
        "gobby.cli.workflows.common.get_session_var_manager",
        return_value=mock_session_var_manager,
    ):
        with patch("gobby.cli.workflows.common.resolve_session_id", return_value="sess1"):
            mock_session_var_manager.get_variables.return_value = {
                "task_claimed": True,
                "session_task": "#42",
            }

            result = cli_runner.invoke(workflows, ["status", "--session", "sess1"])

            assert result.exit_code == 0
            assert "task_claimed" in result.output
            assert "session_task" in result.output


def test_status_json_format(cli_runner, mock_session_var_manager) -> None:
    """Test status with JSON output format."""
    with patch(
        "gobby.cli.workflows.common.get_session_var_manager",
        return_value=mock_session_var_manager,
    ):
        with patch("gobby.cli.workflows.common.resolve_session_id", return_value="sess1"):
            mock_session_var_manager.get_variables.return_value = {
                "task_claimed": True,
                "counter": 5,
            }

            result = cli_runner.invoke(
                workflows, ["status", "--session", "sess1", "--json"]
            )

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["has_variables"] is True
            assert data["variables"]["task_claimed"] is True
            assert data["variables"]["counter"] == 5


def test_status_no_variables(cli_runner, mock_session_var_manager) -> None:
    """Test status when no variables are set."""
    with patch(
        "gobby.cli.workflows.common.get_session_var_manager",
        return_value=mock_session_var_manager,
    ):
        with patch("gobby.cli.workflows.common.resolve_session_id", return_value="sess1"):
            mock_session_var_manager.get_variables.return_value = {}

            result = cli_runner.invoke(workflows, ["status", "--session", "sess1"])

            assert result.exit_code == 0
            assert "No variables set" in result.output


# ==============================================================================
# Tests for import command
# ==============================================================================


def test_import_workflow_file(cli_runner, tmp_path) -> None:
    """Test importing a workflow from a file."""
    source_file = tmp_path / "source_wf.yaml"
    source_file.write_text("name: Imported Workflow\ntype: step\ndescription: Test")

    project_dir = tmp_path / "project"
    project_dir.mkdir()
    gobby_dir = project_dir / ".gobby"
    gobby_dir.mkdir()

    with patch("gobby.cli.workflows.common.get_project_path", return_value=project_dir):
        result = cli_runner.invoke(workflows, ["import", str(source_file)])

        assert result.exit_code == 0
        assert "Imported workflow" in result.output


def test_import_workflow_not_yaml(cli_runner, tmp_path) -> None:
    """Test importing a non-YAML file fails."""
    source_file = tmp_path / "source.txt"
    source_file.write_text("not yaml")

    result = cli_runner.invoke(workflows, ["import", str(source_file)])

    assert result.exit_code == 1
    assert ".yaml or .yml extension" in result.output


def test_import_workflow_file_not_found(cli_runner) -> None:
    """Test importing non-existent file."""
    result = cli_runner.invoke(workflows, ["import", "/nonexistent/path.yaml"])

    assert result.exit_code == 1
    assert "not found" in result.output


# ==============================================================================
# Tests for audit command
# ==============================================================================


def test_audit_no_entries(cli_runner) -> None:
    """Test audit with no entries."""
    with patch("gobby.cli.workflows.common.resolve_session_id", return_value="sess1"):
        with patch("gobby.storage.workflow_audit.WorkflowAuditManager") as MockAudit:
            mock_audit = MagicMock()
            mock_audit.get_entries.return_value = []
            MockAudit.return_value = mock_audit

            result = cli_runner.invoke(workflows, ["audit", "--session", "sess1"])

            assert result.exit_code == 0
            assert "No audit entries found" in result.output


def test_audit_with_entries(cli_runner) -> None:
    """Test audit with entries."""
    from datetime import UTC, datetime

    with patch("gobby.cli.workflows.common.resolve_session_id", return_value="sess1"):
        with patch("gobby.storage.workflow_audit.WorkflowAuditManager") as MockAudit:
            mock_entry = MagicMock()
            mock_entry.id = "entry-1"
            mock_entry.timestamp = datetime.now(UTC)
            mock_entry.step = "plan"
            mock_entry.event_type = "tool_call"
            mock_entry.tool_name = "Edit"
            mock_entry.rule_id = None
            mock_entry.condition = None
            mock_entry.result = "block"
            mock_entry.reason = "Edit not allowed in plan step"
            mock_entry.context = {}

            mock_audit = MagicMock()
            mock_audit.get_entries.return_value = [mock_entry]
            MockAudit.return_value = mock_audit

            result = cli_runner.invoke(workflows, ["audit", "--session", "sess1"])

            assert result.exit_code == 0
            assert "BLOCK" in result.output
            assert "tool_call" in result.output


def test_audit_json_format(cli_runner) -> None:
    """Test audit with JSON output."""
    from datetime import UTC, datetime

    with patch("gobby.cli.workflows.common.resolve_session_id", return_value="sess1"):
        with patch("gobby.storage.workflow_audit.WorkflowAuditManager") as MockAudit:
            mock_entry = MagicMock()
            mock_entry.id = "entry-1"
            mock_entry.timestamp = datetime.now(UTC)
            mock_entry.step = "plan"
            mock_entry.event_type = "tool_call"
            mock_entry.tool_name = "Edit"
            mock_entry.rule_id = None
            mock_entry.condition = None
            mock_entry.result = "allow"
            mock_entry.reason = None
            mock_entry.context = {}

            mock_audit = MagicMock()
            mock_audit.get_entries.return_value = [mock_entry]
            MockAudit.return_value = mock_audit

            result = cli_runner.invoke(
                workflows, ["audit", "--session", "sess1", "--json"]
            )

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert isinstance(data, list)
            assert len(data) == 1


# ==============================================================================
# Tests for set-var command
# ==============================================================================


def test_set_variable_string(cli_runner, mock_session_var_manager) -> None:
    """Test setting a string variable."""
    with patch(
        "gobby.cli.workflows.common.get_session_var_manager",
        return_value=mock_session_var_manager,
    ):
        with patch("gobby.cli.workflows.common.resolve_session_id", return_value="sess1"):
            result = cli_runner.invoke(workflows, ["set-var", "my_var", "my_value"])

            assert result.exit_code == 0
            assert "Set my_var" in result.output
            mock_session_var_manager.set_variable.assert_called_once_with(
                "sess1", "my_var", "my_value"
            )


def test_set_variable_boolean(cli_runner, mock_session_var_manager) -> None:
    """Test setting a boolean variable."""
    with patch(
        "gobby.cli.workflows.common.get_session_var_manager",
        return_value=mock_session_var_manager,
    ):
        with patch("gobby.cli.workflows.common.resolve_session_id", return_value="sess1"):
            result = cli_runner.invoke(workflows, ["set-var", "debug_mode", "true"])

            assert result.exit_code == 0
            assert "Set debug_mode" in result.output
            mock_session_var_manager.set_variable.assert_called_once_with(
                "sess1", "debug_mode", True
            )


def test_set_variable_integer(cli_runner, mock_session_var_manager) -> None:
    """Test setting an integer variable."""
    with patch(
        "gobby.cli.workflows.common.get_session_var_manager",
        return_value=mock_session_var_manager,
    ):
        with patch("gobby.cli.workflows.common.resolve_session_id", return_value="sess1"):
            result = cli_runner.invoke(workflows, ["set-var", "max_retries", "5"])

            assert result.exit_code == 0
            mock_session_var_manager.set_variable.assert_called_once_with(
                "sess1", "max_retries", 5
            )


def test_set_variable_null(cli_runner, mock_session_var_manager) -> None:
    """Test setting a null variable."""
    with patch(
        "gobby.cli.workflows.common.get_session_var_manager",
        return_value=mock_session_var_manager,
    ):
        with patch("gobby.cli.workflows.common.resolve_session_id", return_value="sess1"):
            result = cli_runner.invoke(workflows, ["set-var", "existing", "null"])

            assert result.exit_code == 0
            mock_session_var_manager.set_variable.assert_called_once_with(
                "sess1", "existing", None
            )


# ==============================================================================
# Tests for get-var command
# ==============================================================================


def test_get_variable_specific(cli_runner, mock_session_var_manager) -> None:
    """Test getting a specific variable."""
    with patch(
        "gobby.cli.workflows.common.get_session_var_manager",
        return_value=mock_session_var_manager,
    ):
        with patch("gobby.cli.workflows.common.resolve_session_id", return_value="sess1"):
            mock_session_var_manager.get_variables.return_value = {"my_var": "my_value"}

            result = cli_runner.invoke(workflows, ["get-var", "my_var"])

            assert result.exit_code == 0
            assert "my_var = 'my_value'" in result.output


def test_get_variable_not_set(cli_runner, mock_session_var_manager) -> None:
    """Test getting a variable that's not set."""
    with patch(
        "gobby.cli.workflows.common.get_session_var_manager",
        return_value=mock_session_var_manager,
    ):
        with patch("gobby.cli.workflows.common.resolve_session_id", return_value="sess1"):
            mock_session_var_manager.get_variables.return_value = {}

            result = cli_runner.invoke(workflows, ["get-var", "nonexistent"])

            assert result.exit_code == 0
            assert "not set" in result.output


def test_get_all_variables(cli_runner, mock_session_var_manager) -> None:
    """Test getting all variables."""
    with patch(
        "gobby.cli.workflows.common.get_session_var_manager",
        return_value=mock_session_var_manager,
    ):
        with patch("gobby.cli.workflows.common.resolve_session_id", return_value="sess1"):
            mock_session_var_manager.get_variables.return_value = {
                "var1": "value1",
                "var2": 42,
            }

            result = cli_runner.invoke(workflows, ["get-var"])

            assert result.exit_code == 0
            assert "var1" in result.output
            assert "var2" in result.output


def test_get_all_variables_json(cli_runner, mock_session_var_manager) -> None:
    """Test getting all variables in JSON format."""
    with patch(
        "gobby.cli.workflows.common.get_session_var_manager",
        return_value=mock_session_var_manager,
    ):
        with patch("gobby.cli.workflows.common.resolve_session_id", return_value="sess1"):
            mock_session_var_manager.get_variables.return_value = {
                "var1": "value1",
                "var2": 42,
            }

            result = cli_runner.invoke(workflows, ["get-var", "--json"])

            assert result.exit_code == 0
            data = json.loads(result.output)
            assert data["variables"]["var1"] == "value1"
            assert data["variables"]["var2"] == 42
