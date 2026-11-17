"""Terminal identity capture and header serialization for the MCP wrapper."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

from gobby.agents.constants import GOBBY_PANE_REF, GOBBY_TERMINAL_ID

TERMINAL_CONTEXT_KEYS = (
    "parent_pid",
    "tty",
    "term_program",
    "term_session_id",
    "gobby_terminal_id",
    "gobby_pane_ref",
)


def current_terminal_context() -> dict[str, Any]:
    """Collect terminal identity signals available to the stdio proxy."""
    context: dict[str, Any] = {"parent_pid": os.getppid()}

    for env_name, context_name in (
        ("TTY", "tty"),
        ("TERM_PROGRAM", "term_program"),
        ("TERM_SESSION_ID", "term_session_id"),
        (GOBBY_TERMINAL_ID, "gobby_terminal_id"),
        (GOBBY_PANE_REF, "gobby_pane_ref"),
    ):
        value = os.environ.get(env_name)
        if value:
            context.setdefault(context_name, value)

    return context


def serialize_terminal_context(context: Mapping[str, Any]) -> str:
    """Serialize only supported terminal identity signals as compact JSON."""
    allowlisted = {
        key: context[key]
        for key in TERMINAL_CONTEXT_KEYS
        if key in context and context[key] is not None
    }
    return json.dumps(allowlisted, separators=(",", ":"))
