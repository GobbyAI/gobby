"""
Tests for config/llm_providers.py module.

RED PHASE: Tests initially import from llm_providers.py (should fail),
then will pass once LLMProviderConfig and LLMProvidersConfig are extracted from app.py.
"""

import pytest
from pydantic import ValidationError

pytestmark = pytest.mark.unit


class TestLLMProviderConfigImport:
    """Test that LLMProviderConfig can be imported from the llm_providers module."""

    def test_import_from_llm_providers_module(self) -> None:
        """Test importing LLMProviderConfig from config.llm_providers (RED phase target)."""
        from gobby.config.llm_providers import LLMProviderConfig

        assert LLMProviderConfig is not None


class TestLLMProviderConfigBasic:
    """Test LLMProviderConfig basic functionality."""

    def test_instantiation_with_models(self) -> None:
        """Test LLMProviderConfig instantiation with models."""
        from gobby.config.llm_providers import LLMProviderConfig

        config = LLMProviderConfig(models="model1,model2,model3")
        assert config.models == "model1,model2,model3"

    def test_default_auth_mode(self) -> None:
        """Test default auth_mode is subscription."""
        from gobby.config.llm_providers import LLMProviderConfig

        config = LLMProviderConfig(models="gpt-4")
        assert config.auth_mode == "subscription"

    def test_custom_auth_modes(self) -> None:
        """Test different auth_mode values."""
        from gobby.config.llm_providers import LLMProviderConfig

        config = LLMProviderConfig(models="model", auth_mode="api_key")
        assert config.auth_mode == "api_key"

        config = LLMProviderConfig(models="model", auth_mode="adc")
        assert config.auth_mode == "adc"

    def test_invalid_auth_mode(self) -> None:
        """Test that invalid auth_mode raises ValidationError."""
        from gobby.config.llm_providers import LLMProviderConfig

        with pytest.raises(ValidationError):
            LLMProviderConfig(models="model", auth_mode="invalid")


class TestLLMProviderConfigGetModels:
    """Test LLMProviderConfig.get_models_list() method."""

    def test_get_models_list_basic(self) -> None:
        """Test get_models_list returns list of models."""
        from gobby.config.llm_providers import LLMProviderConfig

        config = LLMProviderConfig(models="model1,model2,model3")
        models = config.get_models_list()
        assert models == ["model1", "model2", "model3"]

    def test_get_models_list_with_spaces(self) -> None:
        """Test get_models_list handles spaces."""
        from gobby.config.llm_providers import LLMProviderConfig

        config = LLMProviderConfig(models="model1, model2 , model3")
        models = config.get_models_list()
        assert models == ["model1", "model2", "model3"]

    def test_get_models_list_single_model(self) -> None:
        """Test get_models_list with single model."""
        from gobby.config.llm_providers import LLMProviderConfig

        config = LLMProviderConfig(models="gpt-4")
        models = config.get_models_list()
        assert models == ["gpt-4"]

    def test_get_models_list_empty_entries(self) -> None:
        """Test get_models_list filters empty entries."""
        from gobby.config.llm_providers import LLMProviderConfig

        config = LLMProviderConfig(models="model1,,model2,")
        models = config.get_models_list()
        assert models == ["model1", "model2"]


class TestLLMProvidersConfigImport:
    """Test that LLMProvidersConfig can be imported from the llm_providers module."""

    def test_import_from_llm_providers_module(self) -> None:
        """Test importing LLMProvidersConfig from config.llm_providers (RED phase target)."""
        from gobby.config.llm_providers import LLMProvidersConfig

        assert LLMProvidersConfig is not None


