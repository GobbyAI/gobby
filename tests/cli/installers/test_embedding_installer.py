"""Tests for the embedding provider installer."""

from __future__ import annotations

import subprocess
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.cli.installers.embedding import (
    _PROVIDER_CONFIG,
    _probe_embedding_dim,
    _setup_lmstudio,
    _setup_ollama,
    install_embedding,
)
from gobby.config.embedding_keys import (
    AI_EMBEDDING_API_BASE_KEY,
    AI_EMBEDDING_API_KEY_KEY,
    AI_EMBEDDING_DIM_KEY,
    AI_EMBEDDING_MODEL_KEY,
    EMBEDDING_API_KEY_SECRET_NAME,
)
from gobby.search.embeddings import EmbeddingGenerationError

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

    def test_openai_compatible_config(self) -> None:
        cfg = _PROVIDER_CONFIG["openai-compatible"]
        assert cfg["model"] == "text-embedding-3-small"
        assert cfg["api_base"] is None
        assert cfg["dim"] == 0

    def test_none_config_exists(self) -> None:
        assert "none" in _PROVIDER_CONFIG


class TestInstallEmbedding:
    """Top-level install_embedding entry point."""

    def test_unknown_provider_returns_error(self) -> None:
        result = install_embedding(provider="bogus")
        assert result["success"] is False
        assert "Unknown provider" in result["error"]

    def test_openai_without_key_returns_error(self) -> None:
        result = install_embedding(provider="openai", embedding_api_key=None)
        assert result["success"] is False
        assert "API key" in result["error"]

    def test_openai_compatible_without_url_returns_error(self) -> None:
        result = install_embedding(provider="openai-compatible")
        assert result["success"] is False
        assert "--embedding-url" in result["error"]

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
        result = install_embedding(provider="openai", embedding_api_key="sk-abc")
        assert result["success"] is True
        assert result["provider"] == "openai"
        assert result["model"] == "text-embedding-3-small"
        assert result["dim"] == 1536
        # Verify the key was passed through to persist
        call_kwargs = mock_persist.call_args.kwargs
        assert call_kwargs["embedding_api_key"] == "sk-abc"


