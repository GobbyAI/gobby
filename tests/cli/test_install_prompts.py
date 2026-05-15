"""Tests for install-flow interactive prompts in gobby.cli._install_prompts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest

from gobby.cli._install_prompts import (
    _echo_install_summary,
    _prompt_hub_api_keys,
    _run_voice_install,
)
from gobby.cli.install import install as install_command
from gobby.config.skills import HubConfig, SkillsConfig

pytestmark = pytest.mark.unit


def _config_with_hubs(hubs: dict[str, HubConfig]) -> MagicMock:
    """Build a mock DaemonConfig exposing just the fields the prompt reads."""
    config = MagicMock()
    config.database_path = "~/.gobby/test.db"
    config.skills = SkillsConfig(hubs=hubs)
    return config


@pytest.fixture
def patched_deps():
    """Patch SecretStore, LocalDatabase, and load_full_config_from_db at import sites."""
    with (
        patch("gobby.cli.utils.load_full_config_from_db") as mock_load,
        patch("gobby.storage.database.LocalDatabase") as mock_db_cls,
        patch("gobby.storage.secrets.SecretStore") as mock_store_cls,
    ):
        mock_db = MagicMock()
        mock_db_cls.return_value = mock_db
        mock_store = MagicMock()
        mock_store_cls.return_value = mock_store
        yield {
            "load": mock_load,
            "db_cls": mock_db_cls,
            "store_cls": mock_store_cls,
            "db": mock_db,
            "store": mock_store,
        }


class TestPromptHubApiKeys:
    def test_prompts_for_missing_key(self, patched_deps) -> None:
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

    def test_skips_when_secret_exists(self, patched_deps) -> None:
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

    def test_empty_input_is_skipped_and_marked_unresolved(self, patched_deps) -> None:
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

    def test_no_interactive_reports_unresolved_without_prompting(self, patched_deps) -> None:
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

    def test_handles_db_init_failure_gracefully(self, patched_deps) -> None:
        patched_deps["load"].side_effect = RuntimeError("DB exploded")

        result = _prompt_hub_api_keys(no_interactive=False)

        assert result == {
            "stored": 0,
            "skipped": 0,
            "already_configured": 0,
            "unresolved": [],
        }

    def test_uses_resolved_config_not_pydantic_defaults(self, patched_deps) -> None:
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

    def test_hubs_without_auth_key_name_are_skipped(self, patched_deps) -> None:
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

    def test_db_opens_resolved_config_path_not_default(self, patched_deps) -> None:
        """LocalDatabase must open the resolved config's database_path, not the default ~/.gobby/gobby-hub.db."""
        config = MagicMock()
        config.database_path = "/custom/path/to.db"
        config.skills = SkillsConfig(hubs={})
        patched_deps["load"].return_value = config

        _prompt_hub_api_keys(no_interactive=False)

        # LocalDatabase was constructed with the expanded custom path, not called with no args.
        assert patched_deps["db_cls"].called
        called_path = patched_deps["db_cls"].call_args.args[0]
        assert str(called_path) == "/custom/path/to.db"

    def test_uses_injected_db_and_secret_store(self, patched_deps) -> None:
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
        proc = MagicMock(returncode=0, stderr="")
        results: dict[str, dict[str, object]] = {}

        with (
            patch("subprocess.run", return_value=proc),
            patch("gobby.storage.config_store.ConfigStore") as mock_config_store,
        ):
            _run_voice_install(results, voice_flag=True, db=db)

        mock_config_store.assert_called_once_with(db)
        assert mock_config_store.call_count == 1
        assert mock_config_store.call_args is not None
        mock_config_store.return_value.set.assert_called_once_with("voice.enabled", True)
        assert mock_config_store.return_value.set.call_count == 1
        assert mock_config_store.return_value.set.call_args is not None
        assert results["voice"]["success"] is True


