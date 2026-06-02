"""Tests for provider resolution logic."""

from unittest.mock import MagicMock

import pytest

from gobby.llm.resolver import (
    DEFAULT_PROVIDER,
    InvalidProviderError,
    MissingProviderError,
    ProviderNotConfiguredError,
    ResolvedProvider,
    resolve_provider,
    validate_provider_name,
)

pytestmark = pytest.mark.unit


class TestValidateProviderName:
    """Tests for validate_provider_name function."""

    def test_valid_provider_names(self) -> None:
        """Test that valid provider names pass validation."""
        assert validate_provider_name("claude") == "claude"
        assert validate_provider_name("codex") == "codex"
        assert validate_provider_name("openai") == "openai"
        assert validate_provider_name("claude-3") == "claude-3"
        assert validate_provider_name("my_provider") == "my_provider"
        assert validate_provider_name("Provider123") == "Provider123"

    def test_none_raises_error(self) -> None:
        """Test that None raises InvalidProviderError."""
        with pytest.raises(InvalidProviderError) as exc_info:
            validate_provider_name(None)

        assert exc_info.value.provider is None
        assert "None" in str(exc_info.value)

    def test_empty_string_raises_error(self) -> None:
        """Test that empty string raises InvalidProviderError."""
        with pytest.raises(InvalidProviderError) as exc_info:
            validate_provider_name("")

        assert "empty" in exc_info.value.reason.lower()

    def test_whitespace_only_raises_error(self) -> None:
        """Test that whitespace-only string raises InvalidProviderError."""
        with pytest.raises(InvalidProviderError):
            validate_provider_name("   ")

    def test_strips_whitespace(self) -> None:
        """Test that leading/trailing whitespace is stripped."""
        # Valid after stripping
        assert validate_provider_name("  claude  ") == "claude"

    def test_too_long_raises_error(self) -> None:
        """Test that names over 64 characters raise InvalidProviderError."""
        long_name = "a" * 65
        with pytest.raises(InvalidProviderError) as exc_info:
            validate_provider_name(long_name)

        assert "64" in str(exc_info.value)

    def test_max_length_accepted(self) -> None:
        """Test that 64 character names are accepted."""
        name_64 = "a" * 64
        assert validate_provider_name(name_64) == name_64

    def test_special_characters_rejected(self) -> None:
        """Test that special characters are rejected."""
        invalid_names = [
            "provider@v1",
            "provider/openai",
            "my provider",
            "provider.name",
            "provider!",
            "provider#1",
        ]

        for name in invalid_names:
            with pytest.raises(InvalidProviderError):
                validate_provider_name(name)

    def test_numeric_only_accepted(self) -> None:
        """Test that numeric-only names are accepted."""
        assert validate_provider_name("123") == "123"


class TestResolveProvider:
    """Tests for resolve_provider function."""

    def test_explicit_provider_highest_priority(self) -> None:
        """Test that explicit provider has highest priority."""
        result = resolve_provider(explicit_provider="codex")

        assert isinstance(result, ResolvedProvider)
        assert result.provider == "codex"
        assert result.source == "explicit"

    def test_workflow_provider_second_priority(self) -> None:
        """Test that workflow provider has second priority."""
        # Create mock workflow
        mock_workflow = MagicMock()
        mock_workflow.variables = {"provider": "codex", "model": "gpt-4o"}

        result = resolve_provider(workflow=mock_workflow)

        assert result.provider == "codex"
        assert result.source == "workflow"
        assert result.model == "gpt-4o"

    def test_config_provider_third_priority(self) -> None:
        """Test that config provider has third priority."""
        # Create mock config
        mock_config = MagicMock()
        mock_config.llm_providers.get_enabled_providers.return_value = ["claude"]

        result = resolve_provider(config=mock_config)

        assert result.provider == "claude"
        assert result.source == "config"

    def test_config_prefers_claude(self) -> None:
        """Test that config prefers claude when available."""
        mock_config = MagicMock()
        mock_config.llm_providers.get_enabled_providers.return_value = [
            "custom",
            "claude",
        ]

        result = resolve_provider(config=mock_config)

        assert result.provider == "claude"
        assert result.source == "config"

    def test_default_provider_lowest_priority(self) -> None:
        """Test that default is used when nothing else is available."""
        result = resolve_provider()

        assert result.provider == DEFAULT_PROVIDER
        assert result.source == "default"

    def test_explicit_overrides_workflow(self) -> None:
        """Test that explicit provider overrides workflow."""
        mock_workflow = MagicMock()
        mock_workflow.variables = {"provider": "codex"}

        result = resolve_provider(explicit_provider="claude", workflow=mock_workflow)

        assert result.provider == "claude"
        assert result.source == "explicit"

    def test_workflow_overrides_config(self) -> None:
        """Test that workflow overrides config."""
        mock_workflow = MagicMock()
        mock_workflow.variables = {"provider": "claude"}

        mock_config = MagicMock()
        mock_config.llm_providers.get_enabled_providers.return_value = ["claude"]

        result = resolve_provider(workflow=mock_workflow, config=mock_config)

        assert result.provider == "claude"
        assert result.source == "workflow"

    def test_validates_explicit_provider(self) -> None:
        """Test that explicit provider is validated."""
        with pytest.raises(InvalidProviderError):
            resolve_provider(explicit_provider="invalid/provider")

    def test_validates_against_config(self) -> None:
        """Test provider validation against config when not allow_unconfigured."""
        mock_config = MagicMock()
        mock_config.llm_providers.get_enabled_providers.return_value = ["claude"]

        with pytest.raises(ProviderNotConfiguredError) as exc_info:
            resolve_provider(
                explicit_provider="codex",
                config=mock_config,
                allow_unconfigured=False,
            )

        assert exc_info.value.provider == "codex"
        assert "claude" in exc_info.value.available

    def test_allow_unconfigured_skips_validation(self) -> None:
        """Test that allow_unconfigured=True skips config validation."""
        mock_config = MagicMock()
        mock_config.llm_providers.get_enabled_providers.return_value = ["claude"]

        # Should not raise
        result = resolve_provider(
            explicit_provider="codex",
            config=mock_config,
            allow_unconfigured=True,
        )

        assert result.provider == "codex"


class TestExceptionTypes:
    """Tests for exception types."""

    def test_invalid_provider_error_fields(self) -> None:
        """Test InvalidProviderError has correct fields."""
        error = InvalidProviderError("bad-provider", "invalid characters")

        assert error.provider == "bad-provider"
        assert error.reason == "invalid characters"
        assert "bad-provider" in str(error)
        assert "invalid characters" in str(error)

    def test_missing_provider_error_fields(self) -> None:
        """Test MissingProviderError has correct fields."""
        error = MissingProviderError(["explicit", "workflow", "config"])

        assert error.checked_levels == ["explicit", "workflow", "config"]
        assert "explicit" in str(error)
        assert "workflow" in str(error)
        assert "config" in str(error)

    def test_provider_not_configured_error_fields(self) -> None:
        """Test ProviderNotConfiguredError has correct fields."""
        error = ProviderNotConfiguredError("codex", ["claude", "gemini"])

        assert error.provider == "codex"
        assert error.available == ["claude", "gemini"]
        assert "codex" in str(error)
        assert "claude" in str(error)
