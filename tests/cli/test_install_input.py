"""Tests for install command interactive-input diagnostics."""

from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from gobby.cli.install import install

pytestmark = pytest.mark.unit


def test_non_tty_install_fails_before_orchestration() -> None:
    detect_claude = MagicMock()
    peek_bootstrap = MagicMock()

    with (
        patch("gobby.cli.install._stdin_is_interactive", return_value=False),
        patch("gobby.cli.install._is_claude_code_installed", detect_claude),
        patch("gobby.cli.install.peek_install_bootstrap", peek_bootstrap),
    ):
        result = CliRunner().invoke(install)

    assert result.exit_code == 1
    assert "requires interactive input" in result.output
    assert "stdin is not a TTY or reached EOF" in result.output
    assert "gobby install --no-interactive" in result.output
    assert "gobby install claude codex git-hooks" in result.output
    detect_claude.assert_not_called()
    peek_bootstrap.assert_not_called()


def test_eof_during_install_prompt_reports_missing_input() -> None:
    def prompt_for_component(*_args: object, **_kwargs: object) -> None:
        click.confirm("Continue component install?")

    install_components = MagicMock(side_effect=prompt_for_component)

    with (
        patch("gobby.cli.install._stdin_is_interactive", return_value=True),
        patch("gobby.cli.install._install_components", install_components),
    ):
        result = CliRunner().invoke(install, ["claude"])

    assert result.exit_code == 1
    assert "stdin is not a TTY or reached EOF" in result.output
    assert "Aborted!" not in result.output


def test_interactive_install_behavior_is_unchanged() -> None:
    detect_claude = MagicMock(side_effect=click.ClickException("continued install"))

    with (
        patch("gobby.cli.install._stdin_is_interactive", return_value=True),
        patch("gobby.cli.install._is_claude_code_installed", detect_claude),
    ):
        result = CliRunner().invoke(install)

    assert result.exit_code == 1
    assert "continued install" in result.output
    detect_claude.assert_called_once_with()


def test_explicit_non_interactive_install_bypasses_tty_requirement() -> None:
    stdin_is_interactive = MagicMock(return_value=False)
    detect_claude = MagicMock(side_effect=click.ClickException("continued install"))

    with (
        patch("gobby.cli.install._stdin_is_interactive", stdin_is_interactive),
        patch("gobby.cli.install._is_claude_code_installed", detect_claude),
    ):
        result = CliRunner().invoke(install, ["--no-interactive"])

    assert result.exit_code == 1
    assert "continued install" in result.output
    stdin_is_interactive.assert_not_called()
    detect_claude.assert_called_once_with()
