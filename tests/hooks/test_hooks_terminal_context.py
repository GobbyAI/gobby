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
        result = enrich_terminal_context_with_cwd(
            {"parent_pid": 4321, "parent_create_time": 1.0, "tmux_pane": "%1"}, None
        )

    assert result == {"tmux_pane": "%1"}


def _process(
    pid: int, cmdline: list[str], create_time: float, parent: MagicMock | None
) -> MagicMock:
    process = MagicMock()
    process.pid = pid
    process.name.return_value = cmdline[0]
    process.cmdline.return_value = cmdline
    process.create_time.return_value = create_time
    process.parent.return_value = parent
    return process


@pytest.mark.parametrize(
    ("parent_cmdline", "expected_pid", "expected_create_time"),
    [
        (["droid", "--auto", "high"], 100, 1.5),
        (["zsh"], 4321, 9.5),
        (["droid", "exec", "--auto", "high"], 4321, 9.5),
    ],
    ids=["tui-backend", "headless-exec", "exec-under-exec"],
)
def test_droid_identity_is_the_process_that_survives_compress_and_clear(
    parent_cmdline: list[str], expected_pid: int, expected_create_time: float
) -> None:
    parent = _process(100, parent_cmdline, 1.5, None)
    backend = _process(4321, ["droid", "exec", "--input-format", "stream-jsonrpc"], 9.5, parent)
    with patch("gobby.hooks.terminal_context.psutil.Process", return_value=backend):
        result = enrich_terminal_context_with_cwd({"parent_pid": 4321}, None)

    assert result == {
        "parent_pid": expected_pid,
        "parent_create_time": expected_create_time,
        "parent_name": "droid",
    }
