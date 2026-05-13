"""Tests for CLI __main__ module."""

import pytest

pytestmark = pytest.mark.unit


def test_cli_main_import() -> None:
    """Test that cli.__main__ module can be imported."""
    # This tests the entry point module loads correctly
    from gobby.cli import __main__

    # Verify cli is accessible
    assert hasattr(__main__, "cli")


def test_cli_main_cli_callable() -> None:
    """Test that the cli object is callable."""
    from gobby.cli.__main__ import cli

    assert callable(cli)
