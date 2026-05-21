"""Tests for the interactive embedding setup wizard in gobby install."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from gobby.cli._detectors import _is_lmstudio_available, _is_ollama_available
from gobby.cli._install_embedding_prompts import _get_openai_key
from gobby.cli._install_prompts import _infer_embedding_provider_from_url, _run_embedding_install

pytestmark = [pytest.mark.unit]


class TestDetectors:
    """Test LM Studio and Ollama detection helpers."""

    @patch("gobby.cli._detectors.shutil.which", return_value=None)
    def test_lmstudio_not_on_path(self, mock_which: MagicMock) -> None:
        assert _is_lmstudio_available() is False

    @patch("gobby.cli._detectors.subprocess.run")
    @patch("gobby.cli._detectors.shutil.which", return_value="/usr/bin/lms")
    def test_lmstudio_running(self, mock_which: MagicMock, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(
            returncode=0, stderr="", stdout="The server is running on port 1234."
        )
        assert _is_lmstudio_available() is True

    @patch("gobby.cli._detectors.subprocess.run")
    @patch("gobby.cli._detectors.shutil.which", return_value="/usr/bin/lms")
    def test_lmstudio_not_running(self, mock_which: MagicMock, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="Server stopped.")
        assert _is_lmstudio_available() is False

    @patch("gobby.cli._detectors.subprocess.run")
    @patch("gobby.cli._detectors.shutil.which", return_value="/usr/bin/lms")
    def test_lmstudio_timeout(self, mock_which: MagicMock, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="lms", timeout=5)
        assert _is_lmstudio_available() is False

    @patch("gobby.cli._detectors.shutil.which", return_value=None)
    def test_ollama_not_on_path(self, mock_which: MagicMock) -> None:
        assert _is_ollama_available() is False

    @patch("gobby.cli._detectors.subprocess.run")
    @patch("gobby.cli._detectors.shutil.which", return_value="/usr/bin/ollama")
    def test_ollama_running(self, mock_which: MagicMock, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="NAME\nmodel1")
        assert _is_ollama_available() is True

    @patch("gobby.cli._detectors.subprocess.run")
    @patch("gobby.cli._detectors.shutil.which", return_value="/usr/bin/ollama")
    def test_ollama_error(self, mock_which: MagicMock, mock_run: MagicMock) -> None:
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert _is_ollama_available() is False


class TestEmbeddingProviderInference:
    """Test custom URL provider inference."""

    @pytest.mark.parametrize(
        ("api_base", "provider"),
        [
            ("http://lan:1234/v1", "lmstudio"),
            ("http://custom-host:11434/v1", "ollama"),
            ("https://embeddings.example.test/v1", "openai-compatible"),
        ],
    )
    def test_custom_url_provider_inference(self, api_base: str, provider: str) -> None:
        """Infer provider from known embedding endpoint ports."""
        assert _infer_embedding_provider_from_url(api_base) == provider


class TestRunEmbeddingInstallNoInteractive:
    """Test auto-selection in non-interactive mode."""

    @patch("gobby.cli._detectors._is_ollama_available", return_value=False)
    @patch("gobby.cli._detectors._is_lmstudio_available", return_value=True)
    def test_autoselect_lmstudio_when_available(
        self, mock_lms: MagicMock, mock_ollama: MagicMock
    ) -> None:
        """Prefer LM Studio when it is the only local provider available."""
        installer = MagicMock(
            return_value={
                "success": True,
                "provider": "lmstudio",
                "model": "text-embedding-nomic-embed-text-v1.5@f16",
                "dim": 768,
                "api_base": "http://localhost:1234/v1",
                "health_check": True,
            }
        )
        results: dict = {}
        provider = _run_embedding_install(installer, results, no_interactive=True)
        assert provider == "lmstudio"
        installer.assert_called_once_with(
            provider="lmstudio",
            openai_api_key=None,
            model_override=None,
            api_base_override=None,
            dim_override=None,
        )

    @patch("gobby.cli._detectors._is_ollama_available", return_value=True)
    @patch("gobby.cli._detectors._is_lmstudio_available", return_value=False)
    def test_autoselect_ollama_when_lmstudio_missing(
        self, mock_lms: MagicMock, mock_ollama: MagicMock
    ) -> None:
        """Select Ollama when LM Studio is unavailable."""
        installer = MagicMock(
            return_value={
                "success": True,
                "provider": "ollama",
                "model": "nomic-embed-text",
                "dim": 768,
                "api_base": "http://localhost:11434/v1",
                "health_check": True,
            }
        )
        results: dict = {}
        provider = _run_embedding_install(installer, results, no_interactive=True)
        assert provider == "ollama"

    @patch("gobby.cli._detectors._is_ollama_available", return_value=False)
    @patch("gobby.cli._detectors._is_lmstudio_available", return_value=False)
    def test_autoselect_skips_when_no_local(
        self, mock_lms: MagicMock, mock_ollama: MagicMock
    ) -> None:
        """Disable semantic search when no local embedding provider is available."""
        installer = MagicMock(return_value={"success": True, "provider": "none"})
        results: dict = {}
        provider = _run_embedding_install(installer, results, no_interactive=True)
        assert provider == "none"
        # Still calls installer with none to persist the disable-semantic-search config
        installer.assert_called_once_with(provider="none")
        assert results["embedding"] == {"success": True, "provider": "none"}

    @patch("gobby.cli._detectors._is_ollama_available", return_value=False)
    @patch("gobby.cli._detectors._is_lmstudio_available", return_value=False)
    def test_autoselect_none_records_invalid_installer_result_shape(
        self,
        mock_lms: MagicMock,
        mock_ollama: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        installer = MagicMock(return_value=["not", "a", "dict"])
        results: dict = {}

        with caplog.at_level("WARNING", logger="gobby.cli._install_embedding_prompts"):
            provider = _run_embedding_install(installer, results, no_interactive=True)

        assert provider == "none"
        assert results["embedding"] == {
            "success": False,
            "error": "Embedding installer returned invalid result shape: list",
        }
        assert "Embedding installer returned invalid result shape: list" in caplog.text
        installer.assert_called_once_with(provider="none")

    @patch("gobby.cli._detectors._is_ollama_available", return_value=False)
    @patch("gobby.cli._detectors._is_lmstudio_available", return_value=False)
    def test_custom_url_infers_lmstudio_without_local_provider(
        self, mock_lms: MagicMock, mock_ollama: MagicMock
    ) -> None:
        """Port 1234 selects LM Studio even when the local CLI is unavailable."""
        installer = MagicMock(
            return_value={
                "success": True,
                "provider": "lmstudio",
                "model": "text-embedding-qwen3-embedding-4b",
                "dim": 2560,
                "api_base": "http://lan:1234/v1",
                "health_check": True,
            }
        )
        results: dict = {}

        provider = _run_embedding_install(
            installer,
            results,
            no_interactive=True,
            api_base_override="http://lan:1234/v1",
            model_override="text-embedding-qwen3-embedding-4b",
            dim_override=2560,
        )

        assert provider == "lmstudio"
        installer.assert_called_once_with(
            provider="lmstudio",
            openai_api_key=None,
            model_override="text-embedding-qwen3-embedding-4b",
            api_base_override="http://lan:1234/v1",
            dim_override=2560,
        )

    @patch("gobby.cli._detectors._is_ollama_available", return_value=False)
    @patch("gobby.cli._detectors._is_lmstudio_available", return_value=False)
    def test_custom_ollama_url_infers_ollama_without_local_provider(
        self, mock_lms: MagicMock, mock_ollama: MagicMock
    ) -> None:
        """Port 11434 selects Ollama even when the local CLI is unavailable."""
        installer = MagicMock(
            return_value={
                "success": True,
                "provider": "ollama",
                "model": "nomic-embed-text",
                "dim": 768,
                "api_base": "http://custom-host:11434/v1",
                "health_check": True,
            }
        )
        results: dict = {}

        provider = _run_embedding_install(
            installer,
            results,
            no_interactive=True,
            api_base_override="http://custom-host:11434/v1",
            dim_override=768,
        )

        assert provider == "ollama"
        installer.assert_called_once_with(
            provider="ollama",
            openai_api_key=None,
            model_override=None,
            api_base_override="http://custom-host:11434/v1",
            dim_override=768,
        )

    @patch("gobby.cli._detectors._is_ollama_available", return_value=False)
    @patch("gobby.cli._detectors._is_lmstudio_available", return_value=False)
    def test_custom_unknown_url_infers_openai_compatible(
        self, mock_lms: MagicMock, mock_ollama: MagicMock
    ) -> None:
        """Unknown custom endpoint ports use OpenAI-compatible routing."""
        installer = MagicMock(
            return_value={
                "success": True,
                "provider": "openai-compatible",
                "model": "vendor-embed",
                "dim": 1024,
                "api_base": "https://embeddings.example.test/v1",
                "health_check": True,
            }
        )
        results: dict = {}

        provider = _run_embedding_install(
            installer,
            results,
            no_interactive=True,
            api_base_override="https://embeddings.example.test/v1",
            model_override="vendor-embed",
            dim_override=1024,
        )

        assert provider == "openai-compatible"
        installer.assert_called_once_with(
            provider="openai-compatible",
            openai_api_key=None,
            model_override="vendor-embed",
            api_base_override="https://embeddings.example.test/v1",
            dim_override=1024,
        )

    @patch("gobby.cli._detectors._is_ollama_available", return_value=False)
    @patch("gobby.cli._detectors._is_lmstudio_available", return_value=False)
    def test_custom_url_explicit_provider_override_wins(
        self, mock_lms: MagicMock, mock_ollama: MagicMock
    ) -> None:
        installer = MagicMock(
            return_value={
                "success": True,
                "provider": "lmstudio",
                "model": "text-embedding-qwen3-embedding-4b",
                "dim": 2560,
                "api_base": "http://lan:9999/v1",
                "health_check": True,
            }
        )
        results: dict = {}

        provider = _run_embedding_install(
            installer,
            results,
            no_interactive=True,
            api_base_override="http://lan:9999/v1",
            model_override="text-embedding-qwen3-embedding-4b",
            dim_override=2560,
            provider_override="lmstudio",
        )

        assert provider == "lmstudio"
        assert installer.call_args.kwargs["provider"] == "lmstudio"

    @patch("gobby.cli._detectors._is_ollama_available", return_value=False)
    @patch("gobby.cli._detectors._is_lmstudio_available", return_value=False)
    def test_installer_exception_records_failure_result(
        self, mock_lms: MagicMock, mock_ollama: MagicMock
    ) -> None:
        installer = MagicMock(side_effect=RuntimeError("boom"))
        results: dict = {}

        provider = _run_embedding_install(
            installer,
            results,
            no_interactive=True,
            api_base_override="http://lan:1234/v1",
            provider_override="lmstudio",
        )

        assert provider == "lmstudio"
        assert results["embedding"]["success"] is False
        assert results["embedding"]["provider"] == "lmstudio"
        assert "boom" in results["embedding"]["error"]

    @patch("gobby.cli._detectors._is_ollama_available", return_value=False)
    @patch("gobby.cli._detectors._is_lmstudio_available", return_value=False)
    def test_subprocess_exception_records_failure_result(
        self, mock_lms: MagicMock, mock_ollama: MagicMock
    ) -> None:
        installer = MagicMock(side_effect=subprocess.SubprocessError("boom"))
        results: dict = {}

        provider = _run_embedding_install(
            installer,
            results,
            no_interactive=True,
            api_base_override="http://lan:1234/v1",
            provider_override="lmstudio",
        )

        assert provider == "lmstudio"
        assert results["embedding"]["success"] is False
        assert results["embedding"]["provider"] == "lmstudio"
        assert "boom" in results["embedding"]["error"]

    @patch("gobby.cli._detectors._is_ollama_available", return_value=False)
    @patch("gobby.cli._detectors._is_lmstudio_available", return_value=False)
    def test_installer_type_error_propagates(
        self, mock_lms: MagicMock, mock_ollama: MagicMock
    ) -> None:
        installer = MagicMock(side_effect=TypeError("programming bug"))

        with pytest.raises(TypeError, match="programming bug"):
            _run_embedding_install(
                installer,
                {},
                no_interactive=True,
                provider_override="lmstudio",
            )

    @patch("gobby.cli._detectors._is_ollama_available", return_value=False)
    @patch("gobby.cli._detectors._is_lmstudio_available", return_value=False)
    def test_openai_missing_key_returns_openai_without_installing(
        self, mock_lms: MagicMock, mock_ollama: MagicMock
    ) -> None:
        installer = MagicMock()
        results: dict = {}

        with patch(
            "gobby.cli._install_embedding_prompts._get_openai_key",
            return_value=None,
        ) as mock_get_key:
            provider = _run_embedding_install(
                installer,
                results,
                no_interactive=True,
                provider_override="openai",
            )

        assert provider == "openai"
        mock_get_key.assert_called_once_with(no_interactive=True, results=results)
        installer.assert_not_called()


class TestRunEmbeddingInstallInteractive:
    """Test interactive menu flow."""

    @patch("gobby.cli._detectors._is_ollama_available", return_value=False)
    @patch("gobby.cli._detectors._is_lmstudio_available", return_value=True)
    def test_interactive_choose_lmstudio(self, mock_lms: MagicMock, mock_ollama: MagicMock) -> None:
        runner = CliRunner()
        installer = MagicMock(
            return_value={
                "success": True,
                "provider": "lmstudio",
                "model": "text-embedding-nomic-embed-text-v1.5@f16",
                "dim": 768,
                "api_base": "http://localhost:1234/v1",
                "health_check": True,
            }
        )

        @click.command()
        def cmd() -> None:
            results: dict = {}
            provider = _run_embedding_install(installer, results, no_interactive=False)
            click.echo(f"CHOSE={provider}")

        # "1\n" selects LM Studio; "\n" accepts the default-no on customize.
        result = runner.invoke(cmd, input="1\n\n")
        assert result.exit_code == 0
        assert "CHOSE=lmstudio" in result.output

    @patch("gobby.cli._detectors._is_ollama_available", return_value=False)
    @patch("gobby.cli._detectors._is_lmstudio_available", return_value=False)
    def test_interactive_choose_none_default(
        self, mock_lms: MagicMock, mock_ollama: MagicMock
    ) -> None:
        runner = CliRunner()
        installer = MagicMock(return_value={"success": True, "provider": "none", "skipped": True})

        @click.command()
        def cmd() -> None:
            results: dict = {}
            provider = _run_embedding_install(installer, results, no_interactive=False)
            click.echo(f"CHOSE={provider}")

        # Just press enter — default is "none" when no local providers
        # Menu will be: [1] OpenAI, [2] None
        result = runner.invoke(cmd, input="\n")
        assert result.exit_code == 0
        assert "CHOSE=none" in result.output

    @patch("gobby.cli._detectors._is_ollama_available", return_value=False)
    @patch("gobby.cli._detectors._is_lmstudio_available", return_value=True)
    def test_interactive_abort_returns_none(
        self, mock_lms: MagicMock, mock_ollama: MagicMock
    ) -> None:
        runner = CliRunner()
        installer = MagicMock()

        @click.command()
        def cmd() -> None:
            results: dict = {}
            provider = _run_embedding_install(installer, results, no_interactive=False)
            click.echo(f"CHOSE={provider}")

        # No input — CliRunner returns EOFError which triggers the abort path
        result = runner.invoke(cmd, input="")
        assert "CHOSE=none" in result.output
        installer.assert_not_called()


class TestRunEmbeddingInstallOverrides:
    """CLI flag and interactive customize behavior."""

    @patch("gobby.cli._detectors._is_ollama_available", return_value=False)
    @patch("gobby.cli._detectors._is_lmstudio_available", return_value=True)
    def test_cli_overrides_skip_customize_prompt(
        self, mock_lms: MagicMock, mock_ollama: MagicMock
    ) -> None:
        installer = MagicMock(
            return_value={
                "success": True,
                "provider": "lmstudio",
                "model": "text-embedding-qwen3-embedding-4b",
                "dim": 2560,
                "api_base": "http://lan:1234/v1",
                "health_check": True,
            }
        )
        runner = CliRunner()

        @click.command()
        def cmd() -> None:
            results: dict = {}
            _run_embedding_install(
                installer,
                results,
                no_interactive=False,
                api_base_override="http://lan:1234/v1",
                model_override="text-embedding-qwen3-embedding-4b",
                dim_override=2560,
            )

        # "1\n" selects lmstudio. No customize prompt should fire because CLI
        # overrides were passed, so a single confirm/abort character would
        # itself end the test — proving the prompt did not appear.
        result = runner.invoke(cmd, input="1\n")
        assert result.exit_code == 0
        installer.assert_called_once_with(
            provider="lmstudio",
            openai_api_key=None,
            model_override="text-embedding-qwen3-embedding-4b",
            api_base_override="http://lan:1234/v1",
            dim_override=2560,
        )

    @patch("gobby.cli._detectors._is_ollama_available", return_value=False)
    @patch("gobby.cli._detectors._is_lmstudio_available", return_value=True)
    def test_provider_override_skips_customize_prompt(
        self, mock_lms: MagicMock, mock_ollama: MagicMock
    ) -> None:
        installer = MagicMock(
            return_value={
                "success": True,
                "provider": "lmstudio",
                "model": "text-embedding-nomic-embed-text-v1.5@f16",
                "dim": 768,
                "api_base": "http://localhost:1234/v1",
                "health_check": True,
            }
        )
        runner = CliRunner()

        @click.command()
        def cmd() -> None:
            results: dict = {}
            _run_embedding_install(
                installer,
                results,
                no_interactive=False,
                provider_override="lmstudio",
            )

        result = runner.invoke(cmd, input="")

        assert result.exit_code == 0
        assert "Customize endpoint URL" not in result.output
        installer.assert_called_once_with(
            provider="lmstudio",
            openai_api_key=None,
            model_override=None,
            api_base_override=None,
            dim_override=None,
        )

    @patch("gobby.cli._detectors._is_ollama_available", return_value=False)
    @patch("gobby.cli._detectors._is_lmstudio_available", return_value=True)
    def test_interactive_customize_collects_overrides(
        self, mock_lms: MagicMock, mock_ollama: MagicMock
    ) -> None:
        installer = MagicMock(
            return_value={
                "success": True,
                "provider": "lmstudio",
                "model": "text-embedding-qwen3-embedding-4b",
                "dim": 2560,
                "api_base": "http://192.168.1.10:1234/v1",
                "health_check": True,
            }
        )
        runner = CliRunner()

        @click.command()
        def cmd() -> None:
            results: dict = {}
            _run_embedding_install(installer, results, no_interactive=False)

        # 1 → lmstudio; y → customize; URL; model; dim
        user_input = "1\ny\nhttp://192.168.1.10:1234/v1\ntext-embedding-qwen3-embedding-4b\n2560\n"
        result = runner.invoke(cmd, input=user_input)
        assert result.exit_code == 0
        kwargs = installer.call_args.kwargs
        assert kwargs["api_base_override"] == "http://192.168.1.10:1234/v1"
        assert kwargs["model_override"] == "text-embedding-qwen3-embedding-4b"
        assert kwargs["dim_override"] == 2560

    @patch("gobby.cli._detectors._is_ollama_available", return_value=False)
    @patch("gobby.cli._detectors._is_lmstudio_available", return_value=True)
    def test_interactive_customize_blank_dim_means_auto_detect(
        self, mock_lms: MagicMock, mock_ollama: MagicMock
    ) -> None:
        installer = MagicMock(
            return_value={
                "success": True,
                "provider": "lmstudio",
                "model": "text-embedding-qwen3-embedding-4b",
                "dim": 2560,
                "api_base": "http://192.168.1.10:1234/v1",
                "health_check": True,
            }
        )
        runner = CliRunner()

        @click.command()
        def cmd() -> None:
            results: dict = {}
            _run_embedding_install(installer, results, no_interactive=False)

        # Customize: URL + model, blank dim → installer is asked to probe.
        user_input = "1\ny\nhttp://192.168.1.10:1234/v1\ntext-embedding-qwen3-embedding-4b\n\n"
        result = runner.invoke(cmd, input=user_input)
        assert result.exit_code == 0
        kwargs = installer.call_args.kwargs
        assert kwargs["dim_override"] is None
        assert kwargs["model_override"] == "text-embedding-qwen3-embedding-4b"

    @pytest.mark.parametrize("dim_input", ["0", "-1", "abc"])
    @patch("gobby.cli._detectors._is_ollama_available", return_value=False)
    @patch("gobby.cli._detectors._is_lmstudio_available", return_value=True)
    def test_interactive_customize_rejects_non_positive_dim(
        self,
        mock_lms: MagicMock,
        mock_ollama: MagicMock,
        dim_input: str,
    ) -> None:
        installer = MagicMock(
            return_value={
                "success": True,
                "provider": "lmstudio",
                "model": "text-embedding-qwen3-embedding-4b",
                "dim": 2560,
                "api_base": "http://192.168.1.10:1234/v1",
                "health_check": True,
            }
        )
        runner = CliRunner()

        @click.command()
        def cmd() -> None:
            results: dict = {}
            _run_embedding_install(installer, results, no_interactive=False)

        user_input = (
            f"1\ny\nhttp://192.168.1.10:1234/v1\ntext-embedding-qwen3-embedding-4b\n{dim_input}\n"
        )
        result = runner.invoke(cmd, input=user_input)

        assert result.exit_code == 0
        assert f"Invalid dim '{dim_input}'; will auto-detect" in result.output
        assert installer.call_args.kwargs["dim_override"] is None

    @patch("gobby.cli._detectors._is_ollama_available", return_value=False)
    @patch("gobby.cli._detectors._is_lmstudio_available", return_value=True)
    def test_invalid_embedding_installer_result_shape_records_failure(
        self, mock_lms: MagicMock, mock_ollama: MagicMock
    ) -> None:
        installer = MagicMock(return_value=["not", "a", "dict"])
        runner = CliRunner()

        @click.command()
        def cmd() -> None:
            results: dict = {}
            provider = _run_embedding_install(installer, results, no_interactive=False)
            click.echo(f"provider={provider}")
            click.echo(f"stored={results['embedding']['success']}")

        result = runner.invoke(cmd, input="1\nn\n")

        assert result.exit_code == 0
        assert "Embedding installer returned invalid result shape: list" in result.output
        assert "stored=False" in result.output


class TestOpenAIKeyLookup:
    """OpenAI key lookup should only suppress expected storage failures."""

    @patch("gobby.storage.hub.runtime.runtime_hub_database", side_effect=RuntimeError("bug"))
    def test_runtime_hub_error_skips_noninteractive(self, mock_database: MagicMock) -> None:
        results: dict[str, dict[str, object]] = {}

        key = _get_openai_key(no_interactive=True, results=results)

        assert key is None
        assert results["embedding"] == {
            "success": False,
            "error": "OpenAI API key not available",
        }
