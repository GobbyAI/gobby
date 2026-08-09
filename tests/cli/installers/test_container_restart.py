"""Managed service container restart-policy tests."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from gobby.cli.installers.container_restart import (
    DISABLED_RESTART_POLICY,
    MANAGED_SERVICE_CONTAINERS,
    apply_managed_service_restart_policy,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("enabled", "expected_policy"),
    [
        (True, "unless-stopped"),
        (False, DISABLED_RESTART_POLICY),
    ],
)
def test_apply_restart_policy_updates_compose_and_existing_containers(
    tmp_path: Path,
    enabled: bool,
    expected_policy: str,
) -> None:
    completed = subprocess.CompletedProcess[str]([], 0, stdout="", stderr="")

    with (
        patch(
            "gobby.cli.installers.container_restart.shutil.which", return_value="/usr/bin/docker"
        ),
        patch(
            "gobby.cli.installers.container_restart.subprocess.run",
            return_value=completed,
        ) as run,
    ):
        result = apply_managed_service_restart_policy(
            enabled=enabled,
            gobby_home=tmp_path,
        )

    assert result["success"] is True
    compose_file = tmp_path / "services" / "docker-compose.yml"
    compose = yaml.safe_load(compose_file.read_text())
    assert {service["restart"] for service in compose["services"].values()} == {expected_policy}
    run.assert_called_once_with(
        [
            "/usr/bin/docker",
            "update",
            "--restart",
            expected_policy,
            *MANAGED_SERVICE_CONTAINERS,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_apply_restart_policy_surfaces_docker_update_failure(tmp_path: Path) -> None:
    completed = subprocess.CompletedProcess[str](
        [],
        1,
        stdout="",
        stderr="missing container",
    )

    with (
        patch(
            "gobby.cli.installers.container_restart.shutil.which", return_value="/usr/bin/docker"
        ),
        patch(
            "gobby.cli.installers.container_restart.subprocess.run",
            return_value=completed,
        ),
    ):
        result = apply_managed_service_restart_policy(
            enabled=True,
            gobby_home=tmp_path,
        )

    assert result["success"] is False
    assert result["error"] == "Docker restart-policy update failed: missing container"
