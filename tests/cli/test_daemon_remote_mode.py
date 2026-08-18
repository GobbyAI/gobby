from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from gobby.cli.daemon import _do_stop, _services_start, _start_dependency_errors
from gobby.cli.installers.compose_env import ComposeEnvironmentError, resolve_compose_runtime
from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import load_bootstrap


def _write_remote_bootstrap(gobby_home: Path) -> Path:
    bootstrap_path = gobby_home / "bootstrap.yaml"
    bootstrap_path.write_text(
        "datastore_mode: remote\n"
        "hub_daemon_url: http://hub.example.test:60887\n"
        "database_url: postgresql://gobby:secret@100.64.0.10:5432/gobby\n",
        encoding="utf-8",
    )
    bootstrap_path.chmod(0o600)
    return bootstrap_path


def test_start_skips_services_in_remote_mode(tmp_path: Path) -> None:
    bootstrap_path = _write_remote_bootstrap(tmp_path)
    config = DaemonConfig.model_validate(load_bootstrap(str(bootstrap_path)).to_config_dict())

    with (
        patch("gobby.cli.daemon.get_gobby_home", return_value=tmp_path),
        patch("gobby.cli.daemon.collect_dependency_report") as collect_dependencies,
        patch("gobby.cli.daemon.required_dependency_errors", return_value=[]),
    ):
        assert _start_dependency_errors() == []
    collect_dependencies.assert_called_once_with(managed_services=False, include_srt=True)

    with patch("shutil.which", side_effect=AssertionError("Docker must not be inspected")):
        result = _services_start(tmp_path)

    assert result.outcome == "skipped"
    assert "100.64.0.10" in result.detail

    runtime = SimpleNamespace(config=config, operational_config=config)
    with (
        patch("gobby.cli.runtime.get_cli_runtime", return_value=runtime),
        patch("gobby.cli.daemon.get_service_status", return_value={}),
        patch("gobby.cli.daemon.stop_daemon_util", return_value=True),
        patch("gobby.cli.daemon._services_stop") as services_stop,
    ):
        assert _do_stop(MagicMock(), docker_flag=True)
    services_stop.assert_not_called()


def test_restart_skips_services_in_remote_mode(tmp_path: Path) -> None:
    bootstrap_path = _write_remote_bootstrap(tmp_path)
    config = DaemonConfig.model_validate(load_bootstrap(str(bootstrap_path)).to_config_dict())
    runtime = SimpleNamespace(config=config, operational_config=config)

    with (
        patch("gobby.cli.runtime.get_cli_runtime", return_value=runtime),
        patch("gobby.cli.daemon.get_service_status", return_value={}),
        patch("gobby.cli.daemon.stop_daemon_util", return_value=True) as stop_daemon,
        patch("gobby.cli.daemon._services_stop") as services_stop,
        patch("shutil.which", side_effect=AssertionError("Docker must not be inspected")),
    ):
        assert _do_stop(MagicMock(), docker_flag=True, shutdown_intent="restart")
        start_result = _services_start(tmp_path)

    stop_daemon.assert_called_once_with(
        quiet=False,
        shutdown_intent="restart",
        shutdown_source="cli_restart",
    )
    services_stop.assert_not_called()
    assert start_result.outcome == "skipped"


def test_compose_runtime_rejects_remote_mode(tmp_path: Path) -> None:
    _write_remote_bootstrap(tmp_path)

    with patch("shutil.which", side_effect=AssertionError("Docker must not be inspected")):
        try:
            resolve_compose_runtime(tmp_path)
        except ComposeEnvironmentError as exc:
            assert str(exc) == (
                "this machine is in datastore_mode: remote; compose management runs on the hub"
            )
        else:
            raise AssertionError("remote compose management must fail")