class TestInstallCommandSharedStores:
    def test_embedding_provider_requires_embedding_url(self, tmp_path: Path) -> None:
        with pytest.raises(click.UsageError, match="--embedding-provider requires --embedding-url"):
            install_command.callback(
                claude_flag=False,
                gemini_flag=False,
                codex_flag=True,
                droid_flag=False,
                qwen_flag=False,
                hooks_flag=False,
                all_flag=False,
                no_ext_services_flag=True,
                neo4j_password=None,
                voice_flag=False,
                project_flag=False,
                embedding_url=None,
                embedding_provider="lmstudio",
                embedding_model=None,
                embedding_dim=None,
                no_interactive_flag=True,
                working_dir=tmp_path,
            )

    def test_builds_one_db_and_secret_store_and_reuses_them(self, tmp_path: Path) -> None:
        config = MagicMock()
        config.database_path = str(tmp_path / "shared.db")
        db = MagicMock()
        secret_store = MagicMock()

        with (
            patch("gobby.cli.install.load_full_config_from_db", return_value=config),
            patch("gobby.cli.install.LocalDatabase", return_value=db) as mock_db_cls,
            patch("gobby.cli.install.SecretStore", return_value=secret_store) as mock_store_cls,
            patch(
                "gobby.cli.install._ensure_daemon_config",
                return_value={"created": False, "path": str(tmp_path / "bootstrap.yaml")},
            ),
            patch("gobby.cli.install.run_daemon_setup"),
            patch("gobby.cli.install.get_install_dir", return_value=tmp_path),
            patch("gobby.cli.install._run_standard_cli_install"),
            patch("gobby.cli.install._run_embedding_install", return_value="none"),
            patch("gobby.cli.install._run_voice_install") as mock_voice_install,
            patch("gobby.cli.install._echo_install_summary", return_value=True) as mock_summary,
        ):
            install_command.callback(
                claude_flag=False,
                gemini_flag=False,
                codex_flag=True,
                droid_flag=False,
                qwen_flag=False,
                hooks_flag=False,
                all_flag=False,
                no_ext_services_flag=True,
                neo4j_password=None,
                voice_flag=False,
                project_flag=False,
                embedding_url=None,
                embedding_provider=None,
                embedding_model=None,
                embedding_dim=None,
                no_interactive_flag=True,
                working_dir=tmp_path,
            )

        expected_path = Path(config.database_path).expanduser()
        mock_db_cls.assert_called_once_with(expected_path)
        mock_store_cls.assert_called_once_with(db)
        assert mock_voice_install.call_args.kwargs["db"] is db
        assert mock_voice_install.call_args.kwargs["secret_store"] is secret_store
        assert mock_summary.call_args.kwargs["db"] is db
        assert mock_summary.call_args.kwargs["secret_store"] is secret_store
        db.close.assert_called_once()

    def test_forwards_embedding_provider_override_and_reuses_shared_stores(
        self, tmp_path: Path
    ) -> None:
        config = MagicMock()
        config.database_path = str(tmp_path / "shared.db")
        db = MagicMock()
        secret_store = MagicMock()

        with (
            patch("gobby.cli.install.load_full_config_from_db", return_value=config),
            patch("gobby.cli.install.LocalDatabase", return_value=db) as mock_db_cls,
            patch("gobby.cli.install.SecretStore", return_value=secret_store) as mock_store_cls,
            patch(
                "gobby.cli.install._ensure_daemon_config",
                return_value={"created": False, "path": str(tmp_path / "bootstrap.yaml")},
            ),
            patch("gobby.cli.install.run_daemon_setup"),
            patch("gobby.cli.install.get_install_dir", return_value=tmp_path),
            patch("gobby.cli.install._run_standard_cli_install"),
            patch(
                "gobby.cli.install._run_embedding_install",
                return_value="lmstudio",
            ) as mock_embedding,
            patch("gobby.cli.install._run_voice_install") as mock_voice_install,
            patch("gobby.cli.install._echo_install_summary", return_value=True) as mock_summary,
            patch("gobby.cli.install._run_qdrant_install"),
            patch("gobby.cli.install._run_neo4j_install"),
        ):
            install_command.callback(
                claude_flag=False,
                gemini_flag=False,
                codex_flag=False,
                droid_flag=False,
                qwen_flag=False,
                hooks_flag=False,
                all_flag=True,
                no_ext_services_flag=True,
                neo4j_password=None,
                voice_flag=False,
                project_flag=False,
                embedding_url="http://lan:1234/v1",
                embedding_provider="lmstudio",
                embedding_model=None,
                embedding_dim=None,
                no_interactive_flag=True,
                working_dir=tmp_path,
            )

        expected_path = Path(config.database_path).expanduser()
        mock_db_cls.assert_called_once_with(expected_path)
        mock_store_cls.assert_called_once_with(db)
        assert mock_embedding.call_args.kwargs["api_base_override"] == "http://lan:1234/v1"
        assert mock_embedding.call_args.kwargs["provider_override"] == "lmstudio"
        assert mock_voice_install.call_args.kwargs["db"] is db
        assert mock_voice_install.call_args.kwargs["secret_store"] is secret_store
        assert mock_summary.call_args.kwargs["db"] is db
        assert mock_summary.call_args.kwargs["secret_store"] is secret_store
        db.close.assert_called_once()
