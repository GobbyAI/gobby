from unittest.mock import patch

from click.testing import CliRunner

from gobby.cli.service import disable, enable, install, status, uninstall


def test_install_success():
    runner = CliRunner()
    result_mock = {
        "success": True,
        "platform": "macOS",
        "mode": "dev",
        "working_directory": "/tmp/dir",
        "python_executable": "python3",
        "log_file": "app.log",
        "plist_file": "com.gobby.plist",
        "unit_file": "gobby.service",
        "cli_note": "A note",
        "warnings": ["warn1"],
    }
    with patch("gobby.cli.service.install_service", return_value=result_mock):
        result = runner.invoke(install, ["--verbose"])
        assert result.exit_code == 0
        assert "Service installed" in result.output
        assert "A note" in result.output
        assert "warn1" in result.output
        assert "git pull" in result.output


def test_install_prod_success():
    runner = CliRunner()
    result_mock = {
        "success": True,
        "platform": "Linux",
        "mode": "prod",
    }
    with patch("gobby.cli.service.install_service", return_value=result_mock):
        result = runner.invoke(install, [])
        assert result.exit_code == 0
        assert "tool upgrade" in result.output


def test_install_failed():
    runner = CliRunner()
    result_mock = {"success": False, "error": "test error"}
    with patch("gobby.cli.service.install_service", return_value=result_mock):
        result = runner.invoke(install, [])
        assert result.exit_code == 1
        assert "Failed: test error" in result.output


def test_uninstall_success():
    runner = CliRunner()
    result_mock = {"success": True, "platform": "macOS"}
    with patch("gobby.cli.service.uninstall_service", return_value=result_mock):
        result = runner.invoke(uninstall, ["--yes"])
        assert result.exit_code == 0
        assert "Service uninstalled (macOS)" in result.output


def test_uninstall_failed():
    runner = CliRunner()
    result_mock = {"success": False, "error": "test error"}
    with patch("gobby.cli.service.uninstall_service", return_value=result_mock):
        result = runner.invoke(uninstall, ["--yes"])
        assert result.exit_code == 1
        assert "Failed: test error" in result.output


def test_status_not_installed():
    runner = CliRunner()
    result_mock = {"installed": False, "platform": "macOS"}
    with patch("gobby.cli.service.get_service_status", return_value=result_mock):
        result = runner.invoke(status, [])
        assert result.exit_code == 0
        assert "not installed" in result.output


def test_status_running():
    runner = CliRunner()
    result_mock = {
        "installed": True,
        "platform": "Linux",
        "enabled": True,
        "running": True,
        "mode": "dev",
        "pid": 1234,
        "plist_file": "x.plist",
        "unit_file": "y.unit",
        "warnings": ["w1"],
    }
    with patch("gobby.cli.service.get_service_status", return_value=result_mock):
        result = runner.invoke(status, [])
        assert result.exit_code == 0
        assert "running" in result.output
        assert "1234" in result.output
        assert "x.plist" in result.output
        assert "y.unit" in result.output
        assert "w1" in result.output


def test_status_enabled_not_running():
    runner = CliRunner()
    result_mock = {"installed": True, "enabled": True, "running": False, "mode": "prod"}
    with patch("gobby.cli.service.get_service_status", return_value=result_mock):
        result = runner.invoke(status, [])
        assert result.exit_code == 0
        assert "enabled, not running" in result.output


def test_status_disabled():
    runner = CliRunner()
    result_mock = {"installed": True, "enabled": False, "running": False, "mode": "prod"}
    with patch("gobby.cli.service.get_service_status", return_value=result_mock):
        result = runner.invoke(status, [])
        assert result.exit_code == 0
        assert "(disabled" in result.output


def test_enable_success():
    runner = CliRunner()
    with patch(
        "gobby.cli.service.enable_service", return_value={"success": True, "platform": "macOS"}
    ):
        result = runner.invoke(enable, [])
        assert result.exit_code == 0
        assert "enabled" in result.output


def test_enable_failed():
    runner = CliRunner()
    with patch("gobby.cli.service.enable_service", return_value={"success": False, "error": "e"}):
        result = runner.invoke(enable, [])
        assert result.exit_code == 1


def test_disable_success():
    runner = CliRunner()
    with patch(
        "gobby.cli.service.disable_service", return_value={"success": True, "platform": "macOS"}
    ):
        result = runner.invoke(disable, [])
        assert result.exit_code == 0
        assert "disabled" in result.output


def test_disable_failed():
    runner = CliRunner()
    with patch("gobby.cli.service.disable_service", return_value={"success": False, "error": "e"}):
        result = runner.invoke(disable, [])
        assert result.exit_code == 1
