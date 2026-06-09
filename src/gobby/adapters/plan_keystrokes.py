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


# A pane-aware resolver maps ``(option_id, live_pane_text)`` onto a keystroke
# sequence, or ``None`` when the pane shows no menu it recognizes. It exists for
# CLIs whose native plan prompt has more than one shape, where the same logical
# option selects different keys per shape (Claude's full plan menu vs. its bare
# "exit plan mode?" confirm). Sources without a resolver fall back to the static
# ``(source, option_id)`` map.
PlanMenuResolver = Callable[[str, str], "PlanKeystrokeSequence | None"]


class PlanKeystrokeRegistry:
    """Maps a plan action onto the keystroke sequence that selects it.

    Two registration styles coexist:

    * **Static** -- ``register(source, option_id, sequence)`` for a CLI whose
      native menu has a single fixed shape; resolved by :meth:`resolve`.
    * **Pane-aware** -- ``register_resolver(source, resolver)`` for a CLI whose
      menu has multiple shapes that must be told apart from the live pane text
      before keys are chosen; resolved by :meth:`resolve_for_pane`.

    ``source`` is the CLI provider name (``session.source`` -- e.g. ``claude``,
    ``codex``, ``droid``), matching the key space used elsewhere. ``option_id`` is
    a plan_options accept id (``approve_yolo`` / ``approve_act``) or
    :data:`REQUEST_CHANGES_OPTION_ID`.
    """

    def __init__(self) -> None:
        self._map: dict[tuple[str, str], PlanKeystrokeSequence] = {}
        self._resolvers: dict[str, PlanMenuResolver] = {}

    def register(self, source: str, option_id: str, sequence: PlanKeystrokeSequence) -> None:
        """Register (overwriting) the static sequence for ``(source, option_id)``."""
        self._map[(source, option_id)] = sequence

    def register_resolver(self, source: str, resolver: PlanMenuResolver) -> None:
        """Register (overwriting) a pane-aware resolver for ``source``."""
        self._resolvers[source] = resolver

    def resolve(self, source: str | None, option_id: str | None) -> PlanKeystrokeSequence | None:
        """Return the static sequence for ``(source, option_id)`` or ``None``."""
        if not source or not option_id:
            return None
        return self._map.get((source, option_id))

    def requires_pane(self, source: str | None) -> bool:
        """Whether resolving ``source`` needs live pane text (has a resolver)."""
        return bool(source) and source in self._resolvers

    def resolve_for_pane(
        self, source: str | None, option_id: str | None, pane_text: str
    ) -> PlanKeystrokeSequence | None:
        """Resolve using a pane-aware resolver when present, else the static map.

        ``pane_text`` is the live tmux pane content; it is consulted only when a
        resolver is registered for ``source``. Returns ``None`` when no sequence
        applies, so the caller errors rather than sending a guessed key.
        """
        if not source or not option_id:
            return None
        resolver = self._resolvers.get(source)
        if resolver is not None:
            return resolver(option_id, pane_text)
        return self._map.get((source, option_id))

    def has_source(self, source: str | None) -> bool:
        """Whether any static sequence or resolver is registered for ``source``."""
        if not source:
            return False
        if source in self._resolvers:
            return True
        return any(key_source == source for key_source, _ in self._map)

    def registered_options(self, source: str) -> frozenset[str]:
        """The statically-registered option ids for ``source``."""
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


# --- Claude Code native plan menus -------------------------------------------
# Captured empirically from Claude Code v2.1.169 in a gobby-managed tmux pane
# (task #15727). Claude shows TWO menu shapes when leaving plan mode, and they
# differ in BOTH the option set AND the activation mechanic, so the menu must be
# read from the live pane before keys are chosen -- a static map would mis-fire
# ("2" is *manually approve* in the full menu but *No/reject* in the bare
# confirm).
#
# FULL menu (a plan was written) -- a digit MOVES the selection cursor, Enter
# activates:
#     Claude has written up a plan and is ready to execute. Would you like to proceed?
#       1. Yes, and use auto mode
#       2. Yes, manually approve edits
#       3. No, refine with Ultraplan on Claude Code on the web
#       4. Tell Claude what to change
# CONFIRM menu (no plan was written) -- the bare Yes/No menu activates on the
# digit alone, with no Enter:
#     Exit plan mode?
#     Claude wants to exit plan mode
#       1. Yes
#       2. No
_CLAUDE_FULL_MENU_MARKERS = ("manually approve edits", "use auto mode")
_CLAUDE_CONFIRM_MENU_MARKER = "wants to exit plan mode"


def _claude_digit_then_enter(digit: str) -> PlanKeystrokeSequence:
    """Full-menu selection: type the item number, then Enter to activate."""
    return PlanKeystrokeSequence(
        strokes=(PlanKeystroke(digit, literal=True), PlanKeystroke("Enter")),
    )


