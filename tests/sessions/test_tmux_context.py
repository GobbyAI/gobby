from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from gobby.config.tmux import TmuxConfig
from gobby.sessions.tmux_context import (
    get_tmux_window_id,
    is_configured_tmux_socket,
    query_tmux_generation,
    query_tmux_identity,
)

pytestmark = pytest.mark.unit


def test_configured_socket_classifies_path_basename() -> None:
    config = TmuxConfig(socket_name="agent-socket")

    assert (
        is_configured_tmux_socket(
            {"tmux_socket_path": "/tmp/tmux-501/agent-socket"},
            config=config,
        )
        is True
    )
    assert (
        is_configured_tmux_socket(
            {"tmux_socket_path": "/tmp/tmux-501/default"},
            config=config,
        )
        is False
    )


def test_configured_socket_uses_exact_configured_path() -> None:
    config = TmuxConfig(socket_name="ignored", socket_path="/tmp/gobby.sock")

    assert (
        is_configured_tmux_socket(
            {"tmux_socket_path": "/tmp/gobby.sock"},
            config=config,
        )
        is True
    )
    assert (
        is_configured_tmux_socket(
            {"tmux_socket_path": "/tmp/other/gobby.sock"},
            config=config,
        )
        is False
    )


def test_configured_socket_is_conservative_for_missing_or_conflicting_identity() -> None:
    config = TmuxConfig(socket_name="agent-socket")

    assert is_configured_tmux_socket({}, config=config) is None
    assert (
        is_configured_tmux_socket(
            {
                "tmux_socket_name": "agent-socket",
                "tmux_socket_path": "/tmp/tmux-501/default",
            },
            config=config,
        )
        is None
    )


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        ({"tmux_window_id": "@290"}, "@290"),
        ({"tmux_window_id": "@invalid"}, None),
        ({"tmux_window_id": "290"}, None),
        ({"tmux_window_id": 290}, None),
        ({}, None),
    ],
)
def test_get_tmux_window_id(context: dict[str, object], expected: str | None) -> None:
    assert get_tmux_window_id(context) == expected


def test_query_tmux_identity_is_bounded_and_parses_result() -> None:
    result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="@290\twork\n",
        stderr="",
    )

    with patch("subprocess.run", return_value=result) as run:
        assert query_tmux_identity("/tmp/tmux-501/default", "%6") == ("@290", "work")

    assert run.call_args.kwargs["timeout"] == 0.5


def test_query_tmux_identity_fails_open_on_timeout() -> None:
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("tmux", 0.5)):
        assert query_tmux_identity("/tmp/tmux-501/default", "%6") is None


def test_query_tmux_generation_parses_pid_and_start_time() -> None:
    result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="1658\t1784592177\t@290\twork\n",
        stderr="",
    )
    with patch("subprocess.run", return_value=result) as run:
        assert query_tmux_generation("/tmp/tmux-501/default", "%6") == {
            "server_pid": 1658,
            "server_start_time": 1784592177,
            "window_id": "@290",
            "session_name": "work",
            "pane_id": "%6",
            "socket_path": "/tmp/tmux-501/default",
        }
    assert run.call_args.kwargs["timeout"] == 0.5
