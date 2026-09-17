"""The daemon keeps process-scoped launch environment out of its children (plan 3.1)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gobby.agents.constants import IDENTITY_ENV_VARS
from gobby.runner import main
from gobby.runner_pid_file import SERVICE_LAUNCH_ENV, SERVICE_NONCE_ENV

pytestmark = pytest.mark.unit


def test_main_pops_inherited_identity_before_the_runner_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A daemon restarted from a pane must not hand that pane's identity to its children."""
    for name in IDENTITY_ENV_VARS:
        monkeypatch.setenv(name, f"inherited-{name.lower()}")
    identity_at_runner_start: list[set[str]] = []

    def record_runner_environment(**_kwargs: object) -> None:
        identity_at_runner_start.append({name for name in IDENTITY_ENV_VARS if name in os.environ})

    ownership = MagicMock()
    bootstrap = MagicMock(daemon_port=8765, bind_host="localhost")
    with (
        patch("gobby.config.bootstrap.load_bootstrap", return_value=bootstrap),
        patch("gobby.runner._healthy_daemon_running", return_value=False),
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.runner_pid_file.adopt_inherited_claim", return_value=ownership),
        patch("gobby.runner.run_gobby", new=record_runner_environment),
        patch("asyncio.run"),
    ):
        main()

    assert identity_at_runner_start == [set()]
    assert {name for name in IDENTITY_ENV_VARS if name in os.environ} == set()
    ownership.release.assert_called_once_with()


def test_main_pops_the_service_marker_once_admission_consumed_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only the service-launched runner is admitted by the marker; its children start ordinarily."""
    nonce_path = str(tmp_path / "gobby.pid.service-nonce")
    monkeypatch.setenv(SERVICE_LAUNCH_ENV, "1")
    monkeypatch.setenv(SERVICE_NONCE_ENV, nonce_path)
    service_env = (SERVICE_LAUNCH_ENV, SERVICE_NONCE_ENV)
    marker_at_admission: list[dict[str, str]] = []
    marker_at_runner_start: list[set[str]] = []
    ownership = MagicMock()

    def admit(_pid_file: Path) -> MagicMock:
        marker_at_admission.append({name: os.environ[name] for name in service_env})
        return ownership

    def record_runner_environment(**_kwargs: object) -> None:
        marker_at_runner_start.append({name for name in service_env if name in os.environ})

    bootstrap = MagicMock(daemon_port=8765, bind_host="localhost")
    with (
        patch("gobby.config.bootstrap.load_bootstrap", return_value=bootstrap),
        patch("gobby.runner._healthy_daemon_running", return_value=False),
        patch("gobby.cli.utils.get_gobby_home", return_value=tmp_path),
        patch("gobby.runner_pid_file.convert_or_acquire_service_claim", new=admit),
        patch("gobby.runner.run_gobby", new=record_runner_environment),
        patch("asyncio.run"),
    ):
        main()

    assert marker_at_admission == [{SERVICE_LAUNCH_ENV: "1", SERVICE_NONCE_ENV: nonce_path}]
    assert marker_at_runner_start == [set()]
    ownership.release.assert_called_once_with()
