"""Tests for cli/install.py — targeting uncovered lines."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from click.testing import CliRunner

from gobby.cli._install_prompts import _echo_uninstall_details
from gobby.cli._install_state import (
    EmbeddingInstallState,
    InstallSectionState,
    InstallState,
    VoiceInstallState,
    empty_install_state,
)
from gobby.cli.install import (
    _echo_install_details,
    _is_claude_code_installed,
    _is_codex_cli_installed,
    install,
)
from gobby.cli.uninstall import uninstall
from gobby.ui_exposure import UiExposeError

pytestmark = pytest.mark.unit


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    import importlib

    runtime = MagicMock()
    runtime.require_database.return_value = MagicMock()
    runtime.require_config.return_value.hooks.provider_timeout = 120
    install_module = importlib.import_module("gobby.cli.install")
    monkeypatch.setattr(install_module, "get_cli_runtime", lambda: runtime)
    monkeypatch.setattr(
        install_module,
        "ensure_install_identity",
        MagicMock(return_value=MagicMock(email="owner@example.com")),
    )
    monkeypatch.setattr(install_module, "SecretStore", MagicMock())
    monkeypatch.setattr(install_module, "ConfigStore", MagicMock())
    monkeypatch.setattr(install_module, "AuthStore", MagicMock())
    monkeypatch.setattr(install_module, "_provision_local_api_token", MagicMock())
    monkeypatch.setattr(install_module, "_provision_gdaemon_for_services", MagicMock())
    monkeypatch.setattr(install_module, "_run_install_preflight", lambda **_kwargs: ([], []))
    monkeypatch.setattr(install_module, "_maybe_start_daemon_after_install", MagicMock())
    monkeypatch.setattr(
        install_module,
        "resolve_installer_ui_exposure",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "gobby.cli.installers.ide_config.find_vscode_family_ides_needing_terminal_integration",
        lambda: [],
    )
    return CliRunner()


def _configured_install_state() -> InstallState:
    return InstallState(
        embedding=EmbeddingInstallState(
            configured=True,
            summary="disabled",
            provider="none",
            dim=0,
        ),
        voice=VoiceInstallState(configured=True, summary="disabled", enabled=False),
        qdrant=InstallSectionState(configured=True, summary="localhost:6333"),
        falkordb=InstallSectionState(configured=True, summary="localhost:16379"),
        has_existing_values=True,
    )


def _record_qdrant_success(
    _installer: object,
    results: dict[str, dict[str, Any]],
) -> None:
    results["qdrant"] = {"success": True}


def _record_falkordb_success(
    _installer: object,
    _password: str | None,
    results: dict[str, dict[str, Any]],
) -> None:
    results["falkordb"] = {"success": True}


# ---------------------------------------------------------------------------
# _echo_install_details / _echo_uninstall_details
# ---------------------------------------------------------------------------
class TestEchoHelpers:
    def test_echo_install_details_basic(self) -> None:
        result: dict[str, Any] = {
            "hooks_installed": ["hook1", "hook2"],
        }
        runner = CliRunner()
        with runner.isolated_filesystem():
            _echo_install_details(result)
            assert result["hooks_installed"] == ["hook1", "hook2"]

    def test_echo_install_details_full(self) -> None:
        result: dict[str, Any] = {
            "hooks_installed": ["hook1"],
            "workflows_installed": ["wf1"],
            "agents_installed": ["agent1"],
            "commands_installed": ["cmd1"],
            "plugins_installed": ["plugin1"],
            "mcp_configured": True,
        }
        _echo_install_details(result, mcp_config_path="~/.claude.json", config_path="~/.config")
        assert result["mcp_configured"] is True
        assert result["plugins_installed"] == ["plugin1"]

    def test_echo_install_details_mcp_already(self) -> None:
        result: dict[str, Any] = {
            "hooks_installed": [],
            "mcp_already_configured": True,
        }
        _echo_install_details(result, mcp_config_path="~/.claude.json")
        assert result["mcp_already_configured"] is True

    def test_echo_uninstall_details_with_hooks(self) -> None:
        result: dict[str, Any] = {
            "hooks_removed": ["hook1", "hook2"],
            "files_removed": ["file1"],
        }
        _echo_uninstall_details(result)
        assert result["hooks_removed"] == ["hook1", "hook2"]
        assert result["files_removed"] == ["file1"]

    def test_echo_uninstall_details_empty(self) -> None:
        result: dict[str, Any] = {
            "hooks_removed": [],
            "files_removed": [],
        }
        _echo_uninstall_details(result)
        assert result == {"hooks_removed": [], "files_removed": []}


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------
class TestDetectionHelpers:
    @patch("gobby.cli._detectors.shutil.which", return_value="/usr/bin/claude")
    def test_claude_installed(self, _mock_which: MagicMock) -> None:
        assert _is_claude_code_installed() is True

    @patch("gobby.cli._detectors.shutil.which", return_value=None)
    def test_claude_not_installed(self, _mock_which: MagicMock) -> None:
        assert _is_claude_code_installed() is False

    @patch("gobby.cli._detectors.shutil.which", return_value="/usr/bin/codex")
    def test_codex_installed(self, _mock_which: MagicMock) -> None:
        assert _is_codex_cli_installed() is True

    @patch("gobby.cli._detectors.shutil.which", return_value=None)
    def test_codex_not_installed(self, _mock_which: MagicMock) -> None:
        assert _is_codex_cli_installed() is False


# ---------------------------------------------------------------------------
# install command — --claude only
# ---------------------------------------------------------------------------
class TestInstallCommand:
    @pytest.fixture(autouse=True)
    def _mock_docker_services(self) -> Any:
        """Mock the managed stack — tests here focus on CLI-specific hooks."""
        postgres_result = {"success": True, "database_url": "postgresql://localhost/gobby"}
        qdrant_result = {"success": True, "qdrant_url": "http://localhost:6333"}
        falkordb_result = {
            "success": True,
            "url": "redis://localhost:6379",
            "browser_url": "http://localhost:13000",
            "password_source": "reused",
            "password": None,
        }
        claim = MagicMock()
        with (
            patch("gobby.cli._install_daemon._docker_daemon_available", return_value=True),
            patch("gobby.cli.install.install_postgres", return_value=postgres_result),
            patch("gobby.cli.install.install_qdrant", return_value=qdrant_result),
            patch("gobby.cli.install.install_falkordb", return_value=falkordb_result),
            patch(
                "gobby.cli.install.apply_managed_service_restart_policy",
                return_value={"success": True, "policy": "unless-stopped"},
            ),
            patch("gobby.cli.install._resolve_ide_settings_consent", return_value=False),
            patch(
                "gobby.cli.install.resolve_install_files_home",
                return_value=Path("/tmp/gobby-files-home"),
            ),
            patch("gobby.cli.install.acquire_install_maintenance", return_value=claim),
            patch(
                "gobby.cli.install.publish_install_files_home",
                return_value={"created": False, "path": "/fake/bootstrap.yaml"},
            ),
            patch(
                "gobby.cli.install.ensure_personal_project_identity",
                return_value=Path("/tmp/gobby-files-home/_personal/.gobby/project.json"),
            ),
            patch("gobby.cli.install.peek_install_bootstrap", return_value={}),
            patch(
                "gobby.cli.install.ensure_install_identity",
                return_value=MagicMock(email="owner@example.com"),
            ),
            patch("gobby.cli.install._configure_secret_kek_posture", lambda *_a, **_k: None),
            patch("gobby.cli.install._provision_local_api_token", lambda *_a, **_k: None),
        ):
            yield

    def test_install_config_only_skips_hooks_and_provisions_services(
        self, runner: CliRunner
    ) -> None:
        with (
            patch(
                "gobby.cli.install._ensure_daemon_config",
                return_value={"created": False, "path": "/fake/bootstrap.yaml"},
            ),
            patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install")),
            patch("gobby.cli.install.run_daemon_setup") as mock_setup,
            patch("gobby.cli.install._should_initialize_project") as mock_should_init,
            patch("gobby.cli.install._resolve_ide_settings_consent") as mock_ide_consent,
            patch("gobby.cli.install.install_agy") as mock_agy,
            patch("gobby.cli.install.install_claude") as mock_claude,
            patch("gobby.cli.install.install_codex") as mock_codex,
            patch("gobby.cli.install.install_droid") as mock_droid,
            patch("gobby.cli.install.install_grok") as mock_grok,
            patch("gobby.cli.install.install_qwen") as mock_qwen,
            patch("gobby.cli.install.install_git_hooks") as mock_git_hooks,
            patch("gobby.cli.install._run_embedding_install") as mock_embedding,
            patch("gobby.cli.install._run_voice_install") as mock_voice,
            patch(
                "gobby.cli.install._run_qdrant_install",
                side_effect=_record_qdrant_success,
            ) as mock_qdrant,
            patch(
                "gobby.cli.install._run_falkordb_install",
                side_effect=_record_falkordb_success,
            ) as mock_falkordb,
            patch("gobby.cli.install._maybe_start_daemon_after_install") as mock_start,
        ):
            result = runner.invoke(
                install,
                ["--config-only", "--no-expose-ui"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "Configuration and required infrastructure complete." in result.output
        mock_setup.assert_called_once_with(Path.cwd(), configure_ide_settings=False)
        mock_should_init.assert_not_called()
        mock_ide_consent.assert_not_called()
        mock_qdrant.assert_called_once()
        mock_falkordb.assert_called_once()
        for skipped in (
            mock_agy,
            mock_claude,
            mock_codex,
            mock_droid,
            mock_grok,
            mock_qwen,
            mock_git_hooks,
            mock_embedding,
            mock_voice,
            mock_start,
        ):
            skipped.assert_not_called()

    @patch("gobby.cli.install.run_daemon_setup")
    @patch(
        "gobby.cli.install._ensure_daemon_config", return_value={"created": False, "path": "/fake"}
    )
    @patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.cli.install.install_claude")
    def test_install_claude_only(
        self,
        mock_install_claude: MagicMock,
        _install_dir: MagicMock,
        _config: MagicMock,
        _setup: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_install_claude.return_value = {
            "success": True,
            "hooks_installed": ["PreToolUse", "PostToolUse"],
            "mcp_configured": True,
        }
        result = runner.invoke(install, ["--claude"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Claude Code" in result.output
        assert "successfully" in result.output.lower()

    @patch("gobby.cli.install.run_daemon_setup")
    @patch(
        "gobby.cli.install._ensure_daemon_config", return_value={"created": True, "path": "/fake"}
    )
    @patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.cli.install.install_claude")
    def test_install_claude_failure(
        self,
        mock_install_claude: MagicMock,
        _install_dir: MagicMock,
        _config: MagicMock,
        _setup: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_install_claude.return_value = {
            "success": False,
            "error": "Something went wrong",
            "hooks_installed": [],
        }
        result = runner.invoke(install, ["--claude"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "Something went wrong" in result.output

    @patch("gobby.cli.install.run_daemon_setup")
    @patch(
        "gobby.cli.install._ensure_daemon_config", return_value={"created": False, "path": "/fake"}
    )
    @patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.cli.install.install_git_hooks")
    def test_install_git_hooks(
        self,
        mock_install: MagicMock,
        _install_dir: MagicMock,
        _config: MagicMock,
        _setup: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_install.return_value = {
            "success": True,
            "installed": ["pre-commit", "post-merge"],
            "skipped": [],
            "wiki_setup": {
                "success": True,
                "gitignore_updated": True,
                "worktree_path": "/fake/repo-wiki",
                "branch": "wiki",
                "warnings": [],
                "tracked_files": [],
            },
        }
        with (
            patch(
                "gobby.cli.install.ensure_personal_project_identity",
                return_value=Path("/fake/personal/.gobby/project.json"),
            ) as personal_identity,
            patch("gobby.cli.install._should_initialize_project") as initialize_project,
            patch("gobby.cli.install._install_required_stack") as required_stack,
            patch("gobby.cli.runtime.CliRuntime.require_config") as load_config,
            patch("gobby.cli.install._run_voice_install") as voice_install,
            patch("gobby.cli.install._echo_install_summary") as full_summary,
        ):
            result = runner.invoke(
                install,
                ["--hooks", "--no-expose-ui"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert "pre-commit" in result.output
        assert "Wiki branch setup:" in result.output
        assert "/fake/repo-wiki" in result.output
        assert "Git hook maintenance complete." in result.output
        mock_install.assert_called_once()
        personal_identity.assert_called_once_with()
        initialize_project.assert_not_called()
        required_stack.assert_not_called()
        load_config.assert_not_called()
        voice_install.assert_not_called()
        full_summary.assert_not_called()
        _config.assert_not_called()
        _setup.assert_not_called()

    def test_expose_ui_failure_warns_and_install_continues(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import importlib

        install_module = importlib.import_module("gobby.cli.install")
        apply_exposure = MagicMock(side_effect=UiExposeError("sentinel"))
        monkeypatch.setattr(
            install_module,
            "resolve_installer_ui_exposure",
            lambda *_args, **_kwargs: True,
        )
        monkeypatch.setattr(install_module, "apply_installer_ui_exposure", apply_exposure)
        with (
            patch(
                "gobby.cli.install.ensure_personal_project_identity",
                return_value=Path("/fake/personal/.gobby/project.json"),
            ),
            patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install")),
            patch(
                "gobby.cli.install._ensure_daemon_config",
                return_value={"created": False, "path": "/fake/bootstrap.yaml"},
            ),
            patch("gobby.cli.install.run_daemon_setup") as daemon_setup,
            patch("gobby.cli.install._should_initialize_project", return_value=False),
            patch("gobby.cli.install._configure_secret_kek_posture"),
            patch("gobby.cli.install._provision_local_api_token"),
            patch("gobby.cli.install._run_git_hooks_install") as git_hooks,
            patch("gobby.cli.install._run_voice_install"),
        ):
            result = runner.invoke(
                install,
                ["--hooks", "--expose-ui", "--no-interactive"],
            )

        assert result.exit_code == 0
        assert "Warning: failed to expose the web UI: sentinel" in result.output
        assert "gobby ui expose" in result.output
        daemon_setup.assert_called_once()
        apply_exposure.assert_called_once_with(True, 60887)
        git_hooks.assert_called_once()

    def test_install_claude_targeted_skips_embedding_and_services(
        self,
        runner: CliRunner,
    ) -> None:
        """Targeted CLI installs do not configure embeddings or Docker services."""
        claude_result = {
            "success": True,
            "hooks_installed": ["PreToolUse"],
            "mcp_configured": True,
        }
        with (
            patch("gobby.cli.install.run_daemon_setup"),
            patch(
                "gobby.cli.install._ensure_daemon_config",
                return_value={"created": False, "path": "/fake"},
            ),
            patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install")),
            patch("gobby.cli.install.install_claude", return_value=claude_result),
            patch("gobby.cli.install._run_embedding_install") as mock_embedding,
            patch(
                "gobby.cli.install._run_qdrant_install",
                side_effect=_record_qdrant_success,
            ) as mock_qdrant,
            patch(
                "gobby.cli.install._run_falkordb_install",
                side_effect=_record_falkordb_success,
            ) as mock_falkordb,
        ):
            result = runner.invoke(install, ["--claude"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "Embedding Provider" not in result.output
        mock_embedding.assert_not_called()
        mock_qdrant.assert_not_called()
        mock_falkordb.assert_not_called()

    @pytest.mark.parametrize(
        ("install_args", "restart_enabled"),
        [([], True), (["--no-container-restarts"], False)],
    )
    def test_install_default_runs_embedding_and_services(
        self,
        runner: CliRunner,
        install_args: list[str],
        restart_enabled: bool,
    ) -> None:
        """Default install configures embeddings and the required managed stack."""
        claude_result = {
            "success": True,
            "hooks_installed": ["PreToolUse"],
            "mcp_configured": True,
        }
        with (
            patch("gobby.cli.install.run_daemon_setup"),
            patch(
                "gobby.cli.install._ensure_daemon_config",
                return_value={"created": False, "path": "/fake"},
            ),
            patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install")),
            patch("gobby.cli.install._is_claude_code_installed", return_value=True),
            patch("gobby.cli.install._is_grok_cli_installed", return_value=False),
            patch("gobby.cli.install._is_agy_cli_installed", return_value=False),
            patch("gobby.cli.install._is_qwen_cli_installed", return_value=False),
            patch("gobby.cli.install._is_codex_cli_installed", return_value=False),
            patch("gobby.cli.install._is_droid_cli_installed", return_value=False),
            patch("gobby.cli.install.install_claude", return_value=claude_result),
            patch("gobby.cli.install.prepare_install_state", return_value=empty_install_state()),
            patch("gobby.cli.install._run_git_hooks_install") as mock_hooks,
            patch(
                "gobby.cli.install._run_embedding_install", return_value="lmstudio"
            ) as mock_embedding,
            patch(
                "gobby.cli.install._run_qdrant_install",
                side_effect=_record_qdrant_success,
            ) as mock_qdrant,
            patch(
                "gobby.cli.install._run_falkordb_install",
                side_effect=_record_falkordb_success,
            ) as mock_falkordb,
            patch(
                "gobby.cli.install.apply_managed_service_restart_policy",
                return_value={"success": True},
            ) as mock_restart_policy,
        ):
            result = runner.invoke(
                install,
                [*install_args, "--no-expose-ui"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert "Gobby Installation" in result.output
        assert (
            "Components to configure: claude, postgres, qdrant, falkordb, git-hooks"
            in result.output
        )
        mock_hooks.assert_called_once()
        mock_embedding.assert_called_once()
        mock_qdrant.assert_called_once()
        mock_falkordb.assert_called_once()
        mock_restart_policy.assert_called_once_with(enabled=restart_enabled)

    def test_install_default_includes_agy_when_detected(
        self,
        runner: CliRunner,
    ) -> None:
        """Default install includes AGY when the agy binary is present."""
        agy_result = {
            "success": True,
            "hooks_installed": ["PreInvocation"],
            "mcp_configured": True,
        }
        with (
            patch("gobby.cli.install.run_daemon_setup"),
            patch(
                "gobby.cli.install._ensure_daemon_config",
                return_value={"created": False, "path": "/fake"},
            ),
            patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install")),
            patch("gobby.cli.install._is_claude_code_installed", return_value=False),
            patch("gobby.cli.install._is_grok_cli_installed", return_value=False),
            patch("gobby.cli.install._is_agy_cli_installed", return_value=True),
            patch("gobby.cli.install._is_qwen_cli_installed", return_value=False),
            patch("gobby.cli.install._is_codex_cli_installed", return_value=False),
            patch("gobby.cli.install._is_droid_cli_installed", return_value=False),
            patch("gobby.cli.install.install_agy", return_value=agy_result) as mock_agy,
            patch("gobby.cli.install.prepare_install_state", return_value=empty_install_state()),
            patch("gobby.cli.install._run_git_hooks_install") as mock_hooks,
            patch("gobby.cli.install._run_embedding_install", return_value="none"),
            patch("gobby.cli.install._maybe_start_daemon_after_install"),
        ):
            result = runner.invoke(install, ["--no-expose-ui"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "Components to configure: agy, postgres, qdrant, falkordb" in result.output
        assert "AGY CLI" in result.output
        mock_agy.assert_called_once()
        mock_hooks.assert_not_called()

    def test_install_rejects_removed_no_ext_services(self, runner: CliRunner) -> None:
        result = runner.invoke(install, ["--all", "--no-ext-services"])

        assert result.exit_code == 2
        assert "No such option '--no-ext-services'" in result.output

    def test_repeat_noninteractive_install_preserves_optional_sections(
        self, runner: CliRunner
    ) -> None:
        """Configured sections perform no setup work when a repeat install keeps them."""
        with (
            patch("gobby.cli.install.run_daemon_setup"),
            patch(
                "gobby.cli.install._ensure_daemon_config",
                return_value={"created": False, "path": "/fake"},
            ),
            patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install")),
            patch.multiple(
                "gobby.cli.install",
                _is_claude_code_installed=MagicMock(return_value=True),
                _is_grok_cli_installed=MagicMock(return_value=False),
                _is_agy_cli_installed=MagicMock(return_value=False),
                _is_qwen_cli_installed=MagicMock(return_value=False),
                _is_codex_cli_installed=MagicMock(return_value=False),
                _is_droid_cli_installed=MagicMock(return_value=False),
            ),
            patch(
                "gobby.cli.install.install_claude",
                return_value={
                    "success": True,
                    "hooks_installed": [],
                    "mcp_configured": True,
                },
            ),
            patch(
                "gobby.cli.install.prepare_install_state",
                return_value=_configured_install_state(),
            ),
            patch("gobby.cli.install._run_git_hooks_install"),
            patch("gobby.cli.install._run_embedding_install") as embedding,
            patch("gobby.cli.install._run_voice_install") as voice,
            patch(
                "gobby.cli.install._run_qdrant_install",
                side_effect=_record_qdrant_success,
            ) as qdrant,
            patch(
                "gobby.cli.install._run_falkordb_install",
                side_effect=_record_falkordb_success,
            ) as falkordb,
            patch("gobby.cli.install._maybe_start_daemon_after_install"),
        ):
            result = runner.invoke(
                install,
                ["--all", "--no-interactive"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert "Change embedding provider/model/endpoint?" not in result.output
        assert "Change voice setting?" not in result.output
        embedding.assert_not_called()
        voice.assert_not_called()
        qdrant.assert_called_once()
        falkordb.assert_called_once()

    def test_repeat_interactive_install_prompts_for_each_optional_section(
        self, runner: CliRunner
    ) -> None:
        with (
            patch("gobby.cli.install.run_daemon_setup"),
            patch(
                "gobby.cli.install._ensure_daemon_config",
                return_value={"created": False, "path": "/fake"},
            ),
            patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install")),
            patch.multiple(
                "gobby.cli.install",
                _is_claude_code_installed=MagicMock(return_value=True),
                _is_grok_cli_installed=MagicMock(return_value=False),
                _is_agy_cli_installed=MagicMock(return_value=False),
                _is_qwen_cli_installed=MagicMock(return_value=False),
                _is_codex_cli_installed=MagicMock(return_value=False),
                _is_droid_cli_installed=MagicMock(return_value=False),
            ),
            patch(
                "gobby.cli.install.install_claude",
                return_value={
                    "success": True,
                    "hooks_installed": [],
                    "mcp_configured": True,
                },
            ),
            patch(
                "gobby.cli.install.prepare_install_state",
                return_value=_configured_install_state(),
            ),
            patch("gobby.cli.install._run_git_hooks_install"),
            patch("gobby.cli.install._run_embedding_install") as embedding,
            patch("gobby.cli.install._run_voice_install") as voice,
            patch(
                "gobby.cli.install._run_qdrant_install",
                side_effect=_record_qdrant_success,
            ) as qdrant,
            patch(
                "gobby.cli.install._run_falkordb_install",
                side_effect=_record_falkordb_success,
            ) as falkordb,
            patch("gobby.cli.install._maybe_start_daemon_after_install"),
        ):
            result = runner.invoke(
                install,
                ["--all"],
                input="n\nn\nn\nn\n",
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        for prompt in ("Change embedding provider/model/endpoint?", "Change voice setting?"):
            assert prompt in result.output
        embedding.assert_not_called()
        voice.assert_not_called()
        qdrant.assert_called_once()
        falkordb.assert_called_once()

    def test_install_help_omits_removed_no_ext_services(self, runner: CliRunner) -> None:
        result = runner.invoke(install, ["--help"])

        assert result.exit_code == 0
        assert "--no-ext-services" not in result.output

    def test_install_all_no_clis_detected(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        with (
            patch("gobby.cli.install.run_daemon_setup"),
            patch(
                "gobby.cli.install._ensure_daemon_config",
                return_value={"created": False, "path": "/fake"},
            ),
            patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/src/install")),
            patch("gobby.cli.install._is_claude_code_installed", return_value=False),
            patch("gobby.cli.install._is_grok_cli_installed", return_value=False),
            patch("gobby.cli.install._is_agy_cli_installed", return_value=False),
            patch("gobby.cli.install._is_qwen_cli_installed", return_value=False),
            patch("gobby.cli.install._is_codex_cli_installed", return_value=False),
            patch("gobby.cli.install._is_droid_cli_installed", return_value=False),
            patch("gobby.cli.install.prepare_install_state", return_value=empty_install_state()),
        ):
            result = runner.invoke(
                install,
                ["-C", str(tmp_path), "--no-expose-ui"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, result.output
        assert "No supported AI coding CLIs detected; CLI hooks will be skipped." in result.output

    @patch("gobby.cli.install.run_daemon_setup")
    @patch(
        "gobby.cli.install._ensure_daemon_config", return_value={"created": False, "path": "/fake"}
    )
    @patch("gobby.cli.install.get_install_dir", return_value=Path("/fake/install"))
    @patch("gobby.cli.install._is_codex_cli_installed", return_value=True)
    @patch("gobby.cli.install.install_codex")
    def test_install_codex_success(
        self,
        mock_install: MagicMock,
        _codex_installed: MagicMock,
        _install_dir: MagicMock,
        _config: MagicMock,
        _setup: MagicMock,
        runner: CliRunner,
    ) -> None:
        mock_install.return_value = {
            "success": True,
            "hooks_installed": [],
            "files_installed": ["/path/to/file"],
            "config_updated": True,
            "workflows_installed": ["wf1"],
            "commands_installed": ["cmd1"],
            "plugins_installed": ["plugin1"],
            "mcp_configured": True,
        }
        result = runner.invoke(install, ["--codex"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Codex" in result.output


# ---------------------------------------------------------------------------
# uninstall command
# ---------------------------------------------------------------------------
class TestUninstallCommand:
    @patch("gobby.cli.uninstall.uninstall_claude")
    def test_uninstall_claude(self, mock_uninstall: MagicMock, runner: CliRunner) -> None:
        mock_uninstall.return_value = {
            "success": True,
            "hooks_removed": ["hook1"],
            "files_removed": ["file1"],
        }
        result = runner.invoke(uninstall, ["--claude", "--yes"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Claude Code" in result.output

    @patch("gobby.cli.uninstall.uninstall_claude")
    def test_uninstall_claude_failure(self, mock_uninstall: MagicMock, runner: CliRunner) -> None:
        mock_uninstall.return_value = {
            "success": False,
            "error": "Permission denied",
        }
        result = runner.invoke(uninstall, ["--claude", "--yes"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "Permission denied" in result.output

    @patch("gobby.cli.uninstall.uninstall_codex")
    def test_uninstall_codex(self, mock_uninstall: MagicMock, runner: CliRunner) -> None:
        mock_uninstall.return_value = {
            "success": True,
            "hooks_removed": ["codex_hook"],
            "files_removed": ["codex_file"],
            "config_updated": True,
        }
        result = runner.invoke(uninstall, ["--codex", "--yes"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "Codex" in result.output

    def test_uninstall_rejects_falkordb_target(self, runner: CliRunner) -> None:
        result = runner.invoke(uninstall, ["--falkordb", "--yes"])

        assert result.exit_code == 2
        assert "No such option '--falkordb'" in result.output

    def test_uninstall_all_nothing_found(self, runner: CliRunner, tmp_path: Path) -> None:
        """When --all is used but no CLI hooks are detected."""
        # Use a clean tmp_path as home so no settings.json files are found
        with patch("gobby.cli.uninstall.Path.home", return_value=tmp_path):
            result = runner.invoke(uninstall, ["--all", "--yes"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "No Gobby hooks found" in result.output

    @staticmethod
    def _global_uninstall(
        tmp_path: Path,
        runner: CliRunner,
        bootstrap: MagicMock,
        disable: MagicMock,
    ) -> Any:
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "settings.json").write_text("{}")
        with (
            patch("gobby.cli.uninstall.Path.home", return_value=tmp_path),
            patch(
                "gobby.cli.uninstall.uninstall_claude",
                return_value={"success": True, "hooks_removed": [], "files_removed": []},
            ),
            patch("gobby.cli.uninstall.load_bootstrap", return_value=bootstrap),
            patch("gobby.cli.uninstall.disable_tailscale_ui", disable),
        ):
            return runner.invoke(uninstall, ["--all", "--yes"], catch_exceptions=False)

    def test_uninstall_all_tears_down_ui_exposure(self, runner: CliRunner, tmp_path: Path) -> None:
        bootstrap = MagicMock(ui_expose="tailscale", daemon_port=60887)
        disable = MagicMock()

        result = self._global_uninstall(tmp_path, runner, bootstrap, disable)

        assert result.exit_code == 0
        assert "Removed Tailscale UI exposure." in result.output
        disable.assert_called_once_with(60887)

    def test_uninstall_all_skips_ui_exposure_without_intent(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        bootstrap = MagicMock(ui_expose=None)
        disable = MagicMock()

        result = self._global_uninstall(tmp_path, runner, bootstrap, disable)

        assert result.exit_code == 0
        disable.assert_not_called()

    def test_uninstall_ui_exposure_failure_is_nonfatal(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        bootstrap = MagicMock(ui_expose="tailscale", daemon_port=60887)
        disable = MagicMock(side_effect=UiExposeError("sentinel"))

        result = self._global_uninstall(tmp_path, runner, bootstrap, disable)

        assert result.exit_code == 0
        assert "Warning: could not remove Tailscale UI exposure: sentinel" in result.output
        assert "gobby ui unexpose" in result.output


class TestInstallFilesHomeLifecycle:
    def test_local_install_persists_files_home_before_identity_and_services(
        self, tmp_path: Path
    ) -> None:
        files_home = tmp_path / "files"
        files_home.mkdir()
        order: list[str] = []

        def publish(_path: Path) -> dict[str, object]:
            order.append("publish")
            return {"created": True, "path": str(tmp_path / "bootstrap.yaml")}

        def identity() -> Path:
            order.append("identity")
            return files_home / "_personal" / ".gobby" / "project.json"

        def stack(*_args: object, **_kwargs: object) -> None:
            order.append("services")

        with (
            patch("gobby.cli.install._run_install_preflight", return_value=([], [])),
            patch("gobby.cli.install.get_install_dir", return_value=tmp_path),
            patch("gobby.cli.install.peek_install_bootstrap", return_value={}),
            patch("gobby.cli.install.acquire_install_maintenance", return_value=MagicMock()),
            patch("gobby.cli.install.publish_install_files_home", side_effect=publish),
            patch("gobby.cli.install.ensure_personal_project_identity", side_effect=identity),
            patch("gobby.cli.install._install_required_stack", side_effect=stack),
            patch("gobby.cli.install.run_daemon_setup"),
            patch("gobby.cli.install.get_cli_runtime") as runtime,
            patch("gobby.cli.install._maybe_start_daemon_after_install"),
            patch("gobby.cli.install._echo_install_summary", return_value=True),
            patch("gobby.cli.install._should_initialize_project", return_value=False),
        ):
            runtime.return_value.require_database.side_effect = RuntimeError("hub")
            result = CliRunner().invoke(
                install,
                [
                    "--config-only",
                    "--no-interactive",
                    "--no-expose-ui",
                    "--files-home",
                    str(files_home),
                ],
                catch_exceptions=False,
            )

        assert result.exit_code != 0 or "publish" in order
        assert order[:2] == ["publish", "identity"]
        assert "services" not in order or order.index("identity") < order.index("services")

    def test_bootstrap_write_failure_skips_identity_and_services(self, tmp_path: Path) -> None:
        files_home = tmp_path / "files"
        files_home.mkdir()
        identity = MagicMock(side_effect=AssertionError("identity must not run"))
        stack = MagicMock(side_effect=AssertionError("services must not run"))
        with (
            patch("gobby.cli.install._run_install_preflight", return_value=([], [])),
            patch("gobby.cli.install.get_install_dir", return_value=tmp_path),
            patch("gobby.cli.install.peek_install_bootstrap", return_value={}),
            patch("gobby.cli.install.acquire_install_maintenance", return_value=MagicMock()),
            patch(
                "gobby.cli.install.publish_install_files_home",
                side_effect=RuntimeError("bootstrap write failed"),
            ),
            patch("gobby.cli.install.ensure_personal_project_identity", identity),
            patch("gobby.cli.install._install_required_stack", stack),
        ):
            result = CliRunner().invoke(
                install,
                [
                    "--config-only",
                    "--no-interactive",
                    "--files-home",
                    str(files_home),
                ],
            )

        assert result.exit_code != 0
        identity.assert_not_called()
        stack.assert_not_called()

    def test_legacy_bootstrap_upgrade_then_identity_uses_files_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gobby.cli.install_files_home import publish_install_files_home
        from gobby.config.bootstrap_io import bootstrap_path, inject_local_files_home

        home = tmp_path / "home"
        home.mkdir()
        files_home = tmp_path / "files"
        files_home.mkdir()
        monkeypatch.setenv("GOBBY_HOME", str(home))
        path = bootstrap_path()
        path.write_text("datastore_mode: local\ndaemon_port: 60887\n", encoding="utf-8")
        path.chmod(0o600)
        result = publish_install_files_home(files_home)
        assert result.get("upgraded") is True
        assert "files_home:" in path.read_text()
        inject_local_files_home(path, files_home)
        assert str(files_home.resolve()) in path.read_text()

    def test_remote_install_missing_token_starts_no_services(self, tmp_path: Path) -> None:
        from gobby.cli.installers.remote_preflight import run_remote_preflight

        home = tmp_path / "gobby-home"
        home.mkdir()
        stack = MagicMock()
        errors = run_remote_preflight(
            "postgresql://gobby:secret@hub.test:5432/gobby",
            gobby_home=home,
            hub_daemon_url="http://hub.example.test:60887",
        )
        assert any("local_cli_token" in error for error in errors)
        stack.assert_not_called()

    def test_remote_owner_probe_failures_are_typed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from gobby.cli.installers import remote_preflight

        home = tmp_path / "home"
        home.mkdir()
        (home / "local_cli_token").write_text("token\n", encoding="utf-8")
        (home / ".secret_kek").write_text("kek\n", encoding="utf-8")

        class FakeResponse:
            def __init__(self, status_code: int, payload: object) -> None:
                self.status_code = status_code
                self._payload = payload
                self.text = str(payload)

            def json(self) -> object:
                if isinstance(self._payload, Exception):
                    raise self._payload
                return self._payload

        monkeypatch.setattr(
            httpx,
            "get",
            lambda *_a, **_k: FakeResponse(401, {"error": "auth"}),
        )
        errors = remote_preflight.probe_hub_user_md(
            "http://hub.example.test:60887",
            gobby_home=home,
        )
        assert any("authentication" in error for error in errors)

        monkeypatch.setattr(
            httpx,
            "get",
            lambda *_a, **_k: (_ for _ in ()).throw(httpx.ConnectError("nope")),
        )
        errors = remote_preflight.probe_hub_user_md(
            "http://hub.example.test:60887",
            gobby_home=home,
        )
        assert any("network" in error.lower() for error in errors)

        monkeypatch.setattr(
            httpx,
            "get",
            lambda *_a, **_k: FakeResponse(404, {"error": "missing"}),
        )
        errors = remote_preflight.probe_hub_user_md(
            "http://hub.example.test:60887",
            gobby_home=home,
        )
        assert any("files_home root" in error for error in errors)

        monkeypatch.setattr(
            httpx,
            "get",
            lambda *_a, **_k: FakeResponse(409, {"error": "hop_refused"}),
        )
        errors = remote_preflight.probe_hub_user_md(
            "http://hub.example.test:60887",
            gobby_home=home,
        )
        assert any("hop refused" in error for error in errors)

        monkeypatch.setattr(
            httpx,
            "get",
            lambda *_a, **_k: FakeResponse(200, {"owner": "remote", "content": ""}),
        )
        errors = remote_preflight.probe_hub_user_md(
            "http://hub.example.test:60887",
            gobby_home=home,
        )
        assert any("remote daemon" in error for error in errors)

    def test_filesystem_identity_requires_held_claim_and_refuses_racer(
        self, tmp_path: Path
    ) -> None:
        from gobby.cli.install_files_home import acquire_install_maintenance
        from gobby.paths import get_gobby_home
        from gobby.runner_pid_file import claim_pid_file

        first = claim_pid_file(get_gobby_home() / "gobby.pid", role="daemon")
        assert first is not None
        try:
            with pytest.raises(Exception, match="singleton|concurrent"):
                acquire_install_maintenance()
        finally:
            first.release()

    def test_install_to_start_converts_held_claim_under_flock(self, tmp_path: Path) -> None:
        from gobby.cli._install_daemon import maybe_start_daemon_after_install
        from gobby.paths import get_gobby_home
        from gobby.runner_pid_file import claim_pid_file

        claim = claim_pid_file(get_gobby_home() / "gobby.pid", role="maintenance")
        assert claim is not None
        converted = MagicMock()
        start = MagicMock(return_value={"success": False, "error": "launch failed"})
        try:
            with (
                patch(
                    "gobby.cli.installers.service.get_service_status",
                    return_value={"installed": True, "platform": "launchd"},
                ),
                patch(
                    "gobby.runner_pid_file.convert_held_claim_to_reservation",
                    converted,
                ),
                patch("gobby.cli.installers.service.service_start", start),
            ):
                maybe_start_daemon_after_install(
                    no_interactive=False,
                    daemon_url=lambda: "http://localhost:60887/",
                    daemon_already_running=lambda: False,
                    ci_environment=lambda: False,
                    headless_or_remote=lambda: False,
                    claim=claim,
                )
            converted.assert_called_once()
            start.assert_called_once_with(reserved=True)
        finally:
            if not claim._released:
                claim.release()
