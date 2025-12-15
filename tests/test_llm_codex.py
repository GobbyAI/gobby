"""Tests for the CodexProvider LLM implementation."""

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.app import (
    DaemonConfig,
    LLMProviderConfig,
    LLMProvidersConfig,
    SessionSummaryConfig,
    TitleSynthesisConfig,
)


@pytest.fixture
def codex_config() -> DaemonConfig:
    """Create a DaemonConfig with Codex provider configured."""
    return DaemonConfig(
        llm_providers=LLMProvidersConfig(
            codex=LLMProviderConfig(models="gpt-4o", auth_mode="subscription"),
        ),
        session_summary=SessionSummaryConfig(model="gpt-4o"),
        title_synthesis=TitleSynthesisConfig(model="gpt-4o-mini"),
    )


@pytest.fixture
def codex_config_api_key() -> DaemonConfig:
    """Create a DaemonConfig with Codex provider using API key auth."""
    return DaemonConfig(
        llm_providers=LLMProvidersConfig(
            codex=LLMProviderConfig(models="gpt-4o", auth_mode="api_key"),
        ),
    )


class TestCodexProviderInit:
    """Tests for CodexProvider initialization."""

    def test_init_subscription_mode_missing_auth_json(self, codex_config: DaemonConfig, tmp_path: Path):
        """Test initialization with subscription mode but no auth.json."""
        with patch("gobby.llm.codex.shutil.which", return_value=None):
            with patch("gobby.llm.codex.Path.home", return_value=tmp_path):
                from gobby.llm.codex import CodexProvider
                provider = CodexProvider(codex_config)

                assert provider._client is None
                assert provider.supports_code_execution is False

    def test_init_codex_cli_not_available(self, codex_config: DaemonConfig, tmp_path: Path):
        """Test that code execution is disabled when CLI is not found."""
        with patch("gobby.llm.codex.shutil.which", return_value=None):
            with patch("gobby.llm.codex.Path.home", return_value=tmp_path):
                from gobby.llm.codex import CodexProvider
                provider = CodexProvider(codex_config)

                assert provider.supports_code_execution is False
                assert provider.provider_name == "codex"

    def test_init_with_auth_json(self, codex_config: DaemonConfig, tmp_path: Path):
        """Test initialization with valid auth.json."""
        # Set up mock auth.json
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        auth_file = codex_dir / "auth.json"
        auth_file.write_text(json.dumps({"OPENAI_API_KEY": "sk-test-key"}))

        with patch("gobby.llm.codex.shutil.which", return_value="/usr/local/bin/codex"):
            with patch("gobby.llm.codex.Path.home", return_value=tmp_path):
                from gobby.llm.codex import CodexProvider
                provider = CodexProvider(codex_config)

                assert provider.supports_code_execution is True
                assert provider.auth_mode == "subscription"


class TestCodexProviderProperties:
    """Tests for CodexProvider properties."""

    def test_provider_name(self, codex_config: DaemonConfig, tmp_path: Path):
        """Test provider_name property."""
        with patch("gobby.llm.codex.shutil.which", return_value=None):
            with patch("gobby.llm.codex.Path.home", return_value=tmp_path):
                from gobby.llm.codex import CodexProvider
                provider = CodexProvider(codex_config)
                assert provider.provider_name == "codex"

    def test_get_model_summary(self, codex_config: DaemonConfig, tmp_path: Path):
        """Test _get_model for summary task."""
        with patch("gobby.llm.codex.shutil.which", return_value=None):
            with patch("gobby.llm.codex.Path.home", return_value=tmp_path):
                from gobby.llm.codex import CodexProvider
                provider = CodexProvider(codex_config)
                assert provider._get_model("summary") == "gpt-4o"

    def test_get_model_title(self, codex_config: DaemonConfig, tmp_path: Path):
        """Test _get_model for title task."""
        with patch("gobby.llm.codex.shutil.which", return_value=None):
            with patch("gobby.llm.codex.Path.home", return_value=tmp_path):
                from gobby.llm.codex import CodexProvider
                provider = CodexProvider(codex_config)
                assert provider._get_model("title") == "gpt-4o-mini"

    def test_get_model_unknown(self, codex_config: DaemonConfig, tmp_path: Path):
        """Test _get_model for unknown task defaults to gpt-4o."""
        with patch("gobby.llm.codex.shutil.which", return_value=None):
            with patch("gobby.llm.codex.Path.home", return_value=tmp_path):
                from gobby.llm.codex import CodexProvider
                provider = CodexProvider(codex_config)
                assert provider._get_model("unknown") == "gpt-4o"


