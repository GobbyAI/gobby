"""Target check for a live wake, applied before the wake writes (#25830, #25901).

A wake reads no pane text. CLI screens change with every release, so what a provider drew
(a spinner, a prompt, transcript prose) never decides a wake. The seat's session row, the
pane's focus and the keystroke composer ledger decide it in the dispatcher; this check
only confirms by process inspection that the foreground process is an agent CLI, so a wake
never types into a bare shell or another program. A node terminal is never typed into: the
hub inspects no local process for it, and its composer report is only a stored copy that
can go stale, so the wake is withheld and the message waits for the seat's next tool call.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gobby.hub.machine_routing import is_local_machine

NODE_TERMINAL_WITHHELD = "node_terminal_withheld"


async def target_gate(
    session_id: str, terminal: Any, terminal_context: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """Return the withheld outcome when the wake's target is no agent CLI, else ``None``.

    ``terminal_context`` is the target session's recorded process identity.
    """
    if terminal is not None and not is_local_machine(terminal.machine_id):
        return {
            "session_id": session_id,
            "delivered": False,
            "method": "terminal",
            "skipped": NODE_TERMINAL_WITHHELD,
            "decline_reason": NODE_TERMINAL_WITHHELD,
            "ism_persisted": True,
        }
    # Imported lazily: the send_keys package imports terminal and agent modules that
    # themselves import the events package.
    from gobby.mcp_proxy.tools.sessions._terminal_send_keys_gate import wake_target_refusal

    refusal = await wake_target_refusal(session_id, terminal, terminal_context)
    if refusal is None:
        return None
    return {
        "session_id": session_id,
        "delivered": False,
        "method": "terminal",
        "skipped": refusal.code,
        "decline_reason": refusal.code,
        "reason": refusal.message,
        "ism_persisted": True,
    }
