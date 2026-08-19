"""Tests for install-flow interactive prompts in gobby.cli._install_prompts."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import click
import pytest
from click.testing import CliRunner

from gobby.cli._install_prompts import (
    _echo_install_summary,
    _prompt_hub_api_keys,
    _run_falkordb_install,
    _run_standard_cli_install,
    _run_voice_install,
)
from gobby.cli._install_state import empty_install_state
from gobby.cli.install import install as install_command
from gobby.config.skills import HubConfig, SkillsConfig
from gobby.storage.config_mutations import ConfigPatch

pytestmark = pytest.mark.unit


def _invoke_install(**kwargs: Any) -> None:
    callback = install_command.callback
    assert callback is not None
    callback(**kwargs)


@pytest.fixture(autouse=True)
def _installed_identity() -> Any:
    with (
        patch(
            "gobby.cli.install.ensure_install_identity",
            return_value=MagicMock(email="owner@example.com"),
        ),
        patch("gobby.cli.install._provision_gdaemon_for_services"),
    ):
        yield


def test_standard_cli_install_forwards_provider_hook_timeout(tmp_path: Path) -> None:
    installer = MagicMock(return_value={"success": False, "error": "expected"})
    results: dict[str, dict[str, Any]] = {}

    _run_standard_cli_install(
        "claude",
        installer,
        tmp_path,
        "project",
        results,
        hook_timeout_seconds=150,
    )

    installer.assert_called_once_with(
        tmp_path,
        mode="project",
        hook_timeout_seconds=150,
    )
    assert results == {"claude": {"success": False, "error": "expected"}}


def test_standard_cli_install_keeps_agy_signature_unchanged(tmp_path: Path) -> None:
    installer = MagicMock(return_value={"success": False, "error": "expected"})
    results: dict[str, dict[str, Any]] = {}

    _run_standard_cli_install(
        "agy",
        installer,
        tmp_path,
        "project",
        results,
        hook_timeout_seconds=150,
    )

    installer.assert_called_once_with(tmp_path, mode="project")
    assert results == {"agy": {"success": False, "error": "expected"}}


def _config_with_hubs(hubs: dict[str, HubConfig]) -> MagicMock:
    """Build a mock DaemonConfig exposing just the fields the prompt reads."""
    config = MagicMock()
    config.database_url = "~/.gobby/test.db"
    config.skills = SkillsConfig(hubs=hubs)
    return config


@pytest.fixture
def patched_deps() -> Any:
    """Patch the CLI runtime, SecretStore, and runtime hub open."""
    runtime = MagicMock()
    with (
        patch("gobby.cli._install_prompts.get_cli_runtime", return_value=runtime),
        patch("gobby.storage.hub.runtime.runtime_hub_database") as mock_db_cls,
        patch("gobby.storage.secrets.SecretStore") as mock_store_cls,
    ):
        mock_db = MagicMock()
        mock_db_cls.return_value = mock_db
        mock_store = MagicMock()
        mock_store_cls.return_value = mock_store
        yield {
            "load": runtime.require_config,
            "runtime": runtime,
            "db_cls": mock_db_cls,
            "store_cls": mock_store_cls,
            "db": mock_db,
            "store": mock_store,
        }


class TestPromptHubApiKeys:
    def test_prompts_for_missing_key(
        self,
        patched_deps: dict[str, MagicMock],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        patched_deps["load"].return_value = _config_with_hubs(
            {
                "skillsmp": HubConfig(
                    type="skillsmp",
                    base_url="https://skillsmp.com/api/v1",
                    auth_key_name="SKILLSMP_API_KEY",
                ),
            }
        )
        patched_deps["store"].exists.return_value = False

        with patch("click.prompt", return_value="my-secret-token"):
            result = _prompt_hub_api_keys(no_interactive=False)

        patched_deps["store"].set.assert_called_once()
        call_kwargs = patched_deps["store"].set.call_args.kwargs
        assert call_kwargs["name"] == "SKILLSMP_API_KEY"
        assert call_kwargs["plaintext_value"] == "my-secret-token"
        assert call_kwargs["category"] == "integration"
        assert result["stored"] == 1
        assert result["already_configured"] == 0
        assert result["skipped"] == 0
        assert result["unresolved"] == []
        output = capsys.readouterr().out
        assert "Stored credential for skillsmp" in output
        assert "SKILLSMP_API_KEY" not in output

    def test_skips_when_secret_exists(self, patched_deps: dict[str, MagicMock]) -> None:
        patched_deps["load"].return_value = _config_with_hubs(
            {
                "skillsmp": HubConfig(
                    type="skillsmp",
                    base_url="https://skillsmp.com/api/v1",
                    auth_key_name="SKILLSMP_API_KEY",
                ),
            }
        )
        patched_deps["store"].exists.return_value = True

        with patch("click.prompt") as mock_prompt:
            result = _prompt_hub_api_keys(no_interactive=False)

        mock_prompt.assert_not_called()
        patched_deps["store"].set.assert_not_called()
        assert result["already_configured"] == 1
        assert result["stored"] == 0
        assert result["unresolved"] == []

    def test_empty_input_is_skipped_and_marked_unresolved(
        self, patched_deps: dict[str, MagicMock]
    ) -> None:
        patched_deps["load"].return_value = _config_with_hubs(
            {
                "skillsmp": HubConfig(
                    type="skillsmp",
                    base_url="https://skillsmp.com/api/v1",
                    auth_key_name="SKILLSMP_API_KEY",
                ),
            }
        )
        patched_deps["store"].exists.return_value = False

        with patch("click.prompt", return_value=""):
            result = _prompt_hub_api_keys(no_interactive=False)

        patched_deps["store"].set.assert_not_called()
        assert result["skipped"] == 1
        assert result["stored"] == 0
        assert result["unresolved"] == [("skillsmp", "SKILLSMP_API_KEY")]

    def test_no_interactive_reports_unresolved_without_prompting(
        self, patched_deps: dict[str, MagicMock]
    ) -> None:
        patched_deps["load"].return_value = _config_with_hubs(
            {
                "skillsmp": HubConfig(
                    type="skillsmp",
                    base_url="https://skillsmp.com/api/v1",
                    auth_key_name="SKILLSMP_API_KEY",
                ),
            }
        )
        patched_deps["store"].exists.return_value = False

        with patch("click.prompt") as mock_prompt:
            result = _prompt_hub_api_keys(no_interactive=True)

        mock_prompt.assert_not_called()
        patched_deps["store"].set.assert_not_called()
        assert result["unresolved"] == [("skillsmp", "SKILLSMP_API_KEY")]
        assert result["stored"] == 0
        assert result["skipped"] == 0

    def test_handles_db_init_failure_gracefully(self, patched_deps: dict[str, MagicMock]) -> None:
        patched_deps["load"].side_effect = RuntimeError("DB exploded")

        result = _prompt_hub_api_keys(no_interactive=False)

        assert result == {
            "stored": 0,
            "skipped": 0,
            "already_configured": 0,
            "unresolved": [],
        }

    def test_uses_resolved_config_not_pydantic_defaults(
        self, patched_deps: dict[str, MagicMock]
    ) -> None:
        """The prompt iterates the user's actual hub config, not SkillsConfig()."""
        # User has a custom hub and NO default skillsmp hub.
        patched_deps["load"].return_value = _config_with_hubs(
            {
                "custom-authed-hub": HubConfig(
                    type="skillsmp",
                    base_url="https://internal.corp/api",
                    auth_key_name="CUSTOM_HUB_KEY",
                ),
            }
        )
        patched_deps["store"].exists.return_value = False

        with patch("click.prompt", return_value="secret") as mock_prompt:
            result = _prompt_hub_api_keys(no_interactive=False)

        # Prompt surfaces the custom hub's key name, not SKILLSMP_API_KEY.
        prompt_text = mock_prompt.call_args.args[0]
        assert "custom-authed-hub" in prompt_text
        assert "CUSTOM_HUB_KEY" in prompt_text
        patched_deps["store"].set.assert_called_once()
        assert patched_deps["store"].set.call_args.kwargs["name"] == "CUSTOM_HUB_KEY"
        assert result["stored"] == 1

    def test_hubs_without_auth_key_name_are_skipped(
        self, patched_deps: dict[str, MagicMock]
    ) -> None:
        """Hubs with auth_key_name=None are not prompted for, not counted, not unresolved."""
        patched_deps["load"].return_value = _config_with_hubs(
            {
                "clawdhub": HubConfig(type="clawdhub"),  # no auth_key_name
                "claude-plugins": HubConfig(
                    type="claude-plugins", base_url="https://x.dev"
                ),  # no auth_key_name
            }
        )
        patched_deps["store"].exists.return_value = False

        with patch("click.prompt") as mock_prompt:
            result = _prompt_hub_api_keys(no_interactive=False)

        mock_prompt.assert_not_called()
        patched_deps["store"].set.assert_not_called()
        assert result == {
            "stored": 0,
            "skipped": 0,
            "already_configured": 0,
            "unresolved": [],
        }

    def test_opens_runtime_hub_without_removed_path(
        self, patched_deps: dict[str, MagicMock]
    ) -> None:
        """Prompt setup opens the active runtime hub instead of a configured PostgreSQL path."""
        config = MagicMock()
        config.database_url = "/custom/path/to.db"
        config.skills = SkillsConfig(hubs={})
        patched_deps["load"].return_value = config

        result = _prompt_hub_api_keys(no_interactive=False)

        assert result == {
            "stored": 0,
            "skipped": 0,
            "already_configured": 0,
            "unresolved": [],
        }
        patched_deps["db_cls"].assert_called_once_with()

    def test_uses_injected_db_and_secret_store(self, patched_deps: dict[str, MagicMock]) -> None:
        patched_deps["load"].return_value = _config_with_hubs(
            {
                "skillsmp": HubConfig(
                    type="skillsmp",
                    base_url="https://skillsmp.com/api/v1",
                    auth_key_name="SKILLSMP_API_KEY",
                ),
            }
        )
        injected_db = MagicMock()
        injected_store = MagicMock()
        injected_store.exists.return_value = False

        with patch("click.prompt", return_value="shared-secret"):
            result = _prompt_hub_api_keys(
                no_interactive=False,
                db=injected_db,
                secret_store=injected_store,
            )

        patched_deps["db_cls"].assert_not_called()
        patched_deps["store_cls"].assert_not_called()
        injected_store.set.assert_called_once()
        assert result["stored"] == 1


