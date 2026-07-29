"""Terminal identity capture and header serialization for the MCP wrapper."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

TERMINAL_CONTEXT_KEYS = (
    "parent_pid",
    "tmux_pane",
    "tmux_socket_path",
    "tmux_session",
    "tty",
    "term_program",
    "term_session_id",
)


def current_terminal_context() -> dict[str, Any]:
    """Collect terminal identity signals available to the stdio proxy."""
    context: dict[str, Any] = {"parent_pid": os.getppid()}

    tmux_pane = os.environ.get("TMUX_PANE")
    if tmux_pane:
        context["tmux_pane"] = tmux_pane

    tmux = os.environ.get("TMUX")
    if tmux:
        tmux_socket_path = tmux.split(",", 1)[0]
        if tmux_socket_path:
            context["tmux_socket_path"] = tmux_socket_path

    for env_name, context_name in (
        ("TMUX_SESSION", "tmux_session"),
        ("TTY", "tty"),
        ("TERM_PROGRAM", "term_program"),
        ("TERM_SESSION_ID", "term_session_id"),
    ):
        value = os.environ.get(env_name)
        if value:
            context[context_name] = value

    return context


def serialize_terminal_context(context: Mapping[str, Any]) -> str:
    """Serialize only supported terminal identity signals as compact JSON."""
    allowlisted = {
        key: context[key]
        for key in TERMINAL_CONTEXT_KEYS
        if key in context and context[key] is not None
    }
    return json.dumps(allowlisted, separators=(",", ":"))
