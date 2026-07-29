"""Tests for install front-door preflight and startup helpers."""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest
from click.testing import CliRunner

from gobby.utils.dependency_requirements import (
    DependencyReport,
    DependencyState,
    DependencyStatus,
)

daemon_module = importlib.import_module("gobby.cli._install_daemon")
install_module = importlib.import_module("gobby.cli.install")

pytestmark = pytest.mark.unit


def _dependency(
    state: DependencyState,
    *,
    name: str,
    minimum: str,
) -> DependencyStatus:
    return DependencyStatus(
        state=state,
        installed_version=None,
        minimum_version=minimum,
        expected_version=None,
        path=None,
        error=f"{name} is missing; detected version unavailable, requires >={minimum}. Install it.",
    )


def test_port_available_sets_reuseaddr_before_timeout_and_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sock = MagicMock()
    sock.__enter__.return_value = sock
    socket_factory = MagicMock(return_value=sock)
    monkeypatch.setattr(daemon_module.socket, "socket", socket_factory)

    assert daemon_module._port_available(60887) is True
    assert sock.method_calls[:3] == [
        call.setsockopt(daemon_module.socket.SOL_SOCKET, daemon_module.socket.SO_REUSEADDR, 1),
        call.settimeout(0.2),
        call.bind(("0.0.0.0", 60887)),
    ]


def test_source_checkout_install_requires_repo_marker(tmp_path: Path) -> None:
    false_positive_install = tmp_path / "site-packages" / "src" / "gobby" / "install"
    false_positive_install.mkdir(parents=True)

    assert daemon_module._is_source_checkout_install(false_positive_install) is False

    checkout = tmp_path / "checkout"
    source_install = checkout / "src" / "gobby" / "install"
    source_install.mkdir(parents=True)
    (checkout / "pyproject.toml").write_text("[project]\nname = 'gobby'\n", encoding="utf-8")

    assert daemon_module._is_source_checkout_install(source_install) is True


def test_docker_daemon_available_handles_subprocess_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_subprocess_error(*_args: object, **_kwargs: object) -> None:
        raise subprocess.SubprocessError("docker failed")

    monkeypatch.setattr(daemon_module.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(daemon_module.subprocess, "run", raise_subprocess_error)

    assert daemon_module._docker_daemon_available() is False


def test_full_preflight_requires_docker_cli_tmux_and_source_uv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon_module, "_docker_daemon_available", lambda: False)
    monkeypatch.setattr(daemon_module, "_is_source_checkout_install", lambda _path: True)
    monkeypatch.setattr(daemon_module, "_port_available", lambda _port: True)
    monkeypatch.setattr(daemon_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        daemon_module,
        "collect_dependency_report",
        lambda **_kwargs: DependencyReport(
            runtime={},
            required={
                "tmux": _dependency("missing", name="tmux", minimum="3.2"),
                "git": _dependency("missing", name="Git", minimum="2.38.0"),
                "node": _dependency("missing", name="Node.js", minimum="20.11.0"),
                "docker_compose": _dependency("missing", name="Docker Compose", minimum="2.7.0"),
            },
            optional={},
            services={},
        ),
    )

    errors, warnings = daemon_module._run_install_preflight(
        is_full_install=True,
        detected_clis=[],
        install_dir=Path("/repo/src/gobby/install"),
        embedding_url=None,
        embedding_provider=None,
        managed_services=True,
    )

    assert any("Docker daemon" in error for error in errors)
    assert any("At least one supported coding CLI" in error for error in errors)
    assert any("tmux" in error for error in errors)
    assert any("Git" in error for error in errors)
    assert any("Node.js" in error for error in errors)
    assert any("Docker Compose" in error for error in errors)
    assert any("uv" in error for error in errors)
    assert any("embedding provider" in warning for warning in warnings)
    assert not warnings or all("git" not in warning for warning in warnings)


