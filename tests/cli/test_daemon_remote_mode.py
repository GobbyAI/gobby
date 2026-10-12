from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from gobby.cli import cli
from gobby.cli.daemon_preflight import restart_start_refusal
from gobby.cli.installers.compose_env import ComposeEnvironmentError, resolve_compose_runtime
from gobby.config.app import DaemonConfig
from gobby.config.bootstrap import load_bootstrap

NODE_REFUSAL = (
    "datastore_mode: remote is a node; a node runs no Python daemon and never takes "
    "the hub lease. See docs/guides/shared-stack.md (Client setup)."
)


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


def test_start_refuses_node_bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_remote_bootstrap(tmp_path)
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    forbidden = AssertionError("a node must not launch or check managed services")

    with (
        patch("gobby.cli.daemon_start.worktree_daemon_refusal", return_value=None),
        patch("gobby.storage.schema_divergence.binary_set_apply_refusal", return_value=None),
        patch("gobby.cli.daemon_start.collect_dependency_report", side_effect=forbidden),
        patch("gobby.runner_pid_file.adopt_inherited_claim", side_effect=forbidden),
        patch("subprocess.Popen", side_effect=forbidden),
    ):
        result = CliRunner().invoke(cli, ["start"])

    assert result.exit_code == 1, result.output
    assert NODE_REFUSAL in result.output
    assert not (tmp_path / "gobby.pid").exists()


def test_restart_start_proof_refuses_node_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_remote_bootstrap(tmp_path)
    monkeypatch.setenv("GOBBY_HOME", str(tmp_path))
    ctx = click.Context(click.Command("restart"))

    with patch(
        "gobby.cli.daemon_preflight._open_hub",
        side_effect=AssertionError("a node must not open the hub to prove a start"),
    ):
        assert restart_start_refusal(ctx) == NODE_REFUSAL


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


def test_stop_skips_services_in_remote_mode(tmp_path: Path) -> None:
    bootstrap_path = _write_remote_bootstrap(tmp_path)
    config = DaemonConfig.model_validate(load_bootstrap(str(bootstrap_path)).to_config_dict())
    runtime = SimpleNamespace(config=config, read_only_operational_config=lambda: config)
    with (
        patch("gobby.cli.runtime.get_cli_runtime", return_value=runtime),
        patch("gobby.cli.daemon_lifecycle.get_service_status", return_value={}),
        patch("gobby.cli.daemon_lifecycle.fetch_protected_runs", return_value=[]),
        patch("gobby.cli.daemon_lifecycle.protect_pending_handoffs"),
        patch("gobby.cli.daemon_lifecycle.stop_daemon_util", return_value=True) as stop_daemon,
        patch("gobby.cli.daemon_lifecycle._services_stop") as services_stop,
        patch("shutil.which", side_effect=AssertionError("Docker must not be inspected")) as which,
    ):
        result = CliRunner().invoke(cli, ["stop", "--docker"])
    assert result.exit_code == 0, result.output
    assert "Stopping Docker containers" not in result.output
    stop_daemon.assert_called_once()
    services_stop.assert_not_called()
    which.assert_not_called()
