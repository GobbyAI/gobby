"""Tests for install front-door preflight and startup helpers."""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path
from typing import Any
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
        install_dir=Path("/repo/src/gobby/install"),
        embedding_url=None,
        embedding_provider=None,
        managed_services=True,
    )

    assert any("Docker daemon" in error for error in errors)
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
        install_dir=Path("/repo/src/gobby/install"),
        embedding_url=None,
        embedding_provider=None,
    )

    assert any("Git is missing" in error for error in errors)
    assert warnings == []


def test_targeted_preflight_does_not_probe_daemon_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port_available = MagicMock(return_value=False)
    monkeypatch.setattr(daemon_module, "_port_available", port_available)
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

    errors, warnings = daemon_module._run_install_preflight(
        is_full_install=False,
        install_dir=Path("/wheel/gobby/install"),
        embedding_url=None,
        embedding_provider=None,
    )

    assert errors == []
    assert warnings == []
    port_available.assert_not_called()


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
        install_dir=Path("/installed/gobby"),
        embedding_url=None,
        embedding_provider=None,
    )

    assert errors == ["Native Windows is unsupported. Install and run Gobby inside WSL 2."]


def test_required_stack_installs_all_services_and_applies_restart_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    postgres = MagicMock(return_value={"success": True})
    qdrant = MagicMock(return_value={"success": True, "qdrant_url": "http://localhost:6333"})
    falkordb = MagicMock(
        return_value={
            "success": True,
            "url": "redis://localhost:6379",
            "browser_url": "http://localhost:3000",
            "password_source": "provided",
        }
    )
    restart = MagicMock(return_value={"success": True})
    monkeypatch.setattr(install_module, "install_postgres", postgres)
    monkeypatch.setattr(install_module, "install_qdrant", qdrant)
    monkeypatch.setattr(install_module, "install_falkordb", falkordb)
    monkeypatch.setattr(install_module, "apply_managed_service_restart_policy", restart)
    results: dict[str, dict[str, Any]] = {}

    install_module._install_required_stack(
        results,
        falkordb_password="secret",
        container_restarts=True,
    )

    assert set(results) == {"postgres", "qdrant", "falkordb", "container-restarts"}
    assert results["postgres"] == {"success": True}
    assert results["qdrant"]["qdrant_url"] == "http://localhost:6333"
    assert results["falkordb"]["password_source"] == "provided"
    assert results["container-restarts"] == {"success": True}
    postgres.assert_called_once_with()
    qdrant.assert_called_once_with()
    falkordb.assert_called_once_with(password="secret")
    restart.assert_called_once_with(enabled=True)


def test_required_stack_reports_dependent_failures_after_postgres_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        install_module,
        "install_postgres",
        lambda: {"success": False, "error": "postgres failed"},
    )
    qdrant = MagicMock()
    falkordb = MagicMock()
    restart = MagicMock()
    monkeypatch.setattr(install_module, "install_qdrant", qdrant)
    monkeypatch.setattr(install_module, "install_falkordb", falkordb)
    monkeypatch.setattr(install_module, "apply_managed_service_restart_policy", restart)
    results: dict[str, dict[str, Any]] = {}

    install_module._install_required_stack(
        results,
        falkordb_password=None,
        container_restarts=True,
    )

    assert all(not result["success"] for result in results.values())
    assert "PostgreSQL" in results["qdrant"]["error"]
    assert "PostgreSQL" in results["falkordb"]["error"]
    qdrant.assert_not_called()
    falkordb.assert_not_called()
    restart.assert_not_called()


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


