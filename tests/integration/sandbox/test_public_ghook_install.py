"""Opt-in live validation for public ghook installer artifacts."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from gobby.cli.install_setup import (
    _GHOOK_COMPATIBILITY_STAMP,
    _GHOOK_INSTALL_METHOD_ENV,
    _GHOOK_INSTALL_VERSION_ENV,
    _GHOOK_VERSION_STAMP,
    _install_ghook,
    ensure_daemon_config,
)

from .runner import ALL_SANDBOX_SPECS, SandboxRunner

pytestmark = [pytest.mark.integration, pytest.mark.slow]

REPO_ROOT = Path(__file__).resolve().parents[3]


def _require_public_install_inputs() -> tuple[str, str]:
    version = os.environ.get(_GHOOK_INSTALL_VERSION_ENV, "").strip()
    if not version:
        pytest.skip(f"set {_GHOOK_INSTALL_VERSION_ENV}=<released version> to run public install")

    method = os.environ.get(_GHOOK_INSTALL_METHOD_ENV, "").strip().lower() or "github"
    if method not in {"github", "cargo-binstall", "cargo-install"}:
        pytest.skip(
            f"{_GHOOK_INSTALL_METHOD_ENV} must be one of: github, cargo-binstall, cargo-install"
        )
    return version, method


def test_public_ghook_install_and_live_sandbox_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    version, method = _require_public_install_inputs()

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv(_GHOOK_INSTALL_VERSION_ENV, version)
    monkeypatch.setenv(_GHOOK_INSTALL_METHOD_ENV, method)
    monkeypatch.chdir(REPO_ROOT)

    ensure_daemon_config()

    result = _install_ghook(force=True)
    assert result["installed"] is True
    assert result["method"] == method
    assert result["version"] == version

    ghook_bin = home / ".gobby" / "bin" / ("ghook.exe" if os.name == "nt" else "ghook")
    assert ghook_bin.exists()
    assert (ghook_bin.parent / _GHOOK_VERSION_STAMP).read_text().strip() == version

    version_probe = subprocess.run(
        [str(ghook_bin), "--version"],
        capture_output=True,
        text=True,
        timeout=10,
        cwd=str(REPO_ROOT),
        env=os.environ.copy(),
    )
    assert version_probe.returncode == 0, version_probe.stderr or version_probe.stdout
    assert (ghook_bin.parent / _GHOOK_COMPATIBILITY_STAMP).exists()
    assert ALL_SANDBOX_SPECS, "Expected at least one sandbox spec to validate public ghook install"

    for spec in ALL_SANDBOX_SPECS:
        runner = SandboxRunner(spec)
        registered_command = runner.build_registered_command()
        assert "--gobby-owned" in registered_command
        assert str(ghook_bin) in registered_command

        diagnose_result = runner.run_diagnose(cwd=REPO_ROOT)
        runner.assert_matches_spec(diagnose_result)
