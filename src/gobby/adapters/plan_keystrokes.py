"""Per-CLI keystroke mapping for native-TUI plan-approval driving (Path B).

Path B drives the *native* plan menu of a CLI running in a tmux pane (a
"proxy-terminal" / attached session) by sending keystrokes, instead of
resolving an in-memory ``ChatSession`` plan gate (Path A -- the managed,
headless web-chat path in :mod:`gobby.servers.websocket.handlers.plan_approval`).

The web UI presents the SAME plan-accept option set for every source -- the
single source of truth is :mod:`gobby.adapters.plan_options` (``approve_yolo`` /
``approve_act``), plus the separate request-changes (reject) action. This module
maps a ``(source, option_id)`` pair onto the concrete keystroke sequence that
selects the matching item in that CLI's native plan menu, and dispatches it to
the pane.

The keystroke *values* are intentionally empty here. Each managed CLI's menu
differs and must be captured empirically (the menu text and its selection
mechanics) rather than guessed; the per-CLI child tasks register their captured
sequences at the marked registration point in
:func:`_register_builtin_plan_keystrokes`. This module supplies the abstraction,
the registration seam, and the dispatch loop; its tests cover those without
depending on any real CLI's values.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from gobby.adapters.plan_options import get_plan_accept_option

# Reserved option id for the reject / request-changes action. It is NOT part of
# the plan_options accept set (which is approve-only); native plan menus expose
# a "keep planning" choice that this id maps onto per CLI.
REQUEST_CHANGES_OPTION_ID = "request_changes"


@dataclass(frozen=True)
class PlanKeystroke:
    """One key event sent to a native CLI plan prompt in a tmux pane.

    Mirrors :meth:`gobby.agents.tmux.session_manager.TmuxSessionManager.send_keys`:

    * ``literal=False`` (default) sends ``keys`` raw, so tmux key *names* apply
      (``Enter``, ``Down``, ``Up``, ``Escape``, ``C-c``). Use for menu
      navigation and confirmation.
    * ``literal=True`` types ``keys`` verbatim (``-l``); a trailing newline
      becomes an Enter. Use for typed input such as a menu number.
    """

    keys: str
    literal: bool = False


@dataclass(frozen=True)
class PlanKeystrokeSequence:
    """Ordered keystrokes that select one plan-menu choice.

    ``settle_seconds`` is paused *between* strokes so the TUI re-renders between,
    e.g., a navigation key and the confirming Enter; it is not applied before the
    first stroke or after the last.
    """

    strokes: tuple[PlanKeystroke, ...]
    settle_seconds: float = 0.05

    def __post_init__(self) -> None:
        if not self.strokes:
            raise ValueError("PlanKeystrokeSequence requires at least one stroke")
        if self.settle_seconds < 0:
            raise ValueError("settle_seconds must be non-negative")


class SupportsSendKeys(Protocol):
    """Structural type for the tmux send-keys capability used at dispatch."""

    async def send_keys(self, session_name: str, keys: str, *, literal: bool = ...) -> bool: ...


class PlanKeystrokeRegistry:
    """Maps ``(source, option_id)`` to the keystroke sequence that selects it.

    ``source`` is the CLI provider name (``session.source`` -- e.g. ``claude``,
    ``codex``, ``droid``), matching the key space used elsewhere. ``option_id`` is
    a plan_options accept id (``approve_yolo`` / ``approve_act``) or
    :data:`REQUEST_CHANGES_OPTION_ID`.
    """

    def __init__(self) -> None:
        self._map: dict[tuple[str, str], PlanKeystrokeSequence] = {}

    def register(self, source: str, option_id: str, sequence: PlanKeystrokeSequence) -> None:
        """Register (overwriting) the sequence for ``(source, option_id)``."""
        self._map[(source, option_id)] = sequence

    def resolve(self, source: str | None, option_id: str | None) -> PlanKeystrokeSequence | None:
        """Return the sequence for ``(source, option_id)`` or ``None`` if unmapped."""
        if not source or not option_id:
            return None
        return self._map.get((source, option_id))

    def has_source(self, source: str | None) -> bool:
        """Whether any sequence is registered for ``source``."""
        if not source:
            return False
        return any(key_source == source for key_source, _ in self._map)

    def registered_options(self, source: str) -> frozenset[str]:
        """The option ids registered for ``source``."""
        return frozenset(option_id for key_source, option_id in self._map if key_source == source)


def resolve_action_option_id(
    source: str | None, decision: str | None, option_id: str | None
) -> str | None:
    """Resolve the registry option id for a plan decision.

    ``request_changes`` maps to :data:`REQUEST_CHANGES_OPTION_ID`. An ``approve``
    decision uses ``option_id`` only when it is a real plan_options accept id (the
    accept set is uniform across sources); an unknown or missing id yields
    ``None`` so the caller errors rather than sending the wrong keystrokes.
    """
    if decision == "request_changes":
        return REQUEST_CHANGES_OPTION_ID
    if decision == "approve" and option_id and get_plan_accept_option(source or "", option_id):
        return option_id
    return None


async def dispatch_plan_keystrokes(
    tmux: SupportsSendKeys,
    pane: str,
    sequence: PlanKeystrokeSequence,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> bool:
    """Send each stroke in order, pausing ``settle_seconds`` between strokes.

    Returns ``False`` on the first failed send (and stops); ``True`` when every
    stroke was sent.
    """
    for index, stroke in enumerate(sequence.strokes):
        if index and sequence.settle_seconds > 0:
            await sleep(sequence.settle_seconds)
        if not await tmux.send_keys(pane, stroke.keys, literal=stroke.literal):
            return False
    return True


def _register_builtin_plan_keystrokes(registry: PlanKeystrokeRegistry) -> None:
    """Per-CLI registration point for native plan-menu keystrokes.

    Each managed CLI's native plan menu must be captured empirically (menu text +
    selection mechanics) before its sequences are registered here. The per-CLI
    child tasks fill in their block below. Until a CLI's block is populated,
    proxy-terminal plan approval for that source resolves to no sequence and the
    handler reports an explicit ``PLAN_KEYSTROKES_UNMAPPED`` error.

    Register the three actions per source:

    * ``approve_yolo``               -> native "auto-accept / bypass" menu item
    * ``approve_act``                -> native "manually approve" menu item
    * :data:`REQUEST_CHANGES_OPTION_ID` -> native "keep planning" menu item

    Example (illustrative only -- do NOT enable a source without a real capture)::

        registry.register(
            "example",
            "approve_yolo",
            PlanKeystrokeSequence(
                (PlanKeystroke("1", literal=True), PlanKeystroke("Enter")),
            ),
        )
    """
    # --- claude (ExitPlanMode menu) -- task #15727 ---
    # --- codex -- task #15728 ---
    # --- droid (ExitSpecMode menu) -- task #15729 ---
    # --- gemini (ACP) -- task #15730 ---
    # --- grok (ACP) -- task #15731 ---
    # --- qwen (ACP) -- task #15732 ---
    return


def build_default_plan_keystroke_registry() -> PlanKeystrokeRegistry:
    """Build the registry seeded with every registered per-CLI sequence."""
    registry = PlanKeystrokeRegistry()
    _register_builtin_plan_keystrokes(registry)
    return registry


# Process-wide default used by the websocket handler. Per-CLI child tasks extend
# the values through :func:`_register_builtin_plan_keystrokes`.
DEFAULT_PLAN_KEYSTROKES = build_default_plan_keystroke_registry()


__all__ = [
    "DEFAULT_PLAN_KEYSTROKES",
    "REQUEST_CHANGES_OPTION_ID",
    "PlanKeystroke",
    "PlanKeystrokeRegistry",
    "PlanKeystrokeSequence",
    "SupportsSendKeys",
    "build_default_plan_keystroke_registry",
    "dispatch_plan_keystrokes",
    "resolve_action_option_id",
]