def test_all_with_only_repository_hooks_still_owns_required_stack(
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
    monkeypatch.setattr(install_module, "peek_install_bootstrap", lambda: {})

    result = CliRunner().invoke(
        install_module.install,
        ["--all", "--no-interactive", "-C", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "stop after classification" in result.output
    preflight.assert_called_once_with(
        is_full_install=True,
        install_dir=tmp_path,
        embedding_url=None,
        embedding_provider=None,
        managed_services=True,
        datastore_mode="local",
        database_url=None,
        hub_daemon_url=None,
    )


def test_default_install_completes_required_stack_without_detected_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for detector in (
        "_is_claude_code_installed",
        "_is_grok_cli_installed",
        "_is_agy_cli_installed",
        "_is_qwen_cli_installed",
        "_is_codex_cli_installed",
        "_is_droid_cli_installed",
    ):
        monkeypatch.setattr(install_module, detector, lambda: False)
    monkeypatch.setattr(install_module, "get_install_dir", lambda: tmp_path)
    monkeypatch.setattr(install_module, "_run_install_preflight", lambda **_kwargs: ([], []))
    monkeypatch.setattr(install_module, "peek_install_bootstrap", lambda: {})
    monkeypatch.setattr(
        install_module,
        "resolve_install_files_home",
        lambda *_a, **_k: tmp_path / "files",
    )
    monkeypatch.setattr(install_module, "acquire_install_maintenance", MagicMock())
    monkeypatch.setattr(
        install_module,
        "publish_install_files_home",
        lambda *_a, **_k: {"created": False, "path": str(tmp_path / "bootstrap.yaml")},
    )
    monkeypatch.setattr(
        install_module,
        "ensure_personal_project_identity",
        lambda: tmp_path / "files/_personal/.gobby/project.json",
    )
    monkeypatch.setattr(
        install_module,
        "ensure_install_identity",
        lambda *_a, **_k: MagicMock(email="owner@example.com"),
    )
    monkeypatch.setattr(
        install_module,
        "_ensure_daemon_config",
        lambda: {"created": False, "path": str(tmp_path / "bootstrap.yaml")},
    )
    required_stack = MagicMock(
        side_effect=lambda results, **_kwargs: results.update(
            {
                "postgres": {"success": True},
                "qdrant": {"success": True},
                "falkordb": {"success": True},
                "container-restarts": {"success": True},
            }
        )
    )
    monkeypatch.setattr(install_module, "_install_required_stack", required_stack)
    monkeypatch.setattr(install_module, "run_daemon_setup", MagicMock())
    monkeypatch.setattr(
        install_module, "_should_initialize_project", lambda *_args, **_kwargs: False
    )
    runtime = MagicMock()
    runtime.require_database.return_value = MagicMock()
    monkeypatch.setattr(install_module, "get_cli_runtime", lambda: runtime)
    monkeypatch.setattr(
        importlib.import_module("gobby.storage.hub.runtime"),
        "runtime_hub_database",
        MagicMock(side_effect=RuntimeError("test hub unavailable")),
    )
    monkeypatch.setattr(install_module, "_configure_secret_kek_posture", lambda *_a, **_k: None)
    monkeypatch.setattr(install_module, "_provision_local_api_token", lambda *_args: None)
    monkeypatch.setattr(
        install_module,
        "prepare_install_state",
        lambda *_args: install_module.empty_install_state(),
    )
    monkeypatch.setattr(install_module, "should_configure_section", lambda *_a, **_k: False)
    summary = MagicMock(return_value=True)
    monkeypatch.setattr(install_module, "_echo_install_summary", summary)
    monkeypatch.setattr(install_module, "_echo_migration_notice", lambda *_args: None)
    start_daemon = MagicMock()
    monkeypatch.setattr(install_module, "_maybe_start_daemon_after_install", start_daemon)

    result = CliRunner().invoke(
        install_module.install,
        ["--no-interactive", "-C", str(tmp_path)],
        catch_exceptions=False,
    )

    assert result.exit_code == 0
    assert "No supported AI coding CLIs detected; CLI hooks will be skipped." in result.output
    required_stack.assert_called_once()
    summary.assert_called_once()
    start_daemon.assert_called_once()
    assert start_daemon.call_args.kwargs["no_interactive"] is True


def test_install_fails_when_personal_identity_cannot_be_written(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(install_module, "get_install_dir", lambda: tmp_path)
    monkeypatch.setattr(install_module, "_run_install_preflight", lambda **_kwargs: ([], []))
    monkeypatch.setattr(install_module, "peek_install_bootstrap", lambda: {})
    monkeypatch.setattr(
        install_module,
        "resolve_install_files_home",
        lambda *_a, **_k: tmp_path / "files",
    )
    monkeypatch.setattr(install_module, "acquire_install_maintenance", MagicMock())
    publish = MagicMock(return_value={"created": True, "path": str(tmp_path / "bootstrap.yaml")})
    monkeypatch.setattr(install_module, "publish_install_files_home", publish)
    monkeypatch.setattr(
        install_module,
        "ensure_personal_project_identity",
        MagicMock(side_effect=PermissionError("read-only marker")),
    )
    ensure_config = MagicMock()
    monkeypatch.setattr(install_module, "_ensure_daemon_config", ensure_config)

    result = CliRunner().invoke(
        install_module.install,
        ["--codex", "--no-interactive", "-C", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "Failed to establish personal project identity: read-only marker" in result.output
    publish.assert_called_once()
    ensure_config.assert_not_called()


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
    monkeypatch.setattr(install_module, "peek_install_bootstrap", lambda: {})

    result = CliRunner().invoke(
        install_module.install,
        ["--config-only", "--no-interactive", "--path", str(tmp_path)],
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
    monkeypatch.setattr(install_module, "peek_install_bootstrap", lambda: {})
    monkeypatch.setattr(
        install_module,
        "_ensure_daemon_config",
        lambda: {"created": False, "path": str(config_path)},
    )
    monkeypatch.setattr(
        install_module,
        "resolve_install_files_home",
        lambda *_a, **_k: tmp_path / "files",
    )
    monkeypatch.setattr(install_module, "acquire_install_maintenance", MagicMock())
    monkeypatch.setattr(
        install_module,
        "publish_install_files_home",
        lambda *_a, **_k: {"created": False, "path": str(config_path)},
    )
    monkeypatch.setattr(
        install_module,
        "ensure_personal_project_identity",
        lambda: tmp_path / "files/_personal/.gobby/project.json",
    )
    monkeypatch.setattr(
        install_module,
        "ensure_install_identity",
        lambda *_a, **_k: MagicMock(email="owner@example.com"),
    )
    required_stack = MagicMock(
        side_effect=lambda results, **_kwargs: results.update(
            {
                "postgres": {"success": True},
                "qdrant": {"success": True},
                "falkordb": {"success": True},
                "container-restarts": {"success": True},
            }
        )
    )
    monkeypatch.setattr(install_module, "_install_required_stack", required_stack)
    run_setup = MagicMock()
    monkeypatch.setattr(install_module, "run_daemon_setup", run_setup)
    runtime = MagicMock()
    runtime.require_database.return_value = MagicMock()
    monkeypatch.setattr(install_module, "get_cli_runtime", lambda: runtime)

    result = CliRunner().invoke(
        install_module.install,
        ["--config-only", "--no-interactive", "--path", str(tmp_path)],
    )

    assert not (tmp_path / ".git").exists()
    assert result.exit_code == 0
    assert "Configuration and required infrastructure complete." in result.output
    required_stack.assert_called_once()
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


def test_files_home_must_be_existing_absolute_directory(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing-files"
    result = CliRunner().invoke(
        install_module.install,
        ["--config-only", "--no-interactive", "--files-home", str(missing)],
    )
    assert result.exit_code != 0
    assert "does not exist" in result.output or "files-home" in result.output.lower()


def test_files_home_refuses_root_and_reserved_overlap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from gobby.config.bootstrap_io import bootstrap_path
    from gobby.paths import get_gobby_home

    identity = MagicMock(side_effect=AssertionError("identity must not run"))
    monkeypatch.setattr(install_module, "ensure_personal_project_identity", identity)
    monkeypatch.setattr(install_module, "_run_install_preflight", lambda **_kwargs: ([], []))
    monkeypatch.setattr(install_module, "peek_install_bootstrap", lambda: {})
    monkeypatch.setattr(install_module, "get_install_dir", lambda: tmp_path)
    leftover = bootstrap_path()
    leftover.unlink(missing_ok=True)
    personal = get_gobby_home() / "personal"
    if personal.exists():
        import shutil

        shutil.rmtree(personal)

    result = CliRunner().invoke(
        install_module.install,
        ["--config-only", "--no-interactive", "--files-home", "/"],
    )
    assert result.exit_code != 0
    identity.assert_not_called()
    assert not bootstrap_path().exists()
    assert not (get_gobby_home() / "personal").exists()

    reserved = get_gobby_home() / "personal"
    reserved.mkdir(parents=True, exist_ok=True)
    result = CliRunner().invoke(
        install_module.install,
        ["--config-only", "--no-interactive", "--files-home", str(reserved)],
    )
    assert result.exit_code != 0
    identity.assert_not_called()