class TestInstallSummary:
    def test_forwards_injected_db_and_secret_store_to_prompts(self) -> None:
        db = MagicMock()
        db.fetchone.return_value = None
        secret_store = MagicMock()

        with (
            patch(
                "gobby.cli._install_prompts._prompt_api_keys",
                return_value={"stored": 0, "skipped": 0, "env_found": 0, "already_configured": 1},
            ) as mock_prompt_api,
            patch(
                "gobby.cli._install_prompts._prompt_hub_api_keys",
                return_value={
                    "stored": 0,
                    "skipped": 0,
                    "already_configured": 0,
                    "unresolved": [],
                },
            ) as mock_prompt_hub,
        ):
            assert (
                _echo_install_summary(
                    {"codex": {"success": True}},
                    True,
                    db=db,
                    secret_store=secret_store,
                )
                is True
            )

        mock_prompt_api.assert_called_once_with(
            no_interactive=True,
            db=db,
            secret_store=secret_store,
        )
        assert mock_prompt_api.call_count == 1
        assert mock_prompt_api.call_args is not None
        mock_prompt_hub.assert_called_once_with(
            no_interactive=True,
            db=db,
            secret_store=secret_store,
        )
        assert mock_prompt_hub.call_count == 1
        assert mock_prompt_hub.call_args is not None