def test_targeted_preflight_still_requires_user_managed_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(daemon_module, "_docker_daemon_available", lambda: False)
    monkeypatch.setattr(daemon_module, "_port_available", lambda _port: True)
    monkeypatch.setattr(daemon_module.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        daemon_module,
        "collect_dependency_report",
        lambda **_kwargs: DependencyReport(
            runtime={},
            required={"git": _dependency("missing", name="Git", minimum="2.38.0")},
            optional={},
            services={},
        ),
    )

    errors, warnings = daemon_module._run_install_preflight(
        is_full_install=False,
        detected_clis=[],
        install_dir=Path("/repo/src/gobby/install"),
        embedding_url=None,
        embedding_provider=None,
    )

    assert any("Git is missing" in error for error in errors)
    assert warnings == []


def test_install_preflight_rejects_native_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        daemon_module,
        "unsupported_platform_error",
        lambda: "Native Windows is unsupported. Install and run Gobby inside WSL 2.",
    )
    monkeypatch.setattr(
        daemon_module,
        "collect_dependency_report",
        lambda **_kwargs: DependencyReport(
            runtime={},
            required={},
            optional={},
            services={},
        ),
    )
    monkeypatch.setattr(daemon_module, "_port_available", lambda _port: True)

    errors, _warnings = daemon_module._run_install_preflight(
        is_full_install=False,
        detected_clis=[],
        install_dir=Path("/installed/gobby"),
        embedding_url=None,
        embedding_provider=None,
    )

    assert errors == ["Native Windows is unsupported. Install and run Gobby inside WSL 2."]


def test_full_install_exits_before_provisioning_without_docker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install_postgres = MagicMock()
    monkeypatch.setattr(daemon_module, "_docker_daemon_available", lambda: False)
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
    monkeypatch.setattr(daemon_module.shutil, "which", lambda _name: "/usr/bin/tool")
    monkeypatch.setattr(install_module, "install_postgres", install_postgres)

    result = CliRunner().invoke(install_module.install, ["--all"])

    assert result.exit_code == 1
    assert "Docker daemon is required for full install" in result.output
    install_postgres.assert_not_called()


def test_all_with_only_repository_hooks_is_not_a_full_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(install_module, "get_install_dir", lambda: tmp_path)
    for detector in (
        "_is_claude_code_installed",
        "_is_grok_cli_installed",
        "_is_agy_cli_installed",
        "_is_qwen_cli_installed",
        "_is_codex_cli_installed",
        "_is_droid_cli_installed",
    ):
        monkeypatch.setattr(install_module, detector, lambda: False)

    preflight = MagicMock(return_value=(["stop after classification"], []))
    monkeypatch.setattr(install_module, "_run_install_preflight", preflight)

    result = CliRunner().invoke(
        install_module.install,
        ["--all", "--no-interactive", "-C", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "stop after classification" in result.output
    preflight.assert_called_once_with(
        is_full_install=False,
        detected_clis=[],
        install_dir=tmp_path,
        embedding_url=None,
        embedding_provider=None,
        managed_services=False,
    )


def test_config_only_requires_git_before_provisioning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ensure_config = MagicMock()
    monkeypatch.setattr(install_module, "get_install_dir", lambda: tmp_path)
    monkeypatch.setattr(
        install_module,
        "_run_install_preflight",
        lambda **_kwargs: (
            [
                "Git is missing; detected version unavailable, requires >=2.38.0. "
                "Install Git 2.38.0 or newer and retry."
            ],
            [],
        ),
    )
    monkeypatch.setattr(install_module, "_ensure_daemon_config", ensure_config)

    result = CliRunner().invoke(
        install_module.install,
        ["--config-only", "--path", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "Git is missing" in result.output
    ensure_config.assert_not_called()


def test_config_only_allows_non_repository_personal_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "bootstrap.yaml"
    monkeypatch.setattr(install_module, "get_install_dir", lambda: tmp_path)
    monkeypatch.setattr(
        install_module,
        "_run_install_preflight",
        lambda **_kwargs: ([], []),
    )
    monkeypatch.setattr(
        install_module,
        "_ensure_daemon_config",
        lambda: {"created": False, "path": str(config_path)},
    )
    run_setup = MagicMock()
    monkeypatch.setattr(install_module, "run_daemon_setup", run_setup)

    result = CliRunner().invoke(
        install_module.install,
        ["--config-only", "--path", str(tmp_path)],
    )

    assert not (tmp_path / ".git").exists()
    assert result.exit_code == 0
    assert "Configuration and database initialization complete." in result.output
    run_setup.assert_called_once()


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
