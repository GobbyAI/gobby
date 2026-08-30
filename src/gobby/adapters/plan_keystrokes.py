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

from gobby.adapters.plan_options import get_plan_accept_option, get_plan_accept_options

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
    """Structural type for the backend-neutral keystroke dispatch used at playback."""

    async def dispatch_keys(self, session_name: str, keys: str, *, literal: bool = ...) -> bool: ...


# A pane-aware resolver maps ``(option_id, live_pane_text)`` onto a keystroke
# sequence, or ``None`` when the pane shows no menu it recognizes. It exists for
# CLIs whose native plan prompt has more than one shape, where the same logical
# option selects different keys per shape (Claude's full plan menu vs. its bare
# "exit plan mode?" confirm). Static-menu sources can still register a matcher
# so stale web-UI clicks do not send blind digits to the current pane.
PlanMenuResolver = Callable[[str, str], "PlanKeystrokeSequence | None"]
PlanMenuMatcher = Callable[[str], bool]
NativePlanOptionResolver = Callable[[int, str], "PlanKeystrokeSequence | None"]


class PlanKeystrokeRegistry:
    """Maps a plan action onto the keystroke sequence that selects it.

    Two registration styles coexist, with optional static-menu guards:

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
        self._menu_matchers: dict[str, PlanMenuMatcher] = {}
        self._native_option_resolvers: dict[str, NativePlanOptionResolver] = {}

    def register(self, source: str, option_id: str, sequence: PlanKeystrokeSequence) -> None:
        """Register (overwriting) the static sequence for ``(source, option_id)``."""
        self._map[(source, option_id)] = sequence

    def register_resolver(self, source: str, resolver: PlanMenuResolver) -> None:
        """Register (overwriting) a pane-aware resolver for ``source``."""
        self._resolvers[source] = resolver

    def register_menu_matcher(self, source: str, matcher: PlanMenuMatcher) -> None:
        """Register (overwriting) a static-menu presence matcher for ``source``."""
        self._menu_matchers[source] = matcher

    def register_native_option_resolver(
        self,
        source: str,
        resolver: NativePlanOptionResolver,
    ) -> None:
        """Register a live-menu resolver for a provider's numbered native choices."""
        self._native_option_resolvers[source] = resolver

    def resolve(self, source: str | None, option_id: str | None) -> PlanKeystrokeSequence | None:
        """Return the static sequence for ``(source, option_id)`` or ``None``."""
        if not source or not option_id:
            return None
        return self._map.get((source, option_id))

    def requires_pane(self, source: str | None) -> bool:
        """Whether resolving ``source`` needs live pane text."""
        return bool(source) and (source in self._resolvers or source in self._menu_matchers)

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
        sequence = self._map.get((source, option_id))
        if sequence is None:
            return None
        matcher = self._menu_matchers.get(source)
        if matcher is not None and not matcher(pane_text):
            return None
        return sequence

    def resolve_native_option_for_pane(
        self,
        source: str | None,
        option: int | None,
        pane_text: str,
    ) -> PlanKeystrokeSequence | None:
        """Resolve an exact numbered choice only when its native menu is live."""
        if not source or option is None or isinstance(option, bool):
            return None
        resolver = self._native_option_resolvers.get(source)
        if resolver is None:
            return None
        return resolver(option, pane_text)

    def has_source(self, source: str | None) -> bool:
        """Whether any static sequence or resolver is registered for ``source``."""
        if not source:
            return False
        if (
            source in self._resolvers
            or source in self._menu_matchers
            or source in self._native_option_resolvers
        ):
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
    decision uses ``option_id`` when it is a real plan_options accept id. A
    missing approve id falls back to the generic approve target, matching Path A.
    Unknown ids still return ``None`` so callers do not send the wrong keystrokes.
    """
    if decision == "request_changes":
        return REQUEST_CHANGES_OPTION_ID
    if decision == "approve":
        if option_id:
            return option_id if get_plan_accept_option(source or "", option_id) else None
        # Without an explicit accept id, intentionally use the first normal
        # post-plan accept option as the generic approve fallback.
        for option in get_plan_accept_options(source or ""):
            if option.post_plan_chat_mode == "normal":
                return option.id
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
        sent = await tmux.dispatch_keys(pane, stroke.keys, literal=stroke.literal)
        if not sent:
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
# Captured empirically from Codex CLI v0.138.0 in a tmux pane
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
# menu shape means a static map suffices, guarded by a menu-presence matcher.


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
# always-present menu shape means a static map suffices, guarded by a
# menu-presence matcher.


def _droid_digit(digit: str) -> PlanKeystrokeSequence:
    """Droid spec-menu selection: the item number activates with no Enter."""
    return PlanKeystrokeSequence(strokes=(PlanKeystroke(digit, literal=True),))


_DROID_PLAN_MENU: dict[str, PlanKeystrokeSequence] = {
    "approve_yolo": _droid_digit("1"),
    "approve_act": _droid_digit("1"),
    REQUEST_CHANGES_OPTION_ID: _droid_digit("4"),
}


def _grok_digit(digit: str) -> PlanKeystrokeSequence:
    """Grok approval-menu selection: the item number activates with no Enter."""
    return PlanKeystrokeSequence(strokes=(PlanKeystroke(digit, literal=True),))


# Grok ("Grok Build" TUI) native tool-approval menu. Captured empirically for
# task #15731 by driving ``grok --permission-mode default`` on a pty and reading
# the rendered menu grid (the gobby-managed ACP path runs grok with
# ``--always-approve`` and never prompts; this mapping is for the native TUI a
# user runs under a proxy terminal). Two menu shapes were observed and the
# selectable option NUMBERS are positionally stable across both ([*] = the
# default-highlighted radio item):
#
#   Shell command:                             File write:
#     1 [*] Yes, and don't ask again for          1 [*] Yes, and don't ask again for
#           anything (always-approve mode)              anything (always-approve mode)
#     2 [ ] Always allow: <command>               2 [ ] Yes, allow all edits this session
#     3 [ ] Yes, proceed                          3 [ ] Yes
#     4 [ ] No, reject (type to add feedback)     4 [ ] No, reject (type to add feedback)
#   footer: "1/4:select | [Left/Right:scope] | Ctrl+o:yolo | Ctrl+c:cancel"
#
# Empirically verified: pressing the digit activates immediately with NO Enter
# (digit "3" approved and executed an echo; digit "4" denied a file write and
# the file was never created). Option 1 (always-approve mode) is the bypass/yolo
# item, option 3 is the single manual approval, option 4 is reject. Option 2 is
# a scope-specific "allow" whose wording varies and has no uniform plan_options
# mapping. Esc only "unselects" the radio (it never rejects), so reject is the
# stable digit 4.
_GROK_PLAN_MENU: dict[str, PlanKeystrokeSequence] = {
    "approve_yolo": _grok_digit("1"),
    "approve_act": _grok_digit("3"),
    REQUEST_CHANGES_OPTION_ID: _grok_digit("4"),
}


def _qwen_digit(digit: str) -> PlanKeystrokeSequence:
    """Qwen Code approval-menu selection: the item number activates with no Enter."""
    return PlanKeystrokeSequence(strokes=(PlanKeystroke(digit, literal=True),))


# Qwen Code (Qwen CLI TUI) native tool-approval menu. Captured empirically for
# task #15732 by driving ``qwen --approval-mode default`` on a pty against a
# working local LM Studio backend and reading the rendered menu grid (the
# gobby-managed ACP path runs qwen headless and never shows this menu; this
# mapping is for the native TUI a user runs under a proxy terminal). Qwen Code
# uses the same RadioButtonSelect confirmation shape as other ACP CLIs
# ([*] = the default-highlighted item):
#
#   Apply this change?
#     [*] 1. Yes, allow once
#     [ ] 2. Yes, allow always
#     [ ] 3. No, suggest changes (esc)
#
# Empirically verified: the item NUMBER activates immediately with NO Enter
# (digit "1" approved and wrote the probe file; digit "2" likewise approved and
# wrote it). Option 1 ("allow once") is the single manual approval, option 2
# ("allow always") is the auto-accept / bypass item. Esc rejects regardless of
# menu shape (Esc on the write menu cancelled the write and the file was never
# created); the reject DIGIT varies by tool type while Escape is
# the shape-independent reject shown as the menu's "(esc)" shortcut.
_QWEN_PLAN_MENU: dict[str, PlanKeystrokeSequence] = {
    "approve_act": _qwen_digit("1"),
    "approve_yolo": _qwen_digit("2"),
    # Reject via the "(esc)" shortcut: the menu's reject DIGIT varies by tool
    # type, but Escape always rejects regardless of menu shape.
    REQUEST_CHANGES_OPTION_ID: PlanKeystrokeSequence(strokes=(PlanKeystroke("Escape"),)),
}


def _pane_contains_all(*needles: str) -> PlanMenuMatcher:
    return lambda pane_text: all(needle in pane_text for needle in needles)


def _numbered_native_resolver(
    matcher: PlanMenuMatcher,
    *,
    minimum: int,
    maximum: int,
    sequence: Callable[[str], PlanKeystrokeSequence],
) -> NativePlanOptionResolver:
    """Build a guarded resolver for a fixed numbered native menu."""

    def resolve(option: int, pane_text: str) -> PlanKeystrokeSequence | None:
        if option < minimum or option > maximum or not matcher(pane_text):
            return None
        return sequence(str(option))

    return resolve


def _claude_native_option(
    option: int,
    pane_text: str,
) -> PlanKeystrokeSequence | None:
    lowered = pane_text.lower()
    if any(marker in lowered for marker in _CLAUDE_FULL_MENU_MARKERS):
        return _claude_digit_then_enter(str(option)) if 1 <= option <= 4 else None
    if _CLAUDE_CONFIRM_MENU_MARKER in lowered:
        return _claude_digit_only(str(option)) if 1 <= option <= 2 else None
    return None


def _agy_open_then(key: str) -> PlanKeystrokeSequence:
    """Open the Action required list with ctrl+r, then send y or n."""
    return PlanKeystrokeSequence(
        strokes=(PlanKeystroke("C-r"), PlanKeystroke(key, literal=True)),
    )


_AGY_PLAN_MENU: dict[str, PlanKeystrokeSequence] = {
    "approve_yolo": _agy_open_then("y"),
    "approve_act": _agy_open_then("y"),
    REQUEST_CHANGES_OPTION_ID: _agy_open_then("n"),
}


def _agy_native_option(option: int, pane_text: str) -> PlanKeystrokeSequence | None:
    if "Action required" not in pane_text:
        return None
    if option == 1:
        return _agy_open_then("y")
    if option == 2:
        return _agy_open_then("n")
    return None


def _qwen_native_option(
    option: int,
    pane_text: str,
) -> PlanKeystrokeSequence | None:
    matcher = _pane_contains_all("Apply this change?", "Yes, allow once", "No, suggest changes")
    if not matcher(pane_text) or option < 1 or option > 3:
        return None
    if option == 3:
        return PlanKeystrokeSequence(strokes=(PlanKeystroke("Escape"),))
    return _qwen_digit(str(option))


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
    registry.register_native_option_resolver("claude", _claude_native_option)
    # --- codex (Plan mode `/plan` -> "Implement this plan?" menu) -- task #15728 ---
    codex_matcher = _pane_contains_all(
        "Implement this plan?", "Yes, implement this plan", "No, stay in Plan mode"
    )
    for _codex_option_id, _codex_sequence in _CODEX_PLAN_MENU.items():
        registry.register("codex", _codex_option_id, _codex_sequence)
    registry.register_menu_matcher("codex", codex_matcher)
    registry.register_native_option_resolver(
        "codex",
        _numbered_native_resolver(
            codex_matcher,
            minimum=1,
            maximum=3,
            sequence=_codex_digit,
        ),
    )
    # --- droid (spec mode `--use-spec` -> "Proceed with the proposal" menu) -- task #15729 ---
    droid_matcher = _pane_contains_all(
        "Proceed with the proposal", "No and explain why", "1-4 select"
    )
    for _droid_option_id, _droid_sequence in _DROID_PLAN_MENU.items():
        registry.register("droid", _droid_option_id, _droid_sequence)
    registry.register_menu_matcher("droid", droid_matcher)
    registry.register_native_option_resolver(
        "droid",
        _numbered_native_resolver(
            droid_matcher,
            minimum=1,
            maximum=4,
            sequence=_droid_digit,
        ),
    )
    # --- grok (Grok Build TUI tool-approval menu) -- task #15731 ---
    grok_matcher = _pane_contains_all("No, reject (type to add feedback)")
    for _grok_option_id, _grok_sequence in _GROK_PLAN_MENU.items():
        registry.register("grok", _grok_option_id, _grok_sequence)
    registry.register_menu_matcher("grok", grok_matcher)
    registry.register_native_option_resolver(
        "grok",
        _numbered_native_resolver(
            grok_matcher,
            minimum=1,
            maximum=4,
            sequence=_grok_digit,
        ),
    )
    # --- qwen (Qwen Code TUI tool-approval menu) -- task #15732 ---
    for _qwen_option_id, _qwen_sequence in _QWEN_PLAN_MENU.items():
        registry.register("qwen", _qwen_option_id, _qwen_sequence)
    registry.register_menu_matcher(
        "qwen",
        _pane_contains_all("Apply this change?", "Yes, allow once", "No, suggest changes (esc)"),
    )
    # --- agy (artifact review: Action required, ctrl+r then y/n) -- task #20755 ---
    agy_matcher = _pane_contains_all("Action required")
    for _agy_option_id, _agy_sequence in _AGY_PLAN_MENU.items():
        registry.register("agy", _agy_option_id, _agy_sequence)
    registry.register_menu_matcher("agy", agy_matcher)
    registry.register_native_option_resolver("agy", _agy_native_option)
    registry.register_native_option_resolver("qwen", _qwen_native_option)


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
    "PlanMenuMatcher",
    "PlanMenuResolver",
    "SupportsSendKeys",
    "build_default_plan_keystroke_registry",
    "dispatch_plan_keystrokes",
    "resolve_action_option_id",
]