class TestCodexProviderGenerateSummary:
    """Tests for generate_summary method."""

    @pytest.mark.asyncio
    async def test_generate_summary_no_client(self, codex_config: DaemonConfig, tmp_path: Path):
        """Test generate_summary returns error when client not initialized."""
        with patch("gobby.llm.codex.shutil.which", return_value=None):
            with patch("gobby.llm.codex.Path.home", return_value=tmp_path):
                from gobby.llm.codex import CodexProvider
                provider = CodexProvider(codex_config)
                provider._client = None

                result = await provider.generate_summary(
                    {"transcript_summary": "test"},
                    prompt_template="Test {transcript_summary}"
                )

                assert "unavailable" in result

    @pytest.mark.asyncio
    async def test_generate_summary_no_template(self, codex_config: DaemonConfig, tmp_path: Path):
        """Test generate_summary raises when no template provided."""
        with patch("gobby.llm.codex.shutil.which", return_value=None):
            with patch("gobby.llm.codex.Path.home", return_value=tmp_path):
                from gobby.llm.codex import CodexProvider
                provider = CodexProvider(codex_config)
                # Set a mock client to pass the None check
                provider._client = MagicMock()

                with pytest.raises(ValueError, match="prompt_template is required"):
                    await provider.generate_summary({"transcript_summary": "test"})


class TestCodexProviderSynthesizeTitle:
    """Tests for synthesize_title method."""

    @pytest.mark.asyncio
    async def test_synthesize_title_no_client(self, codex_config: DaemonConfig, tmp_path: Path):
        """Test synthesize_title returns None when client not initialized."""
        with patch("gobby.llm.codex.shutil.which", return_value=None):
            with patch("gobby.llm.codex.Path.home", return_value=tmp_path):
                from gobby.llm.codex import CodexProvider
                provider = CodexProvider(codex_config)
                provider._client = None

                result = await provider.synthesize_title(
                    "test prompt",
                    prompt_template="Generate title: {user_prompt}"
                )

                assert result is None

    @pytest.mark.asyncio
    async def test_synthesize_title_no_template(self, codex_config: DaemonConfig, tmp_path: Path):
        """Test synthesize_title raises when no template provided."""
        with patch("gobby.llm.codex.shutil.which", return_value=None):
            with patch("gobby.llm.codex.Path.home", return_value=tmp_path):
                from gobby.llm.codex import CodexProvider
                provider = CodexProvider(codex_config)
                provider._client = MagicMock()

                with pytest.raises(ValueError, match="prompt_template is required"):
                    await provider.synthesize_title("test prompt")


class TestCodexProviderExecuteCode:
    """Tests for execute_code method."""

    @pytest.mark.asyncio
    async def test_execute_code_no_cli(self, codex_config: DaemonConfig, tmp_path: Path):
        """Test execute_code fails gracefully when CLI not available."""
        with patch("gobby.llm.codex.shutil.which", return_value=None):
            with patch("gobby.llm.codex.Path.home", return_value=tmp_path):
                from gobby.llm.codex import CodexProvider
                provider = CodexProvider(codex_config)

                result = await provider.execute_code("print('hello')")

                assert result["success"] is False
                assert "requires Codex CLI" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_code_unsupported_language(self, codex_config: DaemonConfig, tmp_path: Path):
        """Test execute_code fails for unsupported languages."""
        # Set up auth.json and mock CLI
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        auth_file = codex_dir / "auth.json"
        auth_file.write_text(json.dumps({"OPENAI_API_KEY": "sk-test"}))

        with patch("gobby.llm.codex.shutil.which", return_value="/usr/local/bin/codex"):
            with patch("gobby.llm.codex.Path.home", return_value=tmp_path):
                from gobby.llm.codex import CodexProvider
                provider = CodexProvider(codex_config)

                result = await provider.execute_code("console.log('hello')", language="javascript")

                assert result["success"] is False
                assert "not supported" in result["error"]
                assert result["language"] == "javascript"


class TestCodexProviderGetApiKey:
    """Tests for _get_api_key method."""

    def test_get_api_key_subscription_corrupt_json(self, codex_config: DaemonConfig, tmp_path: Path):
        """Test _get_api_key handles corrupt auth.json."""
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        auth_file = codex_dir / "auth.json"
        auth_file.write_text("not valid json{")

        with patch("gobby.llm.codex.shutil.which", return_value=None):
            with patch("gobby.llm.codex.Path.home", return_value=tmp_path):
                from gobby.llm.codex import CodexProvider
                provider = CodexProvider(codex_config)
                # Should handle corrupt JSON gracefully
                assert provider._client is None

    def test_get_api_key_subscription_missing_key(self, codex_config: DaemonConfig, tmp_path: Path):
        """Test _get_api_key handles auth.json without OPENAI_API_KEY."""
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        auth_file = codex_dir / "auth.json"
        auth_file.write_text(json.dumps({"other_key": "value"}))

        with patch("gobby.llm.codex.shutil.which", return_value=None):
            with patch("gobby.llm.codex.Path.home", return_value=tmp_path):
                from gobby.llm.codex import CodexProvider
                provider = CodexProvider(codex_config)
                # Should handle missing key gracefully
                assert provider._client is None