class TestVoiceInstall:
    def test_uses_injected_db_for_voice_config_write(self) -> None:
        db = MagicMock()
        results: dict[str, dict[str, object]] = {}

        with (
            patch("subprocess.run") as mock_subprocess_run,
            patch("gobby.storage.config_mutations.ConfigMutations") as mock_mutations,
        ):
            mock_mutations.return_value.repository.read.return_value.revision = 41
            _run_voice_install(results, voice_flag=True, db=db)

        mock_subprocess_run.assert_not_called()
        mock_mutations.assert_called_once_with(db)
        mock_mutations.return_value.patch.assert_called_once_with(
            expected_revision=41,
            patch=ConfigPatch(values={"voice.enabled": True}),
        )
        assert results["voice"]["success"] is True

    def test_reconfigure_can_disable_voice(self) -> None:
        db = MagicMock()
        results: dict[str, dict[str, object]] = {}

        with (
            patch("click.confirm", return_value=False),
            patch("gobby.storage.config_mutations.ConfigMutations") as mock_mutations,
        ):
            mock_mutations.return_value.repository.read.return_value.revision = 42
            _run_voice_install(
                results,
                voice_flag=False,
                no_interactive=False,
                db=db,
                reconfigure=True,
                current_enabled=True,
            )

        mock_mutations.return_value.patch.assert_called_once_with(
            expected_revision=42,
            patch=ConfigPatch(values={"voice.enabled": False}),
        )
        assert results["voice"] == {"success": True, "enabled": False}


