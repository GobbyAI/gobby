"""Target check for a live wake: the send_keys gate, applied before the wake writes (#25830).

A wake types into a local terminal only where send_keys could: a live agent CLI in the
foreground, no credential prompt on screen, and an idle agent composer. The send_keys gate
owns those rules, so the wake calls it rather than keeping a second probe. A node terminal
is never typed into: the hub reads no local pane or process for it, and its composer report
is only a stored copy that can go stale, so the wake is withheld and the message waits for
the seat's next tool call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gobby.agents.idle_detector import ComposerRead
from gobby.hub.machine_routing import is_local_machine
from gobby.terminals.composer_ledger import (
    CURSOR_DRAFT_REASON,
    confirm_cursor_draft,
    cursor_draft_mark,
)

if TYPE_CHECKING:
    from gobby.events.live_wake import TerminalActivity
    from gobby.events.wake import WakeDispatcher

NODE_TERMINAL_WITHHELD = "node_terminal_withheld"


async def target_gate(
    dispatcher: WakeDispatcher,
    session_id: str,
    terminal: Any,
) -> dict[str, Any] | None:
    """Return the withheld outcome when the gate refuses the wake's target, else ``None``.

    No wake replaces composer text: a draft, held or unknown composer is refused on every
    path, operator comms included (#25830 criterion 3). A CLI with no composer rules,
    Grok, has no frame to read, so the ledger's verdict stands for it, as the wake's
    composer probe already read it.
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
    from gobby.mcp_proxy.tools.sessions._terminal_send_keys_gate import (
        ComposerCleared,
        send_keys_target_refusal,
    )

    refusal = await send_keys_target_refusal(
        session_id,
        dispatcher._terminal_manager,
        dispatcher._terminal_runtime_registry,
        resolved_terminal=terminal,
        ledger_vouches=True,
    )
    if refusal is None or isinstance(refusal, ComposerCleared):
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


async def settle_cursor_draft(
    dispatcher: WakeDispatcher,
    session_id: str,
    terminal: Any | None,
    activity: TerminalActivity,
) -> ComposerRead:
    """The composer a wake acts on: an idle, empty pane frame clears a cursor-only draft.

    The pane decides, and a turn in flight is never probed. The draft's mark is taken before
    the frame is read, and the ledger clears only a draft still at that mark, so input that
    lands in between keeps the draft. Otherwise the draft stays and raises the seat's item.
    """
    composer = activity.composer
    if composer.reason != CURSOR_DRAFT_REASON or terminal is None:
        return composer
    if activity.turn_in_flight_fingerprint is not None:
        return composer
    terminal_id = str(terminal.id)
    mark = cursor_draft_mark(terminal_id)
    if mark is None:
        return composer
    # Imported lazily: the send_keys package imports terminal and agent modules that
    # themselves import the events package.
    from gobby.mcp_proxy.tools.sessions._terminal_send_keys_gate import send_keys_target_refusal

    refusal = await send_keys_target_refusal(
        session_id,
        dispatcher._terminal_manager,
        dispatcher._terminal_runtime_registry,
        resolved_terminal=terminal,
        ledger_vouches=True,
        cursor_draft=True,
    )
    if refusal is None and confirm_cursor_draft(terminal_id, mark):
        return ComposerRead("empty")
    return composer
