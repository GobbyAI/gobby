"""The attention item for a seat whose wake waits on a gap- or interrupt-blocked composer.

A host event gap or an interrupt blocks every automatic composer write until a
provider submit or the operator's ``release_composer`` vouches for the composer
again. An idle seat never submits, so the withheld wake raises one item on the
seat's session entry naming the seat and that valve, and retires it once the
ledger no longer blocks. A usage limit raises its own provider item, and an
untracked seat (a tmux pane) is vouched for only by ``release_composer``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from gobby.storage.attention import (
    AttentionState,
    AttentionStateManager,
    session_attention_entry_id,
)
from gobby.terminals.composer_ledger import read_composer

COMPOSER_BLOCKED_REASON = "composer_blocked"
ATTENTION_BLOCKS = frozenset({"gap", "interrupt"})

_CAUSES = {"gap": "a terminal host event gap", "interrupt": "an interrupt"}


async def sync_composer_attention(
    manager: AttentionStateManager,
    run_db: Callable[..., Awaitable[Any]],
    session: Any,
    block: str | None,
) -> None:
    """Raise the seat's item while ``block`` withholds its wake; retire it otherwise.

    Only interactive seats carry it: the interactive monitor sweeps a spawned
    agent's session items every pass, which would reopen the item on each retry.
    Another blocked item already naming the seat is left in place.
    """
    if getattr(session, "agent_depth", 0):
        return
    entry_id = session_attention_entry_id(session.id)
    current: AttentionState | None = await run_db(manager.get, entry_id)
    ours = current is not None and current.reason == COMPOSER_BLOCKED_REASON
    if block not in ATTENTION_BLOCKS:
        if current is not None and ours:
            await manager.transition_async(
                run_db,
                entry_id,
                state=None,
                expected_attention_id=current.attention_id,
                expected_fingerprint=current.fingerprint,
            )
        return
    if current is not None and current.state == "blocked" and not ours:
        return
    ref = getattr(session, "ref", None) or session.id
    await manager.transition_async(
        run_db,
        entry_id,
        state="blocked",
        session_id=session.id,
        reason=COMPOSER_BLOCKED_REASON,
        kind="non_actionable",
        fingerprint=f"{COMPOSER_BLOCKED_REASON}:{block}",
        payload={
            "label": "Composer blocked",
            "message": (
                f"A wake for {ref} is withheld: {_CAUSES[block]} left its composer "
                "unconfirmed. Check the pane holds no draft, then run gobby-sessions "
                f'release_composer(session_id="{ref}").'
            ),
        },
        expected_attention_id=current.attention_id if current is not None else None,
    )


def composer_attention_holds(current: AttentionState, terminal_id: str) -> bool:
    """Whether ``current`` is this item and the ledger still blocks ``terminal_id``."""
    return (
        current.reason == COMPOSER_BLOCKED_REASON
        and read_composer(terminal_id).reason in ATTENTION_BLOCKS
    )