class TestInstallEmbeddingOverrides:
    """Override behavior for --embedding-url / --embedding-model / --embedding-dim."""

    @patch("gobby.cli.installers.embedding._persist_embedding_config")
    @patch("gobby.cli.installers.embedding._health_check_embedding", return_value=True)
    @patch("gobby.cli.installers.embedding._setup_lmstudio")
    def test_explicit_overrides_skip_setup_and_persist(
        self, mock_setup: MagicMock, mock_health: MagicMock, mock_persist: MagicMock
    ) -> None:
        result = install_embedding(
            provider="lmstudio",
            model_override="text-embedding-qwen3-embedding-4b",
            api_base_override="http://192.168.1.10:1234/v1",
            dim_override=2560,
        )
        assert result["success"] is True
        assert result["model"] == "text-embedding-qwen3-embedding-4b"
        assert result["api_base"] == "http://192.168.1.10:1234/v1"
        assert result["dim"] == 2560
        # Custom model means we trust the user — skip the bundled LMS setup.
        mock_setup.assert_not_called()
        persisted = mock_persist.call_args.kwargs
        assert persisted["model"] == "text-embedding-qwen3-embedding-4b"
        assert persisted["api_base"] == "http://192.168.1.10:1234/v1"
        assert persisted["dim"] == 2560

    @patch("gobby.cli.installers.embedding._persist_embedding_config")
    @patch("gobby.cli.installers.embedding._health_check_embedding", return_value=True)
    @patch("gobby.cli.installers.embedding._probe_embedding_dim", return_value=768)
    @patch("gobby.cli.installers.embedding._setup_lmstudio")
    def test_api_base_override_skips_setup_and_uses_default_model(
        self,
        mock_setup: MagicMock,
        mock_probe: MagicMock,
        mock_health: MagicMock,
        mock_persist: MagicMock,
    ) -> None:
        result = install_embedding(
            provider="lmstudio",
            api_base_override="http://192.168.1.10:1234/v1",
        )

        assert result["success"] is True
        assert result["model"] == "text-embedding-nomic-embed-text-v1.5@f16"
        assert result["api_base"] == "http://192.168.1.10:1234/v1"
        mock_setup.assert_not_called()
        mock_probe.assert_called_once()
        persisted = mock_persist.call_args.kwargs
        assert persisted["model"] == "text-embedding-nomic-embed-text-v1.5@f16"
        assert persisted["api_base"] == "http://192.168.1.10:1234/v1"

    @patch("gobby.cli.installers.embedding._persist_embedding_config")
    @patch("gobby.cli.installers.embedding._health_check_embedding", return_value=True)
    @patch("gobby.cli.installers.embedding._probe_embedding_dim", return_value=2560)
    @patch("gobby.cli.installers.embedding._setup_lmstudio")
    def test_api_base_and_model_overrides_skip_setup_and_persist_values(
        self,
        mock_setup: MagicMock,
        mock_probe: MagicMock,
        mock_health: MagicMock,
        mock_persist: MagicMock,
    ) -> None:
        result = install_embedding(
            provider="lmstudio",
            model_override="text-embedding-qwen3-embedding-4b",
            api_base_override="http://192.168.1.10:1234/v1",
        )

        assert result["success"] is True
        assert result["model"] == "text-embedding-qwen3-embedding-4b"
        assert result["dim"] == 2560
        mock_setup.assert_not_called()
        mock_probe.assert_called_once()
        persisted = mock_persist.call_args.kwargs
        assert persisted["model"] == "text-embedding-qwen3-embedding-4b"
        assert persisted["api_base"] == "http://192.168.1.10:1234/v1"

    @patch("gobby.cli.installers.embedding._persist_embedding_config")
    @patch("gobby.cli.installers.embedding._health_check_embedding", return_value=True)
    @patch("gobby.cli.installers.embedding._probe_embedding_dim", return_value=2560)
    @patch("gobby.cli.installers.embedding._setup_lmstudio")
    def test_auto_detect_dim_via_probe(
        self,
        mock_setup: MagicMock,
        mock_probe: MagicMock,
        mock_health: MagicMock,
        mock_persist: MagicMock,
    ) -> None:
        result = install_embedding(
            provider="lmstudio",
            model_override="text-embedding-qwen3-embedding-4b",
            api_base_override="http://192.168.1.10:1234/v1",
        )
        assert result["success"] is True
        assert result["dim"] == 2560
        mock_probe.assert_called_once()
        probe_kwargs = mock_probe.call_args.kwargs
        assert probe_kwargs["model"] == "text-embedding-qwen3-embedding-4b"
        assert probe_kwargs["api_base"] == "http://192.168.1.10:1234/v1"

    @patch("gobby.cli.installers.embedding._setup_lmstudio")
    @patch("gobby.cli.installers.embedding._probe_embedding_dim", return_value=None)
    def test_probe_failure_returns_actionable_error(
        self, mock_probe: MagicMock, mock_setup: MagicMock
    ) -> None:
        result = install_embedding(
            provider="lmstudio",
            model_override="text-embedding-qwen3-embedding-4b",
            api_base_override="http://offline.local:9999/v1",
        )
        assert result["success"] is False
        assert "Could not probe embedding dim" in result["error"]
        assert "--embedding-dim" in result["error"]

    @patch("gobby.cli.installers.embedding._persist_embedding_config")
    @patch("gobby.cli.installers.embedding._health_check_embedding", return_value=True)
    @patch("gobby.cli.installers.embedding._probe_embedding_dim", return_value=None)
    @patch("gobby.cli.installers.embedding._setup_lmstudio")
    def test_provider_default_model_probe_failure_falls_back_to_default_dim(
        self,
        mock_setup: MagicMock,
        mock_probe: MagicMock,
        mock_health: MagicMock,
        mock_persist: MagicMock,
    ) -> None:
        result = install_embedding(
            provider="lmstudio",
            api_base_override="http://192.168.1.10:1234/v1",
        )

        assert result["success"] is True
        assert result["dim"] == 768
        mock_setup.assert_not_called()
        mock_probe.assert_called_once()
        assert mock_health.call_args.kwargs["expected_dim"] == 768
        assert mock_persist.call_args.kwargs["dim"] == 768

    @patch("gobby.cli.installers.embedding._persist_embedding_config")
    @patch("gobby.cli.installers.embedding._health_check_embedding", return_value=True)
    @patch("gobby.cli.installers.embedding._probe_embedding_dim", return_value=1536)
    def test_openai_compatible_custom_url_uses_probe(
        self,
        mock_probe: MagicMock,
        mock_health: MagicMock,
        mock_persist: MagicMock,
    ) -> None:
        result = install_embedding(
            provider="openai-compatible",
            api_base_override="https://embeddings.example.test/v1",
        )

        assert result["success"] is True
        assert result["provider"] == "openai-compatible"
        assert result["model"] == "text-embedding-3-small"
        assert result["dim"] == 1536
        mock_probe.assert_called_once()
        assert mock_health.call_args.kwargs["api_base"] == "https://embeddings.example.test/v1"
        assert mock_persist.call_args.kwargs["provider"] == "openai-compatible"

    @patch("gobby.cli.installers.embedding._probe_embedding_dim", return_value=None)
    def test_openai_compatible_probe_failure_requires_explicit_dim(
        self, mock_probe: MagicMock
    ) -> None:
        result = install_embedding(
            provider="openai-compatible",
            api_base_override="https://embeddings.example.test/v1",
        )

        assert result["success"] is False
        assert "Could not probe embedding dim" in result["error"]
        assert "--embedding-dim" in result["error"]

    @patch("gobby.cli.installers.embedding._persist_embedding_config")
    @patch("gobby.cli.installers.embedding._health_check_embedding", return_value=True)
    @patch("gobby.cli.installers.embedding._probe_embedding_dim")
    @patch("gobby.cli.installers.embedding._setup_lmstudio", return_value={"success": True})
    def test_defaults_preserved_when_no_overrides(
        self,
        mock_setup: MagicMock,
        mock_probe: MagicMock,
        mock_health: MagicMock,
        mock_persist: MagicMock,
    ) -> None:
        result = install_embedding(provider="lmstudio")
        assert result["success"] is True
        assert result["model"] == "text-embedding-nomic-embed-text-v1.5@f16"
        assert result["api_base"] == "http://localhost:1234/v1"
        assert result["dim"] == 768
        # No probe call when defaults apply.
        mock_probe.assert_not_called()
        # Setup IS called when running on the bundled defaults.
        mock_setup.assert_called_once()


