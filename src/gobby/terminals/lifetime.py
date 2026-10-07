"""Internal terminal lifetime: never authored by agent definitions or public requests.

Native gterm is always attempted first; the lifetime only governs whether a
fallback is legal. A durable workspace-pane role is the sole ``persistent_role``
producer; every other spawn is a ``run``.
"""

from __future__ import annotations

from typing import Final, Literal

TerminalLifetime = Literal["run", "persistent_role"]

NATIVE_FIRST_BACKEND: Final = "native"


def lifetime_for_role(role: str | None) -> TerminalLifetime:
    """Map a durable pane role to its lifetime: non-empty is persistent, else run."""
    return "persistent_role" if role else "run"