class TestLLMProvidersConfigDefaults:
    """Test LLMProvidersConfig default values."""

    def test_default_instantiation(self) -> None:
        """Test LLMProvidersConfig creates with all defaults."""
        from gobby.config.llm_providers import LLMProvidersConfig

        config = LLMProvidersConfig()
        # Claude is enabled by default for fresh installs
        assert config.claude is not None
        assert config.claude.models == "haiku,sonnet,opus"
        assert config.claude.auth_mode == "subscription"
        assert config.codex is None

    def test_claude_enabled_by_default(self) -> None:
        """Test Claude provider is enabled by default."""
        from gobby.config.llm_providers import LLMProvidersConfig

        config = LLMProvidersConfig()
        assert config.get_enabled_providers() == ["claude"]


class TestLLMProvidersConfigWithProviders:
    """Test LLMProvidersConfig with configured providers."""

    def test_claude_provider(self) -> None:
        """Test configuring Claude provider."""
        from gobby.config.llm_providers import LLMProviderConfig, LLMProvidersConfig

        config = LLMProvidersConfig(
            claude=LLMProviderConfig(models="claude-haiku-4-5,claude-sonnet-4-5")
        )
        assert config.claude is not None
        assert config.claude.models == "claude-haiku-4-5,claude-sonnet-4-5"

    def test_multiple_providers(self) -> None:
        """Test configuring multiple providers."""
        from gobby.config.llm_providers import LLMProviderConfig, LLMProvidersConfig

        config = LLMProvidersConfig(
            claude=LLMProviderConfig(models="claude-haiku-4-5"),
            codex=LLMProviderConfig(models="gpt-4o-mini", auth_mode="api_key"),
        )
        assert config.claude is not None
        assert config.codex is not None


class TestLLMProvidersConfigGetEnabledProviders:
    """Test LLMProvidersConfig.get_enabled_providers() method."""

    def test_get_enabled_providers_single(self) -> None:
        """Test get_enabled_providers with single provider."""
        from gobby.config.llm_providers import LLMProviderConfig, LLMProvidersConfig

        config = LLMProvidersConfig(claude=LLMProviderConfig(models="claude-haiku-4-5"))
        assert config.get_enabled_providers() == ["claude"]

    def test_get_enabled_providers_multiple(self) -> None:
        """Test get_enabled_providers with multiple providers."""
        from gobby.config.llm_providers import LLMProviderConfig, LLMProvidersConfig

        config = LLMProvidersConfig(
            claude=LLMProviderConfig(models="claude-haiku"),
            codex=LLMProviderConfig(models="gpt-4o"),
        )
        providers = config.get_enabled_providers()
        assert "claude" in providers
        assert "codex" in providers
        assert len(providers) == 2

    def test_get_enabled_providers_all(self) -> None:
        """Test get_enabled_providers with all providers enabled."""
        from gobby.config.llm_providers import LLMProviderConfig, LLMProvidersConfig

        config = LLMProvidersConfig(
            claude=LLMProviderConfig(models="c"),
            codex=LLMProviderConfig(models="c"),
            gemini=LLMProviderConfig(models="c"),
        )
        providers = config.get_enabled_providers()
        assert providers == ["claude", "codex", "gemini"]


class TestLLMProviderConfigFromAppPy:
    """Verify that tests pass when importing from app.py (reference implementation)."""

    def test_import_from_app_py(self) -> None:
        """Test importing LLMProviderConfig from app.py works (baseline)."""
        from gobby.config.llm_providers import LLMProviderConfig

        config = LLMProviderConfig(models="gpt-4")
        assert config.auth_mode == "subscription"
        assert config.get_models_list() == ["gpt-4"]


class TestLLMProvidersConfigFromAppPy:
    """Verify LLMProvidersConfig tests pass when importing from app.py."""

    def test_import_from_app_py(self) -> None:
        """Test importing LLMProvidersConfig from app.py works (baseline)."""
        from gobby.config.llm_providers import LLMProviderConfig, LLMProvidersConfig

        config = LLMProvidersConfig(claude=LLMProviderConfig(models="claude-haiku-4-5"))
        assert config.get_enabled_providers() == ["claude"]
