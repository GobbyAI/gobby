"""Tests for the ``gobby setup`` Python shim."""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.unit

setup_mod = importlib.import_module("gobby.cli.setup")


# ---------------------------------------------------------------------------
# CLI command registration
# ---------------------------------------------------------------------------


def test_setup_command_exists() -> None:
    """Verify the setup command is registered and has help text."""
    runner = CliRunner()
    result = runner.invoke(setup_mod.setup, ["--help"])
    assert result.exit_code == 0
    assert "First-run setup wizard" in result.output


# ---------------------------------------------------------------------------
# Node.js check
# ---------------------------------------------------------------------------


def test_setup_exits_if_node_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exit with error if node is not found."""
    monkeypatch.setattr("shutil.which", lambda x: None)
    runner = CliRunner()
    result = runner.invoke(setup_mod.setup)
    assert result.exit_code != 0
    assert "Node.js is required" in result.output


# ---------------------------------------------------------------------------
# Bundle check
# ---------------------------------------------------------------------------


def test_setup_exits_if_bundle_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exit with error if the bundled setup.mjs is not found."""
    monkeypatch.setattr("shutil.which", lambda x: "/usr/local/bin/node" if x == "node" else None)
    monkeypatch.setattr(
        setup_mod,
        "get_install_dir",
        lambda: tmp_path / "install",
    )
    runner = CliRunner()
    result = runner.invoke(setup_mod.setup)
    assert result.exit_code != 0
    assert "Setup bundle not found" in result.output


# ---------------------------------------------------------------------------
# Subprocess delegation
# ---------------------------------------------------------------------------


def test_setup_delegates_to_node(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify setup delegates to node with the bundled setup.mjs."""
    install_dir = tmp_path / "install"
    bundle_dir = install_dir / "shared" / "setup"
    bundle_dir.mkdir(parents=True)
    bundle = bundle_dir / "setup.mjs"
    bundle.write_text("// mock bundle")

    monkeypatch.setattr("shutil.which", lambda x: "/usr/local/bin/node" if x == "node" else None)
    monkeypatch.setattr(setup_mod, "get_install_dir", lambda: install_dir)

    mock_run = MagicMock(return_value=MagicMock(returncode=0))

    with patch("subprocess.run", mock_run):
        runner = CliRunner()
        runner.invoke(setup_mod.setup)

    # subprocess.run should have been called
    assert mock_run.called
    call_args = mock_run.call_args
    cmd = call_args[0][0]
    assert cmd[0] == "/usr/local/bin/node"
    assert cmd[1].endswith("setup.mjs")

    # Environment should include GOBBY_SKIP_BOOTSTRAP
    env = call_args[1].get("env", call_args.kwargs.get("env", {}))
    assert env.get("GOBBY_SKIP_BOOTSTRAP") == "1"


def test_setup_passes_env_vars(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify setup passes GOBBY_HOME, GOBBY_INSTALL_DIR, GOBBY_BIN env vars."""
    install_dir = tmp_path / "install"
    bundle_dir = install_dir / "shared" / "setup"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "setup.mjs").write_text("// mock")

    monkeypatch.setattr(
        "shutil.which",
        lambda x: {
            "node": "/usr/local/bin/node",
            "gobby": "/usr/local/bin/gobby",
        }.get(x),
    )
    monkeypatch.setattr(setup_mod, "get_install_dir", lambda: install_dir)

    mock_run = MagicMock(return_value=MagicMock(returncode=0))

    with patch("subprocess.run", mock_run):
        runner = CliRunner()
        runner.invoke(setup_mod.setup)

    env = mock_run.call_args[1].get("env", mock_run.call_args.kwargs.get("env", {}))
    assert env.get("GOBBY_INSTALL_DIR") == str(install_dir)
    assert env.get("GOBBY_BIN") == "/usr/local/bin/gobby"
    assert "GOBBY_HOME" in env


def test_setup_propagates_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Verify setup propagates the node process exit code."""
    install_dir = tmp_path / "install"
    bundle_dir = install_dir / "shared" / "setup"
    bundle_dir.mkdir(parents=True)
    (bundle_dir / "setup.mjs").write_text("// mock")

    monkeypatch.setattr("shutil.which", lambda x: "/usr/local/bin/node" if x == "node" else None)
    monkeypatch.setattr(setup_mod, "get_install_dir", lambda: install_dir)

    mock_run = MagicMock(return_value=MagicMock(returncode=42))

    with patch("subprocess.run", mock_run):
        runner = CliRunner()
        result = runner.invoke(setup_mod.setup)

    assert result.exit_code == 42