def _claude_digit_only(digit: str) -> PlanKeystrokeSequence:
    """Confirm-menu selection: the bare Yes/No menu activates on the digit."""
    return PlanKeystrokeSequence(strokes=(PlanKeystroke(digit, literal=True),))


# Full plan menu: 1=auto(yolo), 2=manual(act), 4="tell Claude what to change"
# (keep planning / request changes). Option 3 routes to web Ultraplan and is
# deliberately unused.
_CLAUDE_FULL_MENU: dict[str, PlanKeystrokeSequence] = {
    "approve_yolo": _claude_digit_then_enter("1"),
    "approve_act": _claude_digit_then_enter("2"),
    REQUEST_CHANGES_OPTION_ID: _claude_digit_then_enter("4"),
}

# Bare confirm menu has only Yes/No, so both approves collapse to "Yes" (1) --
# there is no edits to auto-accept-vs-manually-approve when no plan was written --
# and request-changes is "No" (2).
_CLAUDE_CONFIRM_MENU: dict[str, PlanKeystrokeSequence] = {
    "approve_yolo": _claude_digit_only("1"),
    "approve_act": _claude_digit_only("1"),
    REQUEST_CHANGES_OPTION_ID: _claude_digit_only("2"),
}


def _claude_plan_keystrokes(option_id: str, pane_text: str) -> PlanKeystrokeSequence | None:
    """Resolve Claude's plan-menu keystrokes from the live pane text.

    Detects which native menu is showing and returns the option's sequence for
    that menu. Returns ``None`` when the pane shows neither known menu, so the
    handler reports ``PLAN_KEYSTROKES_UNMAPPED`` instead of sending a guessed key.
    """
    lowered = pane_text.lower()
    if any(marker in lowered for marker in _CLAUDE_FULL_MENU_MARKERS):
        return _CLAUDE_FULL_MENU.get(option_id)
    if _CLAUDE_CONFIRM_MENU_MARKER in lowered:
        return _CLAUDE_CONFIRM_MENU.get(option_id)
    return None


# --- Codex native plan-mode approval menu ------------------------------------
# Captured empirically from Codex CLI v0.138.0 (gpt-5.5) in a tmux pane
# (task #15728). Entering Plan mode (`/plan`) and letting the model propose a
# plan renders ONE approval menu; typing the item number selects AND activates
# it immediately -- the "Press enter to confirm" hint applies only to arrow-key
# navigation, the digit needs no following Enter (verified live: pressing "3"
# alone selected "stay in Plan mode" and dismissed the menu):
#     Implement this plan?
#       1. Yes, implement this plan          Switch to Default and start coding.
#       2. Yes, clear context and implement  Fresh thread.
#       3. No, stay in Plan mode             Continue planning with the model.
#       Press enter to confirm or esc to go back
# Unlike Claude, Codex's plan menu has a SINGLE approve semantic ("implement"):
# it cannot express auto/bypass-vs-manual approval at this menu (the post-plan
# approval mode is governed by the session's --ask-for-approval policy, not
# selectable here), so both plan_options approves collapse onto "1" -- the same
# precedent as Claude's bare confirm menu. request-changes maps to "3" (stay in
# Plan mode so the user can send revision feedback). A single, always-present
# menu shape means a static map suffices -- no pane-aware resolver needed.


def _codex_digit(digit: str) -> PlanKeystrokeSequence:
    """Codex plan-menu selection: the item number activates with no Enter."""
    return PlanKeystrokeSequence(strokes=(PlanKeystroke(digit, literal=True),))


_CODEX_PLAN_MENU: dict[str, PlanKeystrokeSequence] = {
    "approve_yolo": _codex_digit("1"),
    "approve_act": _codex_digit("1"),
    REQUEST_CHANGES_OPTION_ID: _codex_digit("3"),
}


# --- Droid native spec-mode (ExitSpecMode) approval menu ---------------------
# Captured empirically from Droid CLI v0.137.1 (Factory, spec model Opus 4.8) in
# a tmux pane (task #15729). Entering spec mode (`droid --use-spec`) and giving
# the agent a real planning task renders ONE approval menu when it proposes a
# spec; typing the item number selects AND activates immediately -- the footer's
# "Enter select" hint applies only to arrow-key (up/down) navigation, the digit
# needs no following Enter (verified live: pressing "4" alone dismissed the menu
# and returned to the spec-mode prompt without touching the file):
#     1. Proceed with the proposal
#     2. Proceed with comment
#     3. Manually edit spec (open via system default)
#     4. No and explain why
#     up/down navigate   1-4 select   Enter select   Tab reasoning   Esc cancel
# Like Codex, Droid's spec menu has a SINGLE approve semantic ("proceed"): it
# cannot express auto/bypass-vs-manual approval here (autonomy is a separate
# axis -- the "Auto (Off) - all actions require approval" status line, toggled
# with ctrl+L, not this menu), so both plan_options approves collapse onto "1".
# request-changes maps to "4" ("No and explain why" -> stays in spec mode so the
# user can send revision feedback). Options 2 ("Proceed with comment", blocks
# for a typed comment) and 3 ("Manually edit spec", opens an external editor)
# are deliberately unused: neither fits a single-keystroke dispatch. A single,
# always-present menu shape means a static map suffices -- no pane-aware
# resolver needed (requires_pane("droid") stays False).


