from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

from gobby.cli.workflows import workflows

pytestmark = pytest.mark.unit


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_loader():
    with patch("gobby.cli.workflows.common.get_workflow_loader") as m:
        loader = Mock()
        m.return_value = loader
        yield loader


@pytest.fixture
def mock_session_var_manager():
    with patch("gobby.cli.workflows.common.get_session_var_manager") as m:
        manager = Mock()
        m.return_value = manager
        yield manager


@pytest.fixture
def mock_resolve_session():
    with patch("gobby.cli.workflows.common.resolve_session_id") as m:
        m.return_value = "sess-123"
        yield m


def test_list_workflows_empty(runner, mock_loader) -> None:
    mock_loader.global_dirs = []
    with patch("gobby.cli.workflows.common.get_project_path", return_value=None):
        result = runner.invoke(workflows, ["list"])
        assert result.exit_code == 0
        assert "No workflows found" in result.output


def test_show_workflow_not_found(runner, mock_loader) -> None:
    mock_loader.load_workflow_sync.return_value = None
    with patch("gobby.cli.workflows.common.get_project_path", return_value=None):
        result = runner.invoke(workflows, ["show", "unknown"])
        assert result.exit_code == 1
        assert "Workflow 'unknown' not found" in result.output


def test_status_no_variables(runner, mock_session_var_manager, mock_resolve_session) -> None:
    mock_session_var_manager.get_variables.return_value = {}
    result = runner.invoke(workflows, ["status"])
    assert result.exit_code == 0
    assert "No variables set" in result.output


def test_reload_workflows_success(runner) -> None:
    with patch("gobby.config.app.load_config") as mock_conf:
        mock_conf.return_value.daemon_port = 60887

        with patch("psutil.process_iter") as mock_iter:
            proc = Mock()
            proc.cmdline.return_value = ["python", "-m", "gobby", "start"]
            mock_iter.return_value = [proc]

            with patch("httpx.post") as mock_post:
                mock_post.return_value.status_code = 200
                mock_post.return_value.json.return_value = {"status": "success"}

                result = runner.invoke(workflows, ["reload"])
                assert result.exit_code == 0
                assert "Triggered daemon workflow reload" in result.output


def test_reload_workflows_fallback(runner) -> None:
    with patch("gobby.config.app.load_config") as mock_conf:
        mock_conf.side_effect = Exception("Config error")

        with patch("gobby.cli.workflows.common.get_workflow_loader") as m_loader:
            result = runner.invoke(workflows, ["reload"])
            assert result.exit_code == 0
            assert "Cleared local workflow cache" in result.output
            m_loader.return_value.clear_cache.assert_called_once()
