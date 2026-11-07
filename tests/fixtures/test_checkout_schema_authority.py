"""The test session's schema authority is this checkout's gdaemon, never an installed binary."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from gobby.storage import schema_contract
from gobby.storage.schema_identity_pin import probe_identity
from gobby.utils.native_bin import NATIVE_BIN_DIR_ENV, native_bin_name
from tests.fixtures import gdaemon_binary
from tests.fixtures.gdaemon_binary import CheckoutBinaryError, build_checkout_gdaemon

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GDAEMON = native_bin_name("gdaemon")


def test_schema_authority_ignores_a_stale_installed_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale installed gdaemon that lacks this checkout's migrations is never read."""
    installed_dir = tmp_path / "bin"
    installed_dir.mkdir()
    installed = installed_dir / _GDAEMON
    installed.write_text("#!/bin/sh\nexit 0\n")
    installed.chmod(0o755)
    monkeypatch.setenv(NATIVE_BIN_DIR_ENV, str(installed_dir))
    checkout = build_checkout_gdaemon(_REPO_ROOT, os.environ, _GDAEMON)

    assert schema_contract.installed_schema_identity() == probe_identity(checkout)


def test_cargo_artifact_for_gdaemon_is_the_build_result(tmp_path: Path) -> None:
    built = tmp_path / "debug" / _GDAEMON
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "reason": "compiler-artifact",
                    "target": {"name": "gclient"},
                    "executable": str(tmp_path / "debug" / "gclient"),
                }
            ),
            json.dumps(
                {
                    "reason": "compiler-artifact",
                    "target": {"name": "gdaemon"},
                    "executable": str(built),
                }
            ),
        ]
    )

    assert gdaemon_binary._artifact_from_cargo_json(stdout, _GDAEMON) == built


def test_failed_checkout_build_raises_and_names_the_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing cargo build is a loud error; the harness never falls back to an installed binary."""

    def failing_cargo(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=101, stdout="", stderr="boom")

    monkeypatch.setattr("tests.fixtures.gdaemon_binary.subprocess.run", failing_cargo)

    with pytest.raises(CheckoutBinaryError, match="cargo build -p gobby-daemon") as caught:
        gdaemon_binary._cargo_build_gdaemon(_REPO_ROOT, {}, _GDAEMON)

    assert "boom" in str(caught.value)


def test_build_without_a_gdaemon_artifact_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def silent_cargo(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    monkeypatch.setattr("tests.fixtures.gdaemon_binary.subprocess.run", silent_cargo)

    with pytest.raises(CheckoutBinaryError, match="no gdaemon artifact"):
        gdaemon_binary._cargo_build_gdaemon(_REPO_ROOT, {}, _GDAEMON)
