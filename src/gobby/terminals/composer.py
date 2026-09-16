"""Composer clearing sequences for Gobby injections.

Gobby types into a CLI's composer for handoff commands (``/clear``, ``/compact``),
wake messages, and the handoff pull prompt. An operator draft already in the box
would otherwise be submitted together with the injected text, so every injection
drains the composer first. Injection sites probe the composer through the
provider's detection manifest (``IdleDetector.composer_read``) and withhold the
injection when the probe positively reads an operator draft; the drain itself
stays blind, so an ``unknown`` read (no frame, no snapshot, unrecognised
provider) still drains exactly as before.
"""

from __future__ import annotations

from gobby.terminals.runtime import NamedKey

__all__ = ["COMPOSER_DRAIN_LINES", "composer_clear_sequence"]

# Lines one drain pass removes; each pass empties the cursor line and joins its
# neighbours, so this bounds the multi-line draft a drain can clear.
COMPOSER_DRAIN_LINES = 8

# ctrl+u and ctrl+k kill to the line start and end in every supported composer
# (Claude Code ignores ctrl+l when driven through tmux); backspace and delete then
# join the emptied line with its neighbours. Each key is a no-op on an empty
# buffer. Never escape or ctrl+c: those cancel turns, and a doubled ctrl+c exits.
_DRAIN_PASS: tuple[NamedKey, ...] = ("ctrl_u", "ctrl_k", "backspace", "delete")


def composer_clear_sequence(cli_source: str | None) -> tuple[NamedKey, ...]:
    """Keys that empty the composer for ``cli_source`` without cancelling a turn."""
    return _DRAIN_PASS * COMPOSER_DRAIN_LINES
