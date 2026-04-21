"""Tests for install-flow interactive prompts in gobby.cli._install_prompts."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from gobby.cli._install_prompts import _prompt_hub_api_keys
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
