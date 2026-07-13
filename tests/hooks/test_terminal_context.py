"""Tests for normalized hook terminal context."""

import pytest

from gobby.hooks.terminal_context import enrich_terminal_context_with_cwd, hook_cwd

pytestmark = pytest.mark.unit


def test_hook_cwd_rejects_whitespace_only_values() -> None:
    assert hook_cwd({"cwd": "   \t"}) is None


def test_hook_cwd_strips_nonblank_values() -> None:
    assert hook_cwd({"cwd": "  /repo/path  "}) == "/repo/path"


def test_enrichment_replaces_blank_terminal_cwd() -> None:
    result = enrich_terminal_context_with_cwd({"cwd": "  "}, " /repo/path ")

    assert result == {"cwd": "/repo/path"}
