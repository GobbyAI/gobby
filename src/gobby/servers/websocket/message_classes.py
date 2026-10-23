"""Message-class sets and predicates that schedule websocket frames.

The connection read loop asks these before it awaits a frame: which types run off
the loop, which ones survive a socket drop, and which ones share an ordering chain.
"""

from __future__ import annotations

import json

from gobby.servers.websocket.workspace_ws import WORKSPACE_OPS

# These handlers do their slow work off the read loop. Later frames on the
# same socket are read while they run. Frames for one terminal stay in
# arrival order; another terminal's frames do not wait (#22709). Workspace
# reads, listings, spawns and kills are here too, so the operator's keystrokes
# never wait behind them (#23080).
_OFF_LOOP_MESSAGE_TYPES = frozenset(
    {
        "terminal_attach",
        "terminal_create",
        "terminal_detach",
        "terminal_focus",
        "terminal_input",
        "terminal_kill",
        "terminal_list",
        "terminal_paste",
        "terminal_release_control",
        "terminal_resize",
        "terminal_take_control",
        "workspace_attach",
        "workspace_op",
        "workspace_snapshot",
    }
)
# A workspace read keeps its place among the socket's workspace mutations.
_WORKSPACE_CHAIN_TYPES = frozenset({"workspace_attach", "workspace_op", "workspace_snapshot"})
# Reads and waits are cancelled with the socket. Mutations stay alive so a
# committed close still publishes its removal and reaps the shell, and a spawn
# or kill already under way is not abandoned halfway.
_READ_WORKSPACE_OPS = frozenset({"workspace.list", "pane.read", "pane.wait_for_output"})
_DURABLE_WORKSPACE_OPS = frozenset(WORKSPACE_OPS) - _READ_WORKSPACE_OPS
_DURABLE_TERMINAL_TYPES = frozenset({"terminal_create", "terminal_kill"})


def _message_leaves_the_read_loop(message: str) -> bool:
    """True when this frame must not be awaited on the connection read loop."""
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    message_type = data.get("type")
    return isinstance(message_type, str) and message_type in _OFF_LOOP_MESSAGE_TYPES


def _survives_disconnect(message: str) -> bool:
    """True when this off-loop frame must finish after the socket drops."""
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("type") in _DURABLE_TERMINAL_TYPES:
        return True
    if data.get("type") != "workspace_op":
        return False
    op = data.get("op")
    return isinstance(op, str) and op in _DURABLE_WORKSPACE_OPS


def _off_loop_chain_key(message: str) -> str:
    """Group off-loop frames that must stay in order on one connection."""
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        return ""
    if not isinstance(data, dict):
        return ""
    message_type = data.get("type")
    if message_type in _WORKSPACE_CHAIN_TYPES:
        return "workspace_op"
    for field in ("terminal_id", "attachment_id"):
        value = data.get(field)
        if isinstance(value, str) and value:
            return value
    return message_type if isinstance(message_type, str) else ""
