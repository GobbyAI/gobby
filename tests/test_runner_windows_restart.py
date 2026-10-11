"""Windows retains its detached HTTP restart handoff."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from gobby.runner_windows_restart import restart_after_exit, spawn_restart_helper

pytestmark = pytest.mark.unit


def test_windows_restart_helper_is_detached() -> None:
    with patch("gobby.runner_windows_restart.subprocess.Popen") as launch:
        spawn_restart_helper(123, 20000)
    launch.assert_called_once()
    args, kwargs = launch.call_args
    assert args[0][-2:] == ["123", "20000"]
    assert kwargs["start_new_session"] is True


@pytest.mark.parametrize("service_managed", [False, True])
def test_windows_restart_uses_its_existing_launch_path(
    tmp_path: Path, service_managed: bool
) -> None:
    service_restart = Mock(return_value={"success": True})
    with (
        patch("gobby.cli.utils._is_process_alive", return_value=False),
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch(
            "gobby.cli.installers.service.get_service_status",
            return_value={
                "installed": service_managed,
                "enabled": service_managed,
            },
        ),
        patch("gobby.cli.installers.service.service_restart", service_restart),
        patch("gobby.runner._healthy_daemon_running", return_value=False),
        patch("gobby.runner_windows_restart.time.monotonic", side_effect=[0.0, 0.0, 4.0]),
        patch("gobby.runner_windows_restart.time.sleep"),
        patch("gobby.runner_windows_restart.subprocess.Popen") as launch,
    ):
        restart_after_exit(123, 20000)
    if service_managed:
        service_restart.assert_called_once_with(shutdown_source="http_restart")
        launch.assert_not_called()
    else:
        service_restart.assert_not_called()
        launch.assert_called_once()
        assert launch.call_args.args[0] == [sys.executable, "-m", "gobby.runner"]
        options = launch.call_args.kwargs
        assert options["start_new_session"] is True
        assert options["stdin"] == subprocess.DEVNULL
        assert Path(options["stdout"].name) == tmp_path / "logs" / "admin-restart.log"
        assert options["stderr"] is options["stdout"]