def _droid_digit(digit: str) -> PlanKeystrokeSequence:
    """Droid spec-menu selection: the item number activates with no Enter."""
    return PlanKeystrokeSequence(strokes=(PlanKeystroke(digit, literal=True),))


_DROID_PLAN_MENU: dict[str, PlanKeystrokeSequence] = {
    "approve_yolo": _droid_digit("1"),
    "approve_act": _droid_digit("1"),
    REQUEST_CHANGES_OPTION_ID: _droid_digit("4"),
}


# --- Gemini native tool-approval menu (interactive TUI) ----------------------
# Captured empirically from Gemini CLI v0.44.1 (Google, model Auto) in a tmux
# pane (task #15730). Gemini's plan mode (`--approval-mode plan`) is read-only
# and gates the *plan* conversationally ("Does this plan look good to you?") with
# no selectable menu -- the approval mode is cycled with Shift+Tab, not chosen
# from a keystroke menu. The native keystroke-selectable gate is the per-action
# approval prompt shown in manual/default mode when the agent runs a tool;
# typing the item number selects AND activates immediately (no following Enter --
# verified live: "4" and Esc each dismissed the menu and cancelled the action
# without touching the file). The menu's top two options are stable across tool
# types, but the reject option's NUMBER varies by tool while its "(esc)" shortcut
# is constant:
#     Apply this change?                  (Edit/Write)
#       1. Allow once
#       2. Allow for this session
#       3. Modify with external editor
#       4. No, suggest changes (esc)
#     Allow execution of [Shell]?         (Shell)
#       1. Allow once
#       2. Allow for this session
#       3. No, suggest changes (esc)
# Unlike Codex/Droid, Gemini's menu CAN express the bypass-vs-manual distinction:
# "Allow once" (1, keep prompting each action) -> approve_act/normal, and "Allow
# for this session" (2, stop prompting this session) -> approve_yolo/bypass.
# request-changes maps to the Esc KEY rather than a digit because the reject
# item's number is not stable (4 for edits, 3 for shell) while Esc always rejects
# -- so a static map keyed on the stable positions 1/2 plus Esc resolves every
# menu shape without a pane-aware resolver (requires_pane("gemini") stays False).
# Option 3 "Modify with external editor" (edit menu only, opens an external
# editor) is deliberately unused: it does not fit a single-keystroke dispatch.


def _gemini_digit(digit: str) -> PlanKeystrokeSequence:
    """Gemini approval-menu selection: the item number activates with no Enter."""
    return PlanKeystrokeSequence(strokes=(PlanKeystroke(digit, literal=True),))


_GEMINI_PLAN_MENU: dict[str, PlanKeystrokeSequence] = {
    "approve_act": _gemini_digit("1"),
    "approve_yolo": _gemini_digit("2"),
    # Reject via the "(esc)" shortcut: the menu's reject DIGIT varies by tool
    # type, but Escape always rejects regardless of menu shape.
    REQUEST_CHANGES_OPTION_ID: PlanKeystrokeSequence(strokes=(PlanKeystroke("Escape"),)),
}


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
    registry.register_resolver("claude", _claude_plan_keystrokes)
    # --- codex (Plan mode `/plan` -> "Implement this plan?" menu) -- task #15728 ---
    for _codex_option_id, _codex_sequence in _CODEX_PLAN_MENU.items():
        registry.register("codex", _codex_option_id, _codex_sequence)
    # --- droid (spec mode `--use-spec` -> "Proceed with the proposal" menu) -- task #15729 ---
    for _droid_option_id, _droid_sequence in _DROID_PLAN_MENU.items():
        registry.register("droid", _droid_option_id, _droid_sequence)
    # --- gemini (interactive TUI tool-approval menu) -- task #15730 ---
    for _gemini_option_id, _gemini_sequence in _GEMINI_PLAN_MENU.items():
        registry.register("gemini", _gemini_option_id, _gemini_sequence)
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
    "PlanMenuResolver",
    "SupportsSendKeys",
    "build_default_plan_keystroke_registry",
    "dispatch_plan_keystrokes",
    "resolve_action_option_id",
]
