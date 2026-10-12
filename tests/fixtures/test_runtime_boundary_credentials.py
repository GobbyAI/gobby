"""Restore proofs skip unavailable seat credentials and retain the real installer path."""

import subprocess
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from tests.e2e import test_runtime_boundary

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("docker_result", ["unavailable", "no-credential"])
def test_restore_skips_before_datastore_changes_without_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, docker_result: str
) -> None:
    monkeypatch.delenv("GOBBY_FALKORDB_PASSWORD", raising=False)
    with (
        patch("tests.e2e.test_runtime_boundary.subprocess.run") as inspect,
        patch.object(test_runtime_boundary, "_start_qdrant_key_proxy") as proxy,
    ):
        if docker_result == "unavailable":
            inspect.side_effect = OSError("Docker unavailable")
        else:
            inspect.return_value = subprocess.CompletedProcess(
                ["docker"], 0, stdout="OTHER_SETTING=value\n"
            )
        with pytest.raises(
            pytest.skip.Exception, match="FalkorDB credential unavailable inside the seat sandbox"
        ):
            test_runtime_boundary.test_restore_replay_rejected(
                cast(test_runtime_boundary.BoundaryHarness, None),
                None,
                "unused",
                "unused",
                tmp_path,
            )
        proxy.assert_not_called()


def test_restore_uses_installer_when_credentials_resolve(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GOBBY_FALKORDB_PASSWORD", "fixture-only-password")
    # The installer probe raises before the harness or database is accessed.
    boundary = cast(test_runtime_boundary.BoundaryHarness, None)
    with (
        patch.object(test_runtime_boundary, "_start_qdrant_key_proxy") as proxy,
        patch.object(test_runtime_boundary, "_install_direct_datastores") as install,
    ):
        proxy.return_value.url = "http://fixture.invalid"
        install.side_effect = RuntimeError("installer reached")
        with pytest.raises(RuntimeError, match="installer reached"):
            test_runtime_boundary.test_restore_replay_rejected(
                boundary, None, "unused", "unused", tmp_path
            )
        install.assert_called_once_with(boundary, qdrant_url="http://fixture.invalid")
        proxy.return_value.close.assert_called_once_with()