class TestProbeEmbeddingDim:
    """Dim probe error boundaries."""

    def test_expected_embedding_failure_returns_none(self) -> None:
        mock_generate = AsyncMock(side_effect=EmbeddingGenerationError("offline"))

        with patch("gobby.search.embeddings.generate_embedding", mock_generate):
            assert _probe_embedding_dim(model="m", api_base="http://localhost:1234/v1") is None

    def test_unexpected_probe_exception_propagates(self) -> None:
        mock_generate = AsyncMock(side_effect=ValueError("bug"))

        with (
            patch("gobby.search.embeddings.generate_embedding", mock_generate),
            pytest.raises(ValueError, match="bug"),
        ):
            _probe_embedding_dim(model="m", api_base="http://localhost:1234/v1")

    @pytest.mark.asyncio
    async def test__probe_embedding_dim_returns_none_when_event_loop_running(self) -> None:
        mock_generate = AsyncMock()

        with patch("gobby.search.embeddings.generate_embedding", mock_generate):
            result = _probe_embedding_dim(model="m", api_base="http://localhost:1234/v1")

        assert result is None
        mock_generate.assert_not_called()


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
    @patch("gobby.storage.hub.runtime.open_runtime_hub_database")
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
        mock_config.database_url = str(tmp_path / "test.db")
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
        assert entries == {
            AI_EMBEDDING_MODEL_KEY: "text-embedding-nomic-embed-text-v1.5@f16",
            AI_EMBEDDING_API_BASE_KEY: "http://localhost:1234/v1",
            AI_EMBEDDING_DIM_KEY: 768,
        }
        # No duplicate namespaces
        assert not any(k.startswith("search.") for k in entries)
        assert not any(k.startswith("mcp_client_proxy.") for k in entries)
        mock_db.close.assert_called_once()

    @patch("gobby.storage.secrets.SecretStore")
    @patch("gobby.storage.config_store.ConfigStore")
    @patch("gobby.storage.hub.runtime.open_runtime_hub_database")
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
        mock_config.database_url = str(tmp_path / "test.db")
        mock_load_config.return_value = mock_config
        mock_db = mock_db_class.return_value
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_store = MagicMock()
        mock_store_class.return_value = mock_store

        _persist_embedding_config(model=None, api_base=None, dim=0, provider="none")

        entries = mock_store.set_many.call_args.args[0]
        assert entries == {
            AI_EMBEDDING_MODEL_KEY: None,
            AI_EMBEDDING_API_BASE_KEY: None,
            AI_EMBEDDING_DIM_KEY: 0,
        }
        mock_db.close.assert_called_once()
        assert mock_db.close.call_count == 1
        assert mock_db.close.call_args is not None

    def test_embedding_key_stored_with_config_secret(
        self,
        temp_db,
        tmp_path,
    ) -> None:
        from gobby.cli.installers.embedding import _persist_embedding_config
        from gobby.storage.config_store import ConfigStore
        from gobby.storage.secrets import SecretStore

        with (
            patch(
                "gobby.storage.hub.runtime.runtime_hub_database",
                return_value=nullcontext(temp_db),
            ),
            patch("gobby.storage.secrets.SALT_FILE", tmp_path / ".secret_salt"),
            patch("gobby.storage.secrets.get_machine_id", return_value="test-machine"),
        ):
            _persist_embedding_config(
                model="text-embedding-3-small",
                api_base=None,
                dim=1536,
                provider="openai",
                embedding_api_key="sk-xxx",
            )

            store = ConfigStore(temp_db)
            secret_store = SecretStore(temp_db)
            row = temp_db.fetchone(
                "SELECT encrypted_value FROM secrets WHERE name = %s",
                (EMBEDDING_API_KEY_SECRET_NAME,),
            )

            assert store.get(AI_EMBEDDING_API_KEY_KEY) == "$secret:embeddings_api_key"
            assert secret_store.get(EMBEDDING_API_KEY_SECRET_NAME) == "sk-xxx"
            assert row is not None
            assert row["encrypted_value"] != "sk-xxx"
            assert row["encrypted_value"].startswith("gAAAAA")

    @patch("gobby.storage.secrets.SecretStore")
    @patch("gobby.storage.config_store.ConfigStore")
    @patch("gobby.storage.hub.runtime.open_runtime_hub_database")
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
        mock_config.database_url = str(tmp_path / "test.db")
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
            embedding_api_key="sk-xxx",
        )

        entries = mock_store.set_many.call_args.args[0]
        assert entries == {
            AI_EMBEDDING_MODEL_KEY: "text-embedding-3-small",
            AI_EMBEDDING_API_BASE_KEY: None,
            AI_EMBEDDING_DIM_KEY: 1536,
        }
        mock_db.close.assert_called_once()
        assert mock_db.close.call_count == 1
        assert mock_db.close.call_args is not None


class TestHealthCheck:
    """Test _health_check_embedding behavior."""

    @patch("gobby.cli.installers.embedding.asyncio.run")
    def test_returns_true_on_success(self, mock_run: MagicMock) -> None:
        from gobby.cli.installers.embedding import _health_check_embedding

        def close_probe(coro: object) -> bool:
            # asyncio.run is mocked, so close the created coroutine to avoid an
            # unawaited-coroutine warning while still exercising the sync path.
            coro.close()
            return True

        mock_run.side_effect = close_probe
        assert _health_check_embedding("model", "http://x/v1") is True

    @pytest.mark.asyncio
    async def test_returns_false_in_running_event_loop(self) -> None:
        from gobby.cli.installers.embedding import _health_check_embedding

        mock_generate = AsyncMock()
        with patch("gobby.search.embeddings.generate_embedding", mock_generate):
            assert _health_check_embedding("model", "http://x/v1") is False
        mock_generate.assert_not_called()
