"""Tests for the interactive embedding setup wizard in gobby install."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from gobby.cli._detectors import _is_lmstudio_available, _is_ollama_available
from gobby.cli._install_prompts import _run_embedding_install

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


class TestRunEmbeddingInstallNoInteractive:
    """Test auto-selection in non-interactive mode."""

    @patch("gobby.cli._detectors._is_ollama_available", return_value=False)
    @patch("gobby.cli._detectors._is_lmstudio_available", return_value=True)
    def test_autoselect_lmstudio_when_available(
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
        results: dict = {}
        provider = _run_embedding_install(installer, results, no_interactive=True)
        assert provider == "lmstudio"
        installer.assert_called_once_with(provider="lmstudio", openai_api_key=None)

    @patch("gobby.cli._detectors._is_ollama_available", return_value=True)
    @patch("gobby.cli._detectors._is_lmstudio_available", return_value=False)
    def test_autoselect_ollama_when_lmstudio_missing(
        self, mock_lms: MagicMock, mock_ollama: MagicMock
    ) -> None:
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
        installer = MagicMock(return_value={"success": True, "provider": "none"})
        results: dict = {}
        provider = _run_embedding_install(installer, results, no_interactive=True)
        assert provider == "none"
        # Still calls installer with none to persist the disable-semantic-search config
        installer.assert_called_once_with(provider="none")


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

        # "1\n" selects LM Studio (first option when detected)
        result = runner.invoke(cmd, input="1\n")
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
