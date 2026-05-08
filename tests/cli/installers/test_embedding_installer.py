"""Tests for the embedding provider installer."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from gobby.cli.installers.embedding import (
    _PROVIDER_CONFIG,
    _setup_lmstudio,
    _setup_ollama,
    install_embedding,
)

pytestmark = [pytest.mark.unit]


class TestProviderConfig:
    """Verify provider config table."""

    def test_lmstudio_config(self) -> None:
        cfg = _PROVIDER_CONFIG["lmstudio"]
        assert cfg["model"] == "text-embedding-nomic-embed-text-v1.5@f16"
        assert cfg["api_base"] == "http://localhost:1234/v1"
        assert cfg["dim"] == 768

    def test_ollama_config(self) -> None:
        cfg = _PROVIDER_CONFIG["ollama"]
        assert cfg["model"] == "nomic-embed-text"
        assert cfg["api_base"] == "http://localhost:11434/v1"
        assert cfg["dim"] == 768

    def test_openai_config(self) -> None:
        cfg = _PROVIDER_CONFIG["openai"]
        assert cfg["model"] == "text-embedding-3-small"
        assert cfg["api_base"] is None
        assert cfg["dim"] == 1536

    def test_none_config_exists(self) -> None:
        assert "none" in _PROVIDER_CONFIG


class TestInstallEmbedding:
    """Top-level install_embedding entry point."""

    def test_unknown_provider_returns_error(self) -> None:
        result = install_embedding(provider="bogus")
        assert result["success"] is False
        assert "Unknown provider" in result["error"]

    def test_openai_without_key_returns_error(self) -> None:
        result = install_embedding(provider="openai", openai_api_key=None)
        assert result["success"] is False
        assert "API key" in result["error"]

    @patch("gobby.cli.installers.embedding._persist_embedding_config")
    def test_none_provider_persists_and_succeeds(self, mock_persist: MagicMock) -> None:
        result = install_embedding(provider="none")
        assert result["success"] is True
        assert result["provider"] == "none"
        assert result["skipped"] is True
        mock_persist.assert_called_once()

    @patch("gobby.cli.installers.embedding._persist_embedding_config")
    @patch("gobby.cli.installers.embedding._health_check_embedding", return_value=True)
    @patch("gobby.cli.installers.embedding._setup_lmstudio", return_value={"success": True})
    def test_lmstudio_happy_path(
        self, mock_setup: MagicMock, mock_health: MagicMock, mock_persist: MagicMock
    ) -> None:
        result = install_embedding(provider="lmstudio")
        assert result["success"] is True
        assert result["provider"] == "lmstudio"
        assert result["model"] == "text-embedding-nomic-embed-text-v1.5@f16"
        assert result["dim"] == 768
        assert result["health_check"] is True
        mock_setup.assert_called_once()
        mock_persist.assert_called_once()

    @patch("gobby.cli.installers.embedding._setup_lmstudio")
    def test_lmstudio_setup_failure_propagates(self, mock_setup: MagicMock) -> None:
        mock_setup.return_value = {"success": False, "error": "lms not found"}
        result = install_embedding(provider="lmstudio")
        assert result["success"] is False
        assert result["error"] == "lms not found"

    @patch("gobby.cli.installers.embedding._persist_embedding_config")
    @patch("gobby.cli.installers.embedding._health_check_embedding", return_value=False)
    @patch("gobby.cli.installers.embedding._setup_lmstudio", return_value={"success": True})
    def test_health_check_failure_returns_error(
        self, mock_setup: MagicMock, mock_health: MagicMock, mock_persist: MagicMock
    ) -> None:
        result = install_embedding(provider="lmstudio")
        assert result["success"] is False
        assert "health check failed" in result["error"]
        mock_persist.assert_not_called()

    @patch("gobby.cli.installers.embedding._persist_embedding_config")
    @patch("gobby.cli.installers.embedding._health_check_embedding", return_value=True)
    def test_openai_with_key_skips_local_setup(
        self, mock_health: MagicMock, mock_persist: MagicMock
    ) -> None:
        result = install_embedding(provider="openai", openai_api_key="sk-abc")
        assert result["success"] is True
        assert result["provider"] == "openai"
        assert result["model"] == "text-embedding-3-small"
        assert result["dim"] == 1536
        # Verify the key was passed through to persist
        call_kwargs = mock_persist.call_args.kwargs
        assert call_kwargs["openai_api_key"] == "sk-abc"


class TestSetupLMStudio:
    """Test LM Studio setup subprocess orchestration."""

    @patch("gobby.cli.installers.embedding.shutil.which", return_value=None)
    def test_lms_not_installed(self, mock_which: MagicMock) -> None:
        result = _setup_lmstudio()
        assert result["success"] is False
        assert "lms CLI not found" in result["error"]

    @patch("gobby.cli.installers.embedding.subprocess.run")
    @patch("gobby.cli.installers.embedding.shutil.which", return_value="/usr/bin/lms")
    def test_already_loaded_short_circuits(
        self, mock_which: MagicMock, mock_run: MagicMock
    ) -> None:
        # server status -> running, ps -> includes nomic
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr="", stdout="The server is running on port 1234."),
            MagicMock(
                returncode=0,
                stderr="",
                stdout=(
                    "IDENTIFIER  MODEL\n"
                    "text-embedding-nomic-embed-text-v1.5@f16  "
                    "text-embedding-nomic-embed-text-v1.5@f16"
                ),
            ),
        ]
        result = _setup_lmstudio()
        assert result["success"] is True
        assert result["action"] == "already_loaded"
        assert mock_run.call_count == 2

    @patch("gobby.cli.installers.embedding.subprocess.run")
    @patch("gobby.cli.installers.embedding.shutil.which", return_value="/usr/bin/lms")
    def test_loads_from_disk_when_not_loaded(
        self, mock_which: MagicMock, mock_run: MagicMock
    ) -> None:
        # status -> running, ps -> no nomic, ls -> has nomic, load -> ok
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr="", stdout="running"),  # server status
            MagicMock(returncode=0, stderr="", stdout="(empty)"),  # ps
            MagicMock(returncode=0, stderr="", stdout="nomic-embed-text-v1.5  Local"),  # ls
            MagicMock(returncode=0, stderr="", stdout="Loaded"),  # load
        ]
        result = _setup_lmstudio()
        assert result["success"] is True
        assert result["action"] == "loaded"

    @patch("gobby.cli.installers.embedding.subprocess.run")
    @patch("gobby.cli.installers.embedding.shutil.which", return_value="/usr/bin/lms")
    def test_downloads_when_not_on_disk(self, mock_which: MagicMock, mock_run: MagicMock) -> None:
        # status -> running, ps -> empty, ls -> no nomic, get -> ok, load -> ok
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr="", stdout="running"),
            MagicMock(returncode=0, stderr="", stdout="(empty)"),
            MagicMock(returncode=0, stderr="", stdout="other-model"),  # no nomic
            MagicMock(returncode=0, stderr="", stdout="Downloaded"),  # get
            MagicMock(returncode=0, stderr="", stdout="Loaded"),  # load
        ]
        result = _setup_lmstudio()
        assert result["success"] is True
        assert mock_run.call_count == 5

    @patch("gobby.cli.installers.embedding.subprocess.run")
    @patch("gobby.cli.installers.embedding.shutil.which", return_value="/usr/bin/lms")
    def test_starts_server_when_not_running(
        self, mock_which: MagicMock, mock_run: MagicMock
    ) -> None:
        # status -> not running, start -> ok, ps -> loaded
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr="", stdout="stopped"),
            MagicMock(returncode=0, stderr="", stdout="started"),
            MagicMock(returncode=0, stderr="", stdout="text-embedding-nomic-embed-text-v1.5@f16"),
        ]
        result = _setup_lmstudio()
        assert result["success"] is True
        # status, start, ps
        assert mock_run.call_count == 3

    @patch("gobby.cli.installers.embedding.subprocess.run")
    @patch("gobby.cli.installers.embedding.shutil.which", return_value="/usr/bin/lms")
    def test_server_start_failure(self, mock_which: MagicMock, mock_run: MagicMock) -> None:
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr="", stdout="stopped"),
            MagicMock(returncode=1, stdout="", stderr="port in use"),
        ]
        result = _setup_lmstudio()
        assert result["success"] is False
        assert "Failed to start" in result["error"]

    @patch("gobby.cli.installers.embedding.subprocess.run")
    @patch("gobby.cli.installers.embedding.shutil.which", return_value="/usr/bin/lms")
    def test_get_timeout_returns_error(self, mock_which: MagicMock, mock_run: MagicMock) -> None:
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr="", stdout="running"),
            MagicMock(returncode=0, stderr="", stdout="(empty)"),
            MagicMock(returncode=0, stderr="", stdout="other-model"),
            subprocess.TimeoutExpired(cmd="lms get", timeout=600),
        ]
        result = _setup_lmstudio()
        assert result["success"] is False
        assert "timed out" in result["error"]


class TestSetupOllama:
    """Test Ollama setup."""

    @patch("gobby.cli.installers.embedding.shutil.which", return_value=None)
    def test_ollama_not_installed(self, mock_which: MagicMock) -> None:
        result = _setup_ollama()
        assert result["success"] is False
        assert "ollama not found" in result["error"]

    @patch("gobby.cli.installers.embedding.subprocess.run")
    @patch("gobby.cli.installers.embedding.shutil.which", return_value="/usr/bin/ollama")
    def test_already_pulled(self, mock_which: MagicMock, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stdout="NAME\nnomic-embed-text  274 MB\n")
        result = _setup_ollama()
        assert result["success"] is True
        assert result["action"] == "already_pulled"
        assert mock_run.call_count == 1

    @patch("gobby.cli.installers.embedding.subprocess.run")
    @patch("gobby.cli.installers.embedding.shutil.which", return_value="/usr/bin/ollama")
    def test_pulls_if_missing(self, mock_which: MagicMock, mock_run: MagicMock) -> None:
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr="", stdout="other-model  1 GB"),  # list, no nomic
            MagicMock(returncode=0, stderr="", stdout="pulling...done"),  # pull
        ]
        result = _setup_ollama()
        assert result["success"] is True
        assert result["action"] == "pulled"

    @patch("gobby.cli.installers.embedding.subprocess.run")
    @patch("gobby.cli.installers.embedding.shutil.which", return_value="/usr/bin/ollama")
    def test_pull_failure(self, mock_which: MagicMock, mock_run: MagicMock) -> None:
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr="", stdout=""),
            MagicMock(returncode=1, stdout="", stderr="connection refused"),
        ]
        result = _setup_ollama()
        assert result["success"] is False
        assert "ollama pull failed" in result["error"]


class TestPersistEmbeddingConfig:
    """Test config persistence writes to all three namespaces."""

    @patch("gobby.storage.secrets.SecretStore")
    @patch("gobby.storage.config_store.ConfigStore")
    @patch("gobby.storage.database.LocalDatabase")
    @patch("gobby.config.app.load_config")
    def test_persists_embeddings_namespace_only(
        self,
        mock_load_config: MagicMock,
        mock_db_class: MagicMock,
        mock_store_class: MagicMock,
        mock_secret_class: MagicMock,
        tmp_path,
    ) -> None:
        from gobby.cli.installers.embedding import _persist_embedding_config

        mock_config = MagicMock()
        mock_config.database_path = str(tmp_path / "test.db")
        mock_load_config.return_value = mock_config

        mock_db = MagicMock()
        mock_db_class.return_value = mock_db
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_store = MagicMock()
        mock_store_class.return_value = mock_store

        _persist_embedding_config(
            model="text-embedding-nomic-embed-text-v1.5@f16",
            api_base="http://localhost:1234/v1",
            dim=768,
            provider="lmstudio",
        )

        mock_store.set_many.assert_called_once()
        entries = mock_store.set_many.call_args.args[0]
        # Only unified embeddings.* namespace
        assert entries == {
            "embeddings.model": "text-embedding-nomic-embed-text-v1.5@f16",
            "embeddings.api_base": "http://localhost:1234/v1",
            "embeddings.dim": 768,
        }
        # No duplicate namespaces
        assert not any(k.startswith("search.") for k in entries)
        assert not any(k.startswith("mcp_client_proxy.") for k in entries)
        mock_db.__exit__.assert_called_once()

    @patch("gobby.storage.secrets.SecretStore")
    @patch("gobby.storage.config_store.ConfigStore")
    @patch("gobby.storage.database.LocalDatabase")
    @patch("gobby.config.app.load_config")
    def test_none_provider_clears_endpoints(
        self,
        mock_load_config: MagicMock,
        mock_db_class: MagicMock,
        mock_store_class: MagicMock,
        mock_secret_class: MagicMock,
        tmp_path,
    ) -> None:
        from gobby.cli.installers.embedding import _persist_embedding_config

        mock_config = MagicMock()
        mock_config.database_path = str(tmp_path / "test.db")
        mock_load_config.return_value = mock_config
        mock_db = mock_db_class.return_value
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_store = MagicMock()
        mock_store_class.return_value = mock_store

        _persist_embedding_config(model=None, api_base=None, dim=0, provider="none")

        entries = mock_store.set_many.call_args.args[0]
        assert entries == {
            "embeddings.model": None,
            "embeddings.api_base": None,
            "embeddings.dim": 0,
        }
        mock_db.__exit__.assert_called_once()
        assert mock_db.__exit__.call_count == 1
        assert mock_db.__exit__.call_args is not None

    @patch("gobby.storage.secrets.SecretStore")
    @patch("gobby.storage.config_store.ConfigStore")
    @patch("gobby.storage.database.LocalDatabase")
    @patch("gobby.config.app.load_config")
    def test_openai_key_stored_in_secrets(
        self,
        mock_load_config: MagicMock,
        mock_db_class: MagicMock,
        mock_store_class: MagicMock,
        mock_secret_class: MagicMock,
        tmp_path,
    ) -> None:
        from gobby.cli.installers.embedding import _persist_embedding_config

        mock_config = MagicMock()
        mock_config.database_path = str(tmp_path / "test.db")
        mock_load_config.return_value = mock_config
        mock_db = mock_db_class.return_value
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_secret = MagicMock()
        mock_secret_class.return_value = mock_secret

        _persist_embedding_config(
            model="text-embedding-3-small",
            api_base=None,
            dim=1536,
            provider="openai",
            openai_api_key="sk-xxx",
        )

        mock_secret.set.assert_called_once()
        call_kwargs = mock_secret.set.call_args.kwargs
        assert call_kwargs["name"] == "openai_api_key"
        assert call_kwargs["plaintext_value"] == "sk-xxx"
        assert call_kwargs["category"] == "llm"
        mock_db.__exit__.assert_called_once()

    @patch("gobby.storage.secrets.SecretStore")
    @patch("gobby.storage.config_store.ConfigStore")
    @patch("gobby.storage.database.LocalDatabase")
    @patch("gobby.config.app.load_config")
    def test_openai_provider_uses_unified_namespace(
        self,
        mock_load_config: MagicMock,
        mock_db_class: MagicMock,
        mock_store_class: MagicMock,
        mock_secret_class: MagicMock,
        tmp_path,
    ) -> None:
        from gobby.cli.installers.embedding import _persist_embedding_config

        mock_config = MagicMock()
        mock_config.database_path = str(tmp_path / "test.db")
        mock_load_config.return_value = mock_config
        mock_db = mock_db_class.return_value
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_store = MagicMock()
        mock_store_class.return_value = mock_store

        _persist_embedding_config(
            model="text-embedding-3-small",
            api_base=None,
            dim=1536,
            provider="openai",
            openai_api_key="sk-xxx",
        )

        entries = mock_store.set_many.call_args.args[0]
        assert entries == {
            "embeddings.model": "text-embedding-3-small",
            "embeddings.api_base": None,
            "embeddings.dim": 1536,
        }
        mock_db.__exit__.assert_called_once()
        assert mock_db.__exit__.call_count == 1
        assert mock_db.__exit__.call_args is not None


class TestHealthCheck:
    """Test _health_check_embedding behavior."""

    @patch("gobby.cli.installers.embedding.asyncio.run")
    def test_returns_true_on_success(self, mock_run: MagicMock) -> None:
        from gobby.cli.installers.embedding import _health_check_embedding

        mock_run.return_value = True
        assert _health_check_embedding("model", "http://x/v1") is True

    @patch("gobby.cli.installers.embedding.asyncio.run")
    def test_returns_false_on_runtime_error(self, mock_run: MagicMock) -> None:
        from gobby.cli.installers.embedding import _health_check_embedding

        mock_run.side_effect = RuntimeError(
            "asyncio.run() cannot be called from a running event loop"
        )
        assert _health_check_embedding("model", "http://x/v1") is False
