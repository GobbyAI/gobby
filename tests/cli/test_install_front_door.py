"""Tests for install front-door preflight and startup helpers."""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

install_module = importlib.import_module("gobby.cli.install")

pytestmark = pytest.mark.unit


def test_source_checkout_install_requires_repo_marker(tmp_path: Path) -> None:
    false_positive_install = tmp_path / "site-packages" / "src" / "gobby" / "install"
    false_positive_install.mkdir(parents=True)

    assert install_module._is_source_checkout_install(false_positive_install) is False

    checkout = tmp_path / "checkout"
    source_install = checkout / "src" / "gobby" / "install"
    source_install.mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("[project]\nname = 'gobby'\n", encoding="utf-8")

    assert install_module._is_source_checkout_install(source_install) is True


def test_docker_daemon_available_handles_subprocess_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_subprocess_error(*_args: object, **_kwargs: object) -> None:
        raise subprocess.SubprocessError("docker failed")

    monkeypatch.setattr(install_module.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(install_module.subprocess, "run", raise_subprocess_error)

    assert install_module._docker_daemon_available() is False


def test_full_preflight_requires_docker_cli_tmux_and_source_uv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(install_module, "_docker_daemon_available", lambda: False)
    monkeypatch.setattr(install_module, "_is_source_checkout_install", lambda _path: True)
    monkeypatch.setattr(install_module, "_port_available", lambda _port: True)
    monkeypatch.setattr(install_module.shutil, "which", lambda _name: None)

    errors, warnings = install_module._run_install_preflight(
        is_full_install=True,
        detected_clis=[],
        install_dir=Path("/repo/src/gobby/install"),
        embedding_url=None,
        embedding_provider=None,
    )

    assert any("Docker daemon" in error for error in errors)
    assert any("At least one supported coding CLI" in error for error in errors)
    assert any("tmux" in error for error in errors)
    assert any("uv" in error for error in errors)
    assert any("embedding provider" in warning for warning in warnings)
    assert any("git was not found" in warning for warning in warnings)


def test_targeted_preflight_skips_full_install_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(install_module, "_docker_daemon_available", lambda: False)
    monkeypatch.setattr(install_module, "_port_available", lambda _port: True)
    monkeypatch.setattr(install_module.shutil, "which", lambda _name: None)

    errors, warnings = install_module._run_install_preflight(
        is_full_install=False,
        detected_clis=[],
        install_dir=Path("/repo/src/gobby/install"),
        embedding_url=None,
        embedding_provider=None,
    )

    assert errors == []
    assert any("git was not found" in warning for warning in warnings)


def test_full_install_exits_before_provisioning_without_docker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_postgres = MagicMock()
    monkeypatch.setattr(install_module, "_docker_daemon_available", lambda: False)
    monkeypatch.setattr(install_module, "get_install_dir", lambda: tmp_path)
    monkeypatch.setattr(install_module, "_is_claude_code_installed", lambda: True)
    for detector in (
        "_is_grok_cli_installed",
        "_is_agy_cli_installed",
        "_is_qwen_cli_installed",
        "_is_codex_cli_installed",
        "_is_droid_cli_installed",
    ):
        monkeypatch.setattr(install_module, detector, lambda: False)
    monkeypatch.setattr(install_module.shutil, "which", lambda _name: "/usr/bin/tool")
    monkeypatch.setattr(install_module, "install_postgres", install_postgres)

    result = CliRunner().invoke(install_module.install, ["--all"])

    assert result.exit_code == 1
    assert "Docker daemon is required for full install" in result.output
    install_postgres.assert_not_called()


def test_should_initialize_project_auto_yes_only_for_no_interactive(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()

    assert install_module._should_initialize_project(tmp_path, no_interactive=True) is True
    assert install_module._should_initialize_project(tmp_path, no_interactive=False) is False


def test_maybe_start_daemon_skips_in_no_interactive(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    popen = MagicMock()
    monkeypatch.setattr(install_module.subprocess, "Popen", popen)
    monkeypatch.setattr(install_module, "_daemon_url", lambda: "http://localhost:60887/")

    install_module._maybe_start_daemon_after_install(no_interactive=True)

    output = capsys.readouterr().out
    assert "Gobby UI: http://localhost:60887/" in output
    assert "/gobby intro" in output
    popen.assert_not_called()


def test_maybe_start_daemon_starts_and_opens_browser(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    process = MagicMock()
    process.poll.return_value = None
    popen = MagicMock(return_value=process)
    open_browser = MagicMock(return_value=True)
    monkeypatch.setattr(install_module, "_ci_environment", lambda: False)
    monkeypatch.setattr(install_module, "_headless_or_remote", lambda: False)
    daemon_running = iter([False, True])
    monkeypatch.setattr(install_module, "_daemon_already_running", lambda: next(daemon_running))
    monkeypatch.setattr(install_module, "_daemon_url", lambda: "http://localhost:60887/")
    monkeypatch.setattr(install_module.subprocess, "Popen", popen)
    monkeypatch.setattr(install_module.webbrowser, "open", open_browser)

    install_module._maybe_start_daemon_after_install(no_interactive=False)

    output = capsys.readouterr().out
    assert "Starting Gobby daemon" in output
    assert "Gobby daemon started: http://localhost:60887/" in output
    assert "/gobby intro" in output
    popen.assert_called_once()
    open_browser.assert_called_once_with("http://localhost:60887/")