class TestFalkorDBInstallPrompt:
    @pytest.mark.parametrize(
        ("password_source", "password", "expected"),
        [
            ("generated", "generated-pw", "Generated FalkorDB password: generated-pw"),
            ("provided", None, "Using provided FalkorDB password (not displayed)"),
            ("reused", None, "Reusing existing FalkorDB password from config_store"),
        ],
    )
    def test_discloses_password_by_source(
        self,
        password_source: str,
        password: str | None,
        expected: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        results: dict[str, dict[str, object]] = {}
        installer = MagicMock(
            return_value={
                "success": True,
                "password_source": password_source,
                "password": password,
                "url": "redis://localhost:16379",
                "browser_url": "http://localhost:13000",
            }
        )

        _run_falkordb_install(installer, "input-pw", results)

        output = capsys.readouterr().out
        installer.assert_called_once_with(password="input-pw")
        assert expected in output
        assert "Browser: http://localhost:13000" in output
        assert results["falkordb"]["password_source"] == password_source


class TestInstallCommandSharedStores:
    def test_embedding_provider_requires_embedding_url(self, tmp_path: Path) -> None:
        with pytest.raises(click.UsageError, match="--embedding-provider requires --embedding-url"):
            _invoke_install(
                claude_flag=False,
                grok_flag=False,
                agy_flag=False,
                codex_flag=True,
                droid_flag=False,
                qwen_flag=False,
                hooks_flag=False,
                all_flag=False,
                config_only_flag=False,
                falkordb_password_stdin=False,
                voice_flag=False,
                project_flag=False,
                embedding_url=None,
                embedding_provider="lmstudio",
                embedding_model=None,
                embedding_dim=None,
                secret_kek_posture="key-file",
                ide_settings_flag=None,
                expose_ui_flag=None,
                no_interactive_flag=True,
                container_restarts_flag=True,
                working_dir=tmp_path,
            )

    def test_builds_one_db_and_secret_store_and_reuses_them(self, tmp_path: Path) -> None:
        config = MagicMock()
        config.database_url = str(tmp_path / "shared.db")
        config.hooks.provider_timeout = 150
        db = MagicMock()
        db.fetchone.return_value = None
        secret_store = MagicMock()
        config_store = MagicMock()
        auth_store = MagicMock()
        mock_store_cls = MagicMock(return_value=secret_store)
        mock_config_cls = MagicMock(return_value=config_store)
        mock_auth_cls = MagicMock(return_value=auth_store)
        mock_provision_token = MagicMock()
        runtime = MagicMock()
        runtime.require_database.return_value = db
        runtime.require_config.return_value = config

        with (
            # all_flag auto-detects installed CLIs via these unpatched probes;
            # without pinning them the result depends on the host PATH and on
            # whatever a prior test left in shutil.which/env, and an empty
            # detection makes the callback sys.exit(1) (install.py:275).
            patch("gobby.cli.install._is_claude_code_installed", return_value=True),
            patch("gobby.cli.install._is_grok_cli_installed", return_value=False),
            patch("gobby.cli.install._is_agy_cli_installed", return_value=False),
            patch("gobby.cli.install._is_qwen_cli_installed", return_value=False),
            patch("gobby.cli.install._is_codex_cli_installed", return_value=False),
            patch("gobby.cli.install._is_droid_cli_installed", return_value=False),
            patch("gobby.cli.install.get_cli_runtime", return_value=runtime) as get_runtime,
            patch.multiple(
                "gobby.cli.install",
                SecretStore=mock_store_cls,
                ConfigStore=mock_config_cls,
                AuthStore=mock_auth_cls,
                _provision_local_api_token=mock_provision_token,
                install_postgres=MagicMock(return_value={"success": True}),
            ),
            patch(
                "gobby.cli.install._ensure_daemon_config",
                return_value={"created": False, "path": str(tmp_path / "bootstrap.yaml")},
            ),
            patch("gobby.cli.install.run_daemon_setup"),
            patch("gobby.cli.install.get_install_dir", return_value=tmp_path),
            patch("gobby.cli.install._run_install_preflight", return_value=([], [])),
            patch("gobby.cli.install.peek_install_bootstrap", return_value={}),
            patch(
                "gobby.cli.install.resolve_install_files_home",
                return_value=tmp_path / "files",
            ),
            patch("gobby.cli.install.acquire_install_maintenance", return_value=MagicMock()),
            patch(
                "gobby.cli.install.publish_install_files_home",
                return_value={"created": False, "path": str(tmp_path / "bootstrap.yaml")},
            ),
            patch(
                "gobby.cli.install.ensure_personal_project_identity",
                return_value=tmp_path / "files/_personal/.gobby/project.json",
            ),
            patch("gobby.cli.install._run_standard_cli_install") as mock_standard_install,
            patch("gobby.cli.install._run_embedding_install", return_value="none"),
            patch("gobby.cli.install._run_voice_install") as mock_voice_install,
            patch("gobby.cli.install._echo_install_summary", return_value=True) as mock_summary,
        ):
            _invoke_install(
                claude_flag=False,
                grok_flag=False,
                agy_flag=False,
                codex_flag=True,
                droid_flag=False,
                qwen_flag=False,
                hooks_flag=False,
                all_flag=False,
                config_only_flag=False,
                falkordb_password_stdin=False,
                voice_flag=False,
                project_flag=False,
                embedding_url=None,
                embedding_provider=None,
                embedding_model=None,
                embedding_dim=None,
                secret_kek_posture="key-file",
                ide_settings_flag=None,
                expose_ui_flag=None,
                no_interactive_flag=True,
                container_restarts_flag=True,
                working_dir=tmp_path,
            )

        get_runtime.assert_called_once_with()
        runtime.require_database.assert_called_once_with()
        runtime.require_config.assert_called_once_with()
        mock_store_cls.assert_called_once_with(db)
        mock_config_cls.assert_called_once_with(db)
        mock_auth_cls.assert_called_once_with(db)
        mock_provision_token.assert_called_once_with(auth_store)
        assert mock_standard_install.call_args.kwargs["hook_timeout_seconds"] == 150
        assert mock_voice_install.call_args.kwargs["db"] is db
        assert mock_voice_install.call_args.kwargs["secret_store"] is secret_store
        assert mock_summary.call_args.kwargs["db"] is db
        assert mock_summary.call_args.kwargs["secret_store"] is secret_store
        runtime.close.assert_called_once_with()
        db.close.assert_not_called()

    def test_closes_database_context_when_secret_setup_fails(self, tmp_path: Path) -> None:
        db = MagicMock()
        secret_store = MagicMock()
        config_store = MagicMock()
        secret_store_cls = MagicMock(return_value=secret_store)
        config_store_cls = MagicMock(return_value=config_store)
        configure_secret_kek = MagicMock(side_effect=RuntimeError("secret setup failed"))
        runtime = MagicMock()
        runtime.require_database.return_value = db

        with (
            patch("gobby.cli.install._is_claude_code_installed", return_value=True),
            patch("gobby.cli.install._is_grok_cli_installed", return_value=False),
            patch("gobby.cli.install._is_agy_cli_installed", return_value=False),
            patch("gobby.cli.install._is_qwen_cli_installed", return_value=False),
            patch("gobby.cli.install._is_codex_cli_installed", return_value=False),
            patch("gobby.cli.install._is_droid_cli_installed", return_value=False),
            patch("gobby.cli.install.get_cli_runtime", return_value=runtime),
            patch("gobby.cli.install.SecretStore", secret_store_cls),
            patch("gobby.cli.install.ConfigStore", config_store_cls),
            patch("gobby.cli.install._configure_secret_kek_posture", configure_secret_kek),
            patch(
                "gobby.cli.install._ensure_daemon_config",
                return_value={"created": False, "path": str(tmp_path / "bootstrap.yaml")},
            ),
            patch("gobby.cli.install.run_daemon_setup"),
            patch("gobby.cli.install.get_install_dir", return_value=tmp_path),
            patch("gobby.cli.install._run_install_preflight", return_value=([], [])),
            patch("gobby.cli.install.peek_install_bootstrap", return_value={}),
            patch(
                "gobby.cli.install.resolve_install_files_home",
                return_value=tmp_path / "files",
            ),
            patch("gobby.cli.install.acquire_install_maintenance", return_value=MagicMock()),
            patch(
                "gobby.cli.install.publish_install_files_home",
                return_value={"created": False, "path": str(tmp_path / "bootstrap.yaml")},
            ),
            patch(
                "gobby.cli.install.ensure_personal_project_identity",
                return_value=tmp_path / "files/_personal/.gobby/project.json",
            ),
            pytest.raises(RuntimeError, match="secret setup failed"),
        ):
            _invoke_install(
                claude_flag=False,
                grok_flag=False,
                agy_flag=False,
                codex_flag=True,
                droid_flag=False,
                qwen_flag=False,
                hooks_flag=False,
                all_flag=False,
                config_only_flag=False,
                falkordb_password_stdin=False,
                voice_flag=False,
                project_flag=False,
                embedding_url=None,
                embedding_provider=None,
                embedding_model=None,
                embedding_dim=None,
                secret_kek_posture="key-file",
                ide_settings_flag=None,
                expose_ui_flag=None,
                no_interactive_flag=True,
                container_restarts_flag=True,
                working_dir=tmp_path,
            )

        runtime.require_database.assert_called_once_with()
        assert secret_store_cls.call_args == call(db)
        assert config_store_cls.call_args == call(db)
        assert configure_secret_kek.call_args == call(
            secret_store,
            "key-file",
            no_interactive=True,
        )
        runtime.close.assert_called_once_with()

    def test_forwards_embedding_provider_override_and_reuses_shared_stores(
        self, tmp_path: Path
    ) -> None:
        config = MagicMock()
        config.database_url = str(tmp_path / "shared.db")
        db = MagicMock()
        db.fetchone.return_value = None
        secret_store = MagicMock()
        config_store = MagicMock()
        auth_store = MagicMock()
        mock_store_cls = MagicMock(return_value=secret_store)
        mock_config_cls = MagicMock(return_value=config_store)
        mock_auth_cls = MagicMock(return_value=auth_store)
        mock_provision_token = MagicMock()
        runtime = MagicMock()
        runtime.require_database.return_value = db
        runtime.require_config.return_value = config

        with (
            # all_flag auto-detects installed CLIs via these unpatched probes;
            # without pinning them the result depends on the host PATH and on
            # whatever a prior test left in shutil.which/env, and an empty
            # detection makes the callback sys.exit(1) (install.py:275).
            patch("gobby.cli.install._is_claude_code_installed", return_value=True),
            patch("gobby.cli.install._is_grok_cli_installed", return_value=False),
            patch("gobby.cli.install._is_agy_cli_installed", return_value=False),
            patch("gobby.cli.install._is_qwen_cli_installed", return_value=False),
            patch("gobby.cli.install._is_codex_cli_installed", return_value=False),
            patch("gobby.cli.install._is_droid_cli_installed", return_value=False),
            patch("gobby.cli.install.get_cli_runtime", return_value=runtime) as get_runtime,
            patch.multiple(
                "gobby.cli.install",
                SecretStore=mock_store_cls,
                ConfigStore=mock_config_cls,
                AuthStore=mock_auth_cls,
                _provision_local_api_token=mock_provision_token,
                install_postgres=MagicMock(return_value={"success": True}),
                apply_managed_service_restart_policy=MagicMock(return_value={"success": True}),
            ),
            patch(
                "gobby.cli.install._ensure_daemon_config",
                return_value={"created": False, "path": str(tmp_path / "bootstrap.yaml")},
            ),
            patch("gobby.cli.install.run_daemon_setup"),
            patch("gobby.cli.install.get_install_dir", return_value=tmp_path),
            patch.multiple(
                "gobby.cli.install",
                peek_install_bootstrap=lambda: {},
                resolve_install_files_home=lambda *_a, **_k: tmp_path / "files",
                acquire_install_maintenance=lambda: MagicMock(),
                publish_install_files_home=lambda *_a, **_k: {
                    "created": False,
                    "path": str(tmp_path / "bootstrap.yaml"),
                },
                ensure_personal_project_identity=lambda: (
                    tmp_path / "files/_personal/.gobby/project.json"
                ),
            ),
            patch("gobby.cli.install._run_standard_cli_install"),
            patch("gobby.cli.install.prepare_install_state", return_value=empty_install_state()),
            patch(
                "gobby.cli.install._run_embedding_install",
                return_value="lmstudio",
            ) as mock_embedding,
            patch("gobby.cli.install._run_voice_install") as mock_voice_install,
            patch("gobby.cli.install._echo_install_summary", return_value=True) as mock_summary,
            patch(
                "gobby.cli.install._run_qdrant_install",
                side_effect=lambda _installer, results: results.update(
                    {"qdrant": {"success": True}}
                ),
            ),
            patch(
                "gobby.cli.install._run_falkordb_install",
                side_effect=lambda _installer, _password, results: results.update(
                    {"falkordb": {"success": True}}
                ),
            ),
            patch("gobby.cli.install._run_install_preflight", return_value=([], [])),
            patch("gobby.cli.install._maybe_start_daemon_after_install"),
        ):
            _invoke_install(
                claude_flag=False,
                grok_flag=False,
                agy_flag=False,
                codex_flag=False,
                droid_flag=False,
                qwen_flag=False,
                hooks_flag=False,
                all_flag=True,
                config_only_flag=False,
                falkordb_password_stdin=False,
                voice_flag=False,
                project_flag=False,
                embedding_url="http://lan:1234/v1",
                embedding_provider="lmstudio",
                embedding_model=None,
                embedding_dim=None,
                secret_kek_posture="key-file",
                ide_settings_flag=None,
                expose_ui_flag=None,
                no_interactive_flag=True,
                container_restarts_flag=True,
                working_dir=tmp_path,
            )

        get_runtime.assert_called_once_with()
        runtime.require_database.assert_called_once_with()
        runtime.require_config.assert_called_once_with()
        mock_store_cls.assert_called_once_with(db)
        mock_config_cls.assert_called_once_with(db)
        mock_auth_cls.assert_called_once_with(db)
        mock_provision_token.assert_called_once_with(auth_store)
        assert mock_embedding.call_args.kwargs["api_base_override"] == "http://lan:1234/v1"
        assert "embedding_api_key" not in mock_embedding.call_args.kwargs
        assert mock_embedding.call_args.kwargs["provider_override"] == "lmstudio"
        assert mock_voice_install.call_args.kwargs["db"] is db
        assert mock_voice_install.call_args.kwargs["secret_store"] is secret_store
        assert mock_summary.call_args.kwargs["db"] is db
        assert mock_summary.call_args.kwargs["secret_store"] is secret_store
        runtime.close.assert_called_once_with()
        db.close.assert_not_called()


def test_install_prompt_accepts_files_home(tmp_path: Path) -> None:
    files_home = tmp_path / "files"
    files_home.mkdir()
    resolve = MagicMock(return_value=files_home)
    with (
        patch("gobby.cli.install._run_install_preflight", return_value=([], [])),
        patch("gobby.cli.install.get_install_dir", return_value=tmp_path),
        patch("gobby.cli.install.peek_install_bootstrap", return_value={}),
        patch("gobby.cli.install.resolve_install_files_home", resolve),
        patch("gobby.cli.install.acquire_install_maintenance", return_value=MagicMock()),
        patch(
            "gobby.cli.install.publish_install_files_home",
            return_value={"created": False, "path": str(tmp_path / "bootstrap.yaml")},
        ),
        patch(
            "gobby.cli.install.ensure_personal_project_identity",
            return_value=files_home / "_personal/.gobby/project.json",
        ),
        patch(
            "gobby.cli.install._ensure_daemon_config",
            return_value={"created": False, "path": "x"},
        ),
        patch(
            "gobby.cli.install.run_daemon_setup",
            side_effect=RuntimeError("stop after identity"),
        ),
    ):
        CliRunner().invoke(
            install_command,
            ["--config-only", "--no-interactive", "--files-home", str(files_home)],
        )
    assert resolve.call_count >= 1
    assert resolve.call_args[0][0] == files_home
    assert files_home.is_dir()
