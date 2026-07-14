"""Tests for normalized hook terminal context."""

from unittest.mock import MagicMock, patch

import psutil
import pytest

from gobby.hooks.terminal_context import enrich_terminal_context_with_cwd, hook_cwd

pytestmark = pytest.mark.unit


def test_hook_cwd_rejects_whitespace_only_values() -> None:
    assert hook_cwd({"cwd": "   \t"}) is None


def test_hook_cwd_strips_nonblank_values() -> None:
    assert hook_cwd({"cwd": "  /repo/path  "}) == "/repo/path"


def test_enrichment_replaces_blank_terminal_cwd() -> None:
    with patch("gobby.hooks.terminal_context.psutil.Process") as process_cls:
        process = MagicMock()
        process.create_time.return_value = 123.5
        process.name.return_value = "codex"
        process_cls.return_value = process

        result = enrich_terminal_context_with_cwd(
            {"cwd": "  ", "parent_pid": 4321},
            " /repo/path ",
        )

    assert result == {
        "cwd": "/repo/path",
        "parent_pid": 4321,
        "parent_create_time": 123.5,
        "parent_name": "codex",
    }
    process_cls.assert_called_once_with(4321)


def test_enrichment_omits_parent_identity_when_process_is_gone() -> None:
    with patch(
        "gobby.hooks.terminal_context.psutil.Process",
        side_effect=psutil.NoSuchProcess(4321),
    ):
        result = enrich_terminal_context_with_cwd({"parent_pid": 4321}, None)

    assert result == {"parent_pid": 4321}
