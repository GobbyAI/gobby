"""Composer clearing sequences for Gobby injections.

Gobby types into a CLI's composer for handoff commands (``/clear``, ``/compact``),
wake messages, and the handoff pull prompt. The composer ledger gates every
injection and admits a drain only over daemon text it holds; this standard pass
is the turn-safe tail of that drain, and ``composer_drain_keys`` in
``gobby.terminals.composer_ledger`` leads it with one backspace per held character.
"""

from __future__ import annotations

from gobby.terminals.runtime import NamedKey

__all__ = ["COMPOSER_CLEAR_KEYS", "COMPOSER_DRAIN_LINES", "composer_clear_sequence"]

# Lines one drain pass removes; each pass empties the cursor line and joins its
# neighbours, so this bounds the multi-line draft a drain can clear.
COMPOSER_DRAIN_LINES = 8

# ctrl+u and ctrl+k kill to the line start and end in Claude Code (which ignores
# ctrl+l when driven through tmux); backspace and delete then join the emptied
# line with its neighbours. Codex binds no line kill: ctrl+u on a held ``/compact``
# opened its slash popup, and each backspace removes one character, so this pass
# alone clears at most COMPOSER_DRAIN_LINES characters there. Each key is a no-op
# on an empty buffer. Never escape or ctrl+c: those cancel turns, and a doubled
# ctrl+c exits.
_DRAIN_PASS: tuple[NamedKey, ...] = ("ctrl_u", "ctrl_k", "backspace", "delete")
COMPOSER_CLEAR_KEYS: frozenset[str] = frozenset(_DRAIN_PASS)


def composer_clear_sequence(cli_source: str | None) -> tuple[NamedKey, ...]:
    """The standard turn-safe drain pass for ``cli_source``; see ``_DRAIN_PASS``."""
    return _DRAIN_PASS * COMPOSER_DRAIN_LINES
