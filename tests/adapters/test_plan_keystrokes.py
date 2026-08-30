"""Unit tests for the Path B plan-keystroke registry and dispatch.

These cover the mapping abstraction and dispatch resolution only -- not any
real CLI's keystroke values, which the per-CLI child tasks capture and register.
"""

from __future__ import annotations

import pytest

from gobby.adapters.plan_keystrokes import (
    DEFAULT_PLAN_KEYSTROKES,
    REQUEST_CHANGES_OPTION_ID,
    PlanKeystroke,
    PlanKeystrokeRegistry,
    PlanKeystrokeSequence,
    dispatch_plan_keystrokes,
    resolve_action_option_id,
)

# Verbatim captures from Claude Code v2.1.169 in a gobby-managed tmux pane.
# FULL menu renders when a plan was written; the bare CONFIRM menu when not.
_CLAUDE_FULL_MENU_PANE = """\
 Claude has written up a plan and is ready to execute. Would you like to proceed?

 ❯ 1. Yes, and use auto mode
   2. Yes, manually approve edits
   3. No, refine with Ultraplan on Claude Code on the web
   4. Tell Claude what to change
      shift+tab to approve with this feedback

 ctrl+g to edit in  Vim  · ~/.claude/plans/you-are-in-plan-cached-torvalds.md
"""

_CLAUDE_CONFIRM_MENU_PANE = """\
 Exit plan mode?

  Claude wants to exit plan mode

  ❯ 1. Yes
    2. No
"""

# Plain plan-mode prompt with no exit menu showing. Contains the footer phrase
# "plan mode" but neither menu's markers, so it must resolve to None.
_CLAUDE_NO_MENU_PANE = (
    "josh@MBP gobby % normal shell output here\n❯ \n  ⏸ plan mode on (shift+tab to cycle)\n"
)


class _FakeTmux:
    """Records send_keys calls; returns queued results or True by default."""

    def __init__(self, results: list[bool] | None = None) -> None:
        self.calls: list[tuple[str, str, bool]] = []
        self._results = list(results) if results is not None else None

    async def send_keys(self, session_name: str, keys: str, *, literal: bool = True) -> bool:
        self.calls.append((session_name, keys, literal))
        if self._results is None:
            return True
        return self._results.pop(0)

    async def dispatch_keys(self, session_name: str, keys: str, *, literal: bool = True) -> bool:
        return await self.send_keys(session_name, keys, literal=literal)


class TestPlanKeystrokeSequence:
    def test_rejects_empty_strokes(self) -> None:
        with pytest.raises(ValueError, match="at least one stroke"):
            PlanKeystrokeSequence(strokes=())

    def test_rejects_negative_settle(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            PlanKeystrokeSequence(strokes=(PlanKeystroke("Enter"),), settle_seconds=-0.1)

    def test_defaults(self) -> None:
        stroke = PlanKeystroke("1", literal=True)
        seq = PlanKeystrokeSequence(strokes=(stroke,))
        assert seq.strokes == (stroke,)
        assert seq.settle_seconds == pytest.approx(0.05)
        assert stroke.literal is True
        assert PlanKeystroke("Enter").literal is False


class TestPlanKeystrokeRegistry:
    def test_register_and_resolve_roundtrip(self) -> None:
        registry = PlanKeystrokeRegistry()
        seq = PlanKeystrokeSequence(strokes=(PlanKeystroke("Enter"),))
        registry.register("example", "approve_yolo", seq)
        assert registry.resolve("example", "approve_yolo") is seq

    def test_resolve_missing_returns_none(self) -> None:
        registry = PlanKeystrokeRegistry()
        assert registry.resolve("example", "approve_yolo") is None

    def test_resolve_with_falsy_inputs_returns_none(self) -> None:
        registry = PlanKeystrokeRegistry()
        registry.register("example", "approve_yolo", PlanKeystrokeSequence((PlanKeystroke("x"),)))
        assert registry.resolve(None, "approve_yolo") is None
        assert registry.resolve("example", None) is None
        assert registry.resolve("", "approve_yolo") is None

    def test_register_overwrites(self) -> None:
        registry = PlanKeystrokeRegistry()
        first = PlanKeystrokeSequence((PlanKeystroke("a"),))
        second = PlanKeystrokeSequence((PlanKeystroke("b"),))
        registry.register("example", "approve_act", first)
        registry.register("example", "approve_act", second)
        assert registry.resolve("example", "approve_act") is second

    def test_has_source_and_registered_options(self) -> None:
        registry = PlanKeystrokeRegistry()
        registry.register("example", "approve_yolo", PlanKeystrokeSequence((PlanKeystroke("1"),)))
        registry.register(
            "example", REQUEST_CHANGES_OPTION_ID, PlanKeystrokeSequence((PlanKeystroke("3"),))
        )
        assert registry.has_source("example") is True
        assert registry.has_source("other") is False
        assert registry.has_source(None) is False
        assert registry.registered_options("example") == frozenset(
            {"approve_yolo", REQUEST_CHANGES_OPTION_ID}
        )
        assert registry.registered_options("other") == frozenset()

    def test_register_resolver_and_resolve_for_pane(self) -> None:
        registry = PlanKeystrokeRegistry()
        go = PlanKeystrokeSequence((PlanKeystroke("1", literal=True),))

        def resolver(option_id: str, pane_text: str) -> PlanKeystrokeSequence | None:
            return go if (option_id == "approve_yolo" and "go" in pane_text) else None

        registry.register_resolver("example", resolver)
        assert registry.requires_pane("example") is True
        assert registry.requires_pane("other") is False
        assert registry.requires_pane(None) is False
        assert registry.has_source("example") is True
        assert registry.resolve_for_pane("example", "approve_yolo", "go now") is go
        # Resolver vetoes when the pane does not match.
        assert registry.resolve_for_pane("example", "approve_yolo", "stop") is None
        assert registry.resolve_for_pane("example", "approve_act", "go now") is None
        # A pure resolver has no static entries.
        assert registry.resolve("example", "approve_yolo") is None

    def test_static_resolution_can_require_matching_pane_text(self) -> None:
        registry = PlanKeystrokeRegistry()
        seq = PlanKeystrokeSequence((PlanKeystroke("1"),))
        registry.register("static", "approve_yolo", seq)
        registry.register_menu_matcher("static", lambda pane_text: "menu" in pane_text)
        assert registry.requires_pane("static") is True
        assert registry.resolve_for_pane("static", "approve_yolo", "menu") is seq
        assert registry.resolve_for_pane("static", "approve_yolo", "anything") is None

    def test_resolve_for_pane_falsy_inputs_return_none(self) -> None:
        registry = PlanKeystrokeRegistry()
        registry.register_resolver(
            "example", lambda _o, _p: PlanKeystrokeSequence((PlanKeystroke("1"),))
        )
        assert registry.resolve_for_pane(None, "approve_yolo", "x") is None
        assert registry.resolve_for_pane("example", None, "x") is None
        assert registry.resolve_for_pane("", "approve_yolo", "x") is None


class TestResolveActionOptionId:
    def test_request_changes_maps_to_sentinel(self) -> None:
        assert (
            resolve_action_option_id("claude", "request_changes", None) == REQUEST_CHANGES_OPTION_ID
        )
        # option_id is ignored for request_changes.
        assert (
            resolve_action_option_id("claude", "request_changes", "approve_yolo")
            == REQUEST_CHANGES_OPTION_ID
        )

    @pytest.mark.parametrize("option_id", ["approve_yolo", "approve_act"])
    def test_approve_with_valid_accept_id(self, option_id: str) -> None:
        assert resolve_action_option_id("codex", "approve", option_id) == option_id

    def test_approve_source_agnostic(self) -> None:
        # The accept set is uniform, so source does not gate validity.
        assert resolve_action_option_id(None, "approve", "approve_yolo") == "approve_yolo"

    def test_approve_with_unknown_option_is_none(self) -> None:
        assert resolve_action_option_id("codex", "approve", "approve_bogus") is None

    def test_approve_without_option_uses_generic_act_default(self) -> None:
        assert resolve_action_option_id("codex", "approve", None) == "approve_act"

    @pytest.mark.parametrize("decision", ["", "deny", None])
    def test_unknown_decision_is_none(self, decision: str | None) -> None:
        assert resolve_action_option_id("codex", decision, "approve_yolo") is None


class TestDispatchPlanKeystrokes:
    @pytest.mark.asyncio
    async def test_sends_strokes_in_order_with_literal_flags(self) -> None:
        tmux = _FakeTmux()
        sleeps: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        seq = PlanKeystrokeSequence(
            strokes=(PlanKeystroke("2", literal=True), PlanKeystroke("Enter")),
            settle_seconds=0.05,
        )
        ok = await dispatch_plan_keystrokes(tmux, "%9", seq, sleep=fake_sleep)

        assert ok is True
        assert tmux.calls == [("%9", "2", True), ("%9", "Enter", False)]
        # One settle pause between the two strokes; none before the first.
        assert sleeps == [pytest.approx(0.05)]

    @pytest.mark.asyncio
    async def test_no_sleep_when_settle_zero(self) -> None:
        tmux = _FakeTmux()
        sleeps: list[float] = []

        async def fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        seq = PlanKeystrokeSequence(
            strokes=(PlanKeystroke("Down"), PlanKeystroke("Enter")), settle_seconds=0.0
        )
        ok = await dispatch_plan_keystrokes(tmux, "%9", seq, sleep=fake_sleep)

        assert ok is True
        assert sleeps == []
        assert len(tmux.calls) == 2

    @pytest.mark.asyncio
    async def test_stops_on_first_failed_send(self) -> None:
        tmux = _FakeTmux(results=[True, False, True])
        seq = PlanKeystrokeSequence(
            strokes=(PlanKeystroke("Down"), PlanKeystroke("Down"), PlanKeystroke("Enter")),
            settle_seconds=0.0,
        )
        ok = await dispatch_plan_keystrokes(tmux, "%9", seq)

        assert ok is False
        # Stopped after the failing second send; the third was never attempted.
        assert len(tmux.calls) == 2


def _full(digit: str) -> PlanKeystrokeSequence:
    """Expected full-menu selection: digit (literal) then Enter to activate."""
    return PlanKeystrokeSequence((PlanKeystroke(digit, literal=True), PlanKeystroke("Enter")))


def _confirm(digit: str) -> PlanKeystrokeSequence:
    """Expected confirm-menu selection: the bare Yes/No menu activates on digit."""
    return PlanKeystrokeSequence((PlanKeystroke(digit, literal=True),))


def _codex(digit: str) -> PlanKeystrokeSequence:
    """Expected Codex plan-menu selection: the digit activates with no Enter."""
    return PlanKeystrokeSequence((PlanKeystroke(digit, literal=True),))


def _droid(digit: str) -> PlanKeystrokeSequence:
    """Expected Droid spec-menu selection: the digit activates with no Enter."""
    return PlanKeystrokeSequence((PlanKeystroke(digit, literal=True),))


def _grok(digit: str) -> PlanKeystrokeSequence:
    """Expected Grok approval-menu selection: the digit activates with no Enter."""
    return PlanKeystrokeSequence((PlanKeystroke(digit, literal=True),))


def _qwen(digit: str) -> PlanKeystrokeSequence:
    """Expected Qwen Code approval-menu selection: the digit activates with no Enter."""
    return PlanKeystrokeSequence((PlanKeystroke(digit, literal=True),))


def _qwen_escape() -> PlanKeystrokeSequence:
    """Expected Qwen Code reject: the shape-independent Escape ('(esc)') shortcut."""
    return PlanKeystrokeSequence((PlanKeystroke("Escape"),))


def _agy_approve() -> PlanKeystrokeSequence:
    return PlanKeystrokeSequence(
        strokes=(PlanKeystroke("C-r"), PlanKeystroke("y", literal=True)),
    )


def _agy_reject() -> PlanKeystrokeSequence:
    return PlanKeystrokeSequence(
        strokes=(PlanKeystroke("C-r"), PlanKeystroke("n", literal=True)),
    )


_AGY_PLAN_MENU_PANE = """\
Action required
  brain/abc/plan.md
  y approve   n reject
"""


class TestClaudeResolver:
    """Claude's pane-aware mapping over both native ExitPlanMode menu shapes."""

    def test_claude_requires_pane(self) -> None:
        assert DEFAULT_PLAN_KEYSTROKES.requires_pane("claude") is True

    @pytest.mark.parametrize(
        ("option_id", "expected"),
        [
            ("approve_yolo", _full("1")),
            ("approve_act", _full("2")),
            (REQUEST_CHANGES_OPTION_ID, _full("4")),
        ],
    )
    def test_full_menu_mapping(self, option_id: str, expected: PlanKeystrokeSequence) -> None:
        assert (
            DEFAULT_PLAN_KEYSTROKES.resolve_for_pane("claude", option_id, _CLAUDE_FULL_MENU_PANE)
            == expected
        )

    @pytest.mark.parametrize(
        ("option_id", "expected"),
        [
            # No plan was written, so both approves collapse to "Yes" (1).
            ("approve_yolo", _confirm("1")),
            ("approve_act", _confirm("1")),
            (REQUEST_CHANGES_OPTION_ID, _confirm("2")),
        ],
    )
    def test_confirm_menu_mapping(self, option_id: str, expected: PlanKeystrokeSequence) -> None:
        assert (
            DEFAULT_PLAN_KEYSTROKES.resolve_for_pane("claude", option_id, _CLAUDE_CONFIRM_MENU_PANE)
            == expected
        )

    def test_full_menu_uses_enter_confirm_menu_does_not(self) -> None:
        # The activation mechanic differs between the menus: the full menu needs
        # a trailing Enter, the bare confirm menu activates on the digit alone.
        full = DEFAULT_PLAN_KEYSTROKES.resolve_for_pane(
            "claude", "approve_yolo", _CLAUDE_FULL_MENU_PANE
        )
        confirm = DEFAULT_PLAN_KEYSTROKES.resolve_for_pane(
            "claude", "approve_yolo", _CLAUDE_CONFIRM_MENU_PANE
        )
        assert full is not None and confirm is not None
        assert [s.keys for s in full.strokes] == ["1", "Enter"]
        assert [s.keys for s in confirm.strokes] == ["1"]

    def test_unknown_menu_returns_none(self) -> None:
        # No menu showing -> no guessed keystrokes (handler then errors).
        assert (
            DEFAULT_PLAN_KEYSTROKES.resolve_for_pane("claude", "approve_yolo", _CLAUDE_NO_MENU_PANE)
            is None
        )

    def test_unknown_option_returns_none(self) -> None:
        assert (
            DEFAULT_PLAN_KEYSTROKES.resolve_for_pane(
                "claude", "approve_bogus", _CLAUDE_FULL_MENU_PANE
            )
            is None
        )


# Verbatim capture from Codex CLI v0.138.0 in a tmux pane: the single
# plan-mode approval menu shown after `/plan` proposes a plan.
_CODEX_PLAN_MENU_PANE = """\
  Implement this plan?

› 1. Yes, implement this plan          Switch to Default and start coding.
  2. Yes, clear context and implement  Fresh thread. Context: 2% used.
  3. No, stay in Plan mode             Continue planning with the model.
  Press enter to confirm or esc to go back
"""


class TestCodexPlanMenu:
    """Codex's single-shape plan-mode approval menu -- a guarded static map."""

    def test_codex_is_registered_static(self) -> None:
        assert DEFAULT_PLAN_KEYSTROKES.has_source("codex") is True
        assert DEFAULT_PLAN_KEYSTROKES.requires_pane("codex") is True

    def test_registered_options(self) -> None:
        assert DEFAULT_PLAN_KEYSTROKES.registered_options("codex") == frozenset(
            {"approve_yolo", "approve_act", REQUEST_CHANGES_OPTION_ID}
        )

    @pytest.mark.parametrize(
        ("option_id", "expected"),
        [
            # Codex's plan menu has a single "implement" approve, so both
            # approves collapse onto "1"; request-changes is "3" (stay in Plan
            # mode so the user can send revision feedback).
            ("approve_yolo", _codex("1")),
            ("approve_act", _codex("1")),
            (REQUEST_CHANGES_OPTION_ID, _codex("3")),
        ],
    )
    def test_plan_menu_mapping(self, option_id: str, expected: PlanKeystrokeSequence) -> None:
        assert DEFAULT_PLAN_KEYSTROKES.resolve("codex", option_id) == expected

    @pytest.mark.parametrize(
        ("option_id", "digit"),
        [("approve_yolo", "1"), ("approve_act", "1"), (REQUEST_CHANGES_OPTION_ID, "3")],
    )
    def test_digit_activates_without_enter(self, option_id: str, digit: str) -> None:
        # Verified live: the item number selects AND activates with no trailing
        # Enter (unlike Claude's full menu).
        seq = DEFAULT_PLAN_KEYSTROKES.resolve("codex", option_id)
        assert seq is not None
        assert [s.keys for s in seq.strokes] == [digit]
        assert all(s.literal for s in seq.strokes)

    def test_static_resolution_requires_matching_pane_text(self) -> None:
        assert (
            DEFAULT_PLAN_KEYSTROKES.resolve_for_pane("codex", "approve_yolo", _CODEX_PLAN_MENU_PANE)
            == DEFAULT_PLAN_KEYSTROKES.resolve("codex", "approve_yolo")
            == _codex("1")
        )
        assert DEFAULT_PLAN_KEYSTROKES.resolve_for_pane("codex", "approve_act", "") is None

    def test_unknown_option_returns_none(self) -> None:
        assert DEFAULT_PLAN_KEYSTROKES.resolve("codex", "approve_bogus") is None


# Verbatim capture from Droid CLI v0.137.1 (Factory, spec model Opus 4.8) in a
# tmux pane: the single spec-mode approval menu shown after the agent proposes a
# spec in `droid --use-spec`.
_DROID_PLAN_MENU_PANE = """\
 1. Proceed with the proposal
 2. Proceed with comment
 3. Manually edit spec (open via system default)
 4. No and explain why
   up/down navigate   1-4 select   Enter select   Tab reasoning   Esc cancel
"""


class TestDroidPlanMenu:
    """Droid's single-shape spec-mode approval menu -- a guarded static map."""

    def test_droid_is_registered_static(self) -> None:
        assert DEFAULT_PLAN_KEYSTROKES.has_source("droid") is True
        assert DEFAULT_PLAN_KEYSTROKES.requires_pane("droid") is True

    def test_registered_options(self) -> None:
        assert DEFAULT_PLAN_KEYSTROKES.registered_options("droid") == frozenset(
            {"approve_yolo", "approve_act", REQUEST_CHANGES_OPTION_ID}
        )

    @pytest.mark.parametrize(
        ("option_id", "expected"),
        [
            # Droid's spec menu has a single "proceed" approve, so both approves
            # collapse onto "1"; request-changes is "4" ("No and explain why" ->
            # stay in spec mode so the user can send revision feedback).
            ("approve_yolo", _droid("1")),
            ("approve_act", _droid("1")),
            (REQUEST_CHANGES_OPTION_ID, _droid("4")),
        ],
    )
    def test_plan_menu_mapping(self, option_id: str, expected: PlanKeystrokeSequence) -> None:
        assert DEFAULT_PLAN_KEYSTROKES.resolve("droid", option_id) == expected

    @pytest.mark.parametrize(
        ("option_id", "digit"),
        [("approve_yolo", "1"), ("approve_act", "1"), (REQUEST_CHANGES_OPTION_ID, "4")],
    )
    def test_digit_activates_without_enter(self, option_id: str, digit: str) -> None:
        # Verified live: the item number selects AND activates with no trailing
        # Enter ("Enter select" in the footer applies only to arrow-key nav).
        seq = DEFAULT_PLAN_KEYSTROKES.resolve("droid", option_id)
        assert seq is not None
        assert [s.keys for s in seq.strokes] == [digit]
        assert all(s.literal for s in seq.strokes)

    def test_static_resolution_requires_matching_pane_text(self) -> None:
        assert (
            DEFAULT_PLAN_KEYSTROKES.resolve_for_pane("droid", "approve_yolo", _DROID_PLAN_MENU_PANE)
            == DEFAULT_PLAN_KEYSTROKES.resolve("droid", "approve_yolo")
            == _droid("1")
        )
        assert DEFAULT_PLAN_KEYSTROKES.resolve_for_pane("droid", "approve_act", "") is None

    def test_unknown_option_returns_none(self) -> None:
        assert DEFAULT_PLAN_KEYSTROKES.resolve("droid", "approve_bogus") is None


# Verbatim captures from a retired ACP CLI in a tmux pane:
# the per-action approval menus shown in manual/default mode. The reject option's
# DIGIT differs between tool types (4 vs 3) while "Allow once"/"Allow for this
# session" stay at positions 1/2 and the "(esc)" shortcut always rejects.
# Verbatim captures from Grok ("Grok Build" Beta 0.2.38, xAI) driven on a pty in
# `--permission-mode default`: the per-action approval menus. The option numbers
# (1 always-approve / 3 single-approve / 4 reject) stay positionally stable; only
# option 2's scope-specific wording changes between tool types.
_GROK_SHELL_MENU_PANE = """\
 Run the exact user-requested shell command: echo hello-grok-cap
 echo hello-grok-cap
 Use ← → to choose permission whitelist scope

 1 (●) Yes, and don't ask again for anything (always-approve mode)
 2 (○) Always allow: echo hello-grok-cap
 3 (○) Yes, proceed
 4 (○) No, reject (type to add feedback)
"""

_GROK_EDIT_MENU_PANE = """\
 Allow Write `cap_probe.txt`?

 1 (●) Yes, and don't ask again for anything (always-approve mode)
 2 (○) Yes, allow all edits during this session
 3 (○) Yes
 4 (○) No, reject (type to add feedback)
"""


class TestGrokPlanMenu:
    """Grok's ("Grok Build" TUI) per-action approval menu -- a static (non-pane)
    map whose option numbers (1 yolo / 3 approve / 4 reject) are positionally
    stable across menu shapes, so every action is a single immediate digit."""

    def test_grok_is_registered_static(self) -> None:
        assert DEFAULT_PLAN_KEYSTROKES.has_source("grok") is True
        assert DEFAULT_PLAN_KEYSTROKES.requires_pane("grok") is True

    def test_registered_options(self) -> None:
        assert DEFAULT_PLAN_KEYSTROKES.registered_options("grok") == frozenset(
            {"approve_yolo", "approve_act", REQUEST_CHANGES_OPTION_ID}
        )

    @pytest.mark.parametrize(
        ("option_id", "expected"),
        [
            # 1 = "always-approve mode" (bypass) -> approve_yolo; 3 = "Yes,
            # proceed"/"Yes" (single approval) -> approve_act; 4 = "No, reject".
            ("approve_yolo", _grok("1")),
            ("approve_act", _grok("3")),
            (REQUEST_CHANGES_OPTION_ID, _grok("4")),
        ],
    )
    def test_plan_menu_mapping(self, option_id: str, expected: PlanKeystrokeSequence) -> None:
        assert DEFAULT_PLAN_KEYSTROKES.resolve("grok", option_id) == expected

    @pytest.mark.parametrize(
        ("option_id", "digit"),
        [("approve_yolo", "1"), ("approve_act", "3"), (REQUEST_CHANGES_OPTION_ID, "4")],
    )
    def test_digit_activates_without_enter(self, option_id: str, digit: str) -> None:
        # Verified live: the item number selects AND activates with no trailing
        # Enter -- approves (digit 3 ran an echo) and reject alike (digit 4 denied
        # a write; the file was never created).
        seq = DEFAULT_PLAN_KEYSTROKES.resolve("grok", option_id)
        assert seq is not None
        assert [s.keys for s in seq.strokes] == [digit]
        assert all(s.literal for s in seq.strokes)

    def test_reject_uses_stable_digit_not_escape(self) -> None:
        # Grok's reject is the stable digit 4 (identical "No,
        # reject" item across menu shapes); Esc only "unselects" the radio.
        seq = DEFAULT_PLAN_KEYSTROKES.resolve("grok", REQUEST_CHANGES_OPTION_ID)
        assert seq is not None
        assert [s.keys for s in seq.strokes] == ["4"]
        assert all(s.literal for s in seq.strokes)

    def test_static_resolution_requires_matching_pane_text(self) -> None:
        assert (
            DEFAULT_PLAN_KEYSTROKES.resolve_for_pane("grok", "approve_act", _GROK_EDIT_MENU_PANE)
            == DEFAULT_PLAN_KEYSTROKES.resolve_for_pane(
                "grok", "approve_act", _GROK_SHELL_MENU_PANE
            )
            == DEFAULT_PLAN_KEYSTROKES.resolve("grok", "approve_act")
            == _grok("3")
        )
        assert (
            DEFAULT_PLAN_KEYSTROKES.resolve_for_pane("grok", REQUEST_CHANGES_OPTION_ID, "") is None
        )

    def test_unknown_option_returns_none(self) -> None:
        assert DEFAULT_PLAN_KEYSTROKES.resolve("grok", "approve_bogus") is None


# Verbatim capture from Qwen Code (Qwen CLI TUI, v0.17.0) driven on a pty in
# `--approval-mode default` against a working local LM Studio backend: the
# WriteFile tool-approval menu. Qwen uses RadioButtonSelect (› marks the
# default-highlighted
# item). A `run_shell_command` echo auto-approved in default mode, so only the
# write/edit menu shape was observed.
_QWEN_EDIT_MENU_PANE = """\
 ?  WriteFile Writing to cap_probe.txt

 1 hello

 Apply this change?

 › 1. Yes, allow once
   2. Yes, allow always
   3. No, suggest changes (esc)
"""


class TestQwenPlanMenu:
    """Qwen Code's (Qwen CLI TUI) per-action approval menu -- a static (non-pane)
    map. Qwen's confirmation menu uses digit 1 for "Yes, allow once" (single
    approval), digit 2 for "Yes,
    allow always" (bypass), and Escape rejects (the menu's "(esc)" shortcut)."""

    def test_qwen_is_registered_static(self) -> None:
        assert DEFAULT_PLAN_KEYSTROKES.has_source("qwen") is True
        assert DEFAULT_PLAN_KEYSTROKES.requires_pane("qwen") is True

    def test_registered_options(self) -> None:
        assert DEFAULT_PLAN_KEYSTROKES.registered_options("qwen") == frozenset(
            {"approve_yolo", "approve_act", REQUEST_CHANGES_OPTION_ID}
        )

    @pytest.mark.parametrize(
        ("option_id", "expected"),
        [
            # 1 = "Yes, allow once" (single approval) -> approve_act; 2 = "Yes,
            # allow always" (bypass) -> approve_yolo; Escape = "No, suggest
            # changes (esc)" -> reject.
            ("approve_act", _qwen("1")),
            ("approve_yolo", _qwen("2")),
            (REQUEST_CHANGES_OPTION_ID, _qwen_escape()),
        ],
    )
    def test_plan_menu_mapping(self, option_id: str, expected: PlanKeystrokeSequence) -> None:
        assert DEFAULT_PLAN_KEYSTROKES.resolve("qwen", option_id) == expected

    @pytest.mark.parametrize(
        ("option_id", "digit"),
        [("approve_act", "1"), ("approve_yolo", "2")],
    )
    def test_digit_activates_without_enter(self, option_id: str, digit: str) -> None:
        # Verified live: the item number selects AND activates with no trailing
        # Enter (digit "1" and digit "2" each approved and wrote the probe file).
        seq = DEFAULT_PLAN_KEYSTROKES.resolve("qwen", option_id)
        assert seq is not None
        assert [s.keys for s in seq.strokes] == [digit]
        assert all(s.literal for s in seq.strokes)

    def test_reject_uses_escape(self) -> None:
        # Qwen's reject is the shape-
        # independent Escape -- verified live: Esc on the write menu cancelled
        # the write and the probe file was never created. Escape is a key NAME
        # (literal=False), not a typed character.
        seq = DEFAULT_PLAN_KEYSTROKES.resolve("qwen", REQUEST_CHANGES_OPTION_ID)
        assert seq is not None
        assert [s.keys for s in seq.strokes] == ["Escape"]
        assert all(not s.literal for s in seq.strokes)

    def test_static_resolution_requires_matching_pane_text(self) -> None:
        assert (
            DEFAULT_PLAN_KEYSTROKES.resolve_for_pane("qwen", "approve_act", _QWEN_EDIT_MENU_PANE)
            == DEFAULT_PLAN_KEYSTROKES.resolve("qwen", "approve_act")
            == _qwen("1")
        )
        assert (
            DEFAULT_PLAN_KEYSTROKES.resolve_for_pane("qwen", REQUEST_CHANGES_OPTION_ID, "") is None
        )

    def test_unknown_option_returns_none(self) -> None:
        assert DEFAULT_PLAN_KEYSTROKES.resolve("qwen", "approve_bogus") is None


class TestNativeNumberedPlanOptions:
    @pytest.mark.parametrize(
        ("source", "option", "pane_text", "expected"),
        [
            ("claude", 3, _CLAUDE_FULL_MENU_PANE, _full("3")),
            ("claude", 2, _CLAUDE_CONFIRM_MENU_PANE, _confirm("2")),
            ("codex", 1, _CODEX_PLAN_MENU_PANE, _codex("1")),
            ("codex", 2, _CODEX_PLAN_MENU_PANE, _codex("2")),
            ("codex", 3, _CODEX_PLAN_MENU_PANE, _codex("3")),
            ("droid", 2, _DROID_PLAN_MENU_PANE, _droid("2")),
            ("droid", 3, _DROID_PLAN_MENU_PANE, _droid("3")),
            ("grok", 2, _GROK_EDIT_MENU_PANE, _grok("2")),
            ("qwen", 1, _QWEN_EDIT_MENU_PANE, _qwen("1")),
            ("qwen", 2, _QWEN_EDIT_MENU_PANE, _qwen("2")),
            ("qwen", 3, _QWEN_EDIT_MENU_PANE, _qwen_escape()),
        ],
    )
    def test_resolves_exact_live_provider_choice(
        self,
        source: str,
        option: int,
        pane_text: str,
        expected: PlanKeystrokeSequence,
    ) -> None:
        assert (
            DEFAULT_PLAN_KEYSTROKES.resolve_native_option_for_pane(
                source,
                option,
                pane_text,
            )
            == expected
        )

    @pytest.mark.parametrize(
        ("source", "option", "pane_text"),
        [
            ("agy", 1, _CODEX_PLAN_MENU_PANE),
            ("codex", 4, _CODEX_PLAN_MENU_PANE),
            ("codex", 1, "ordinary prompt"),
            ("qwen", 0, _QWEN_EDIT_MENU_PANE),
        ],
    )
    def test_rejects_unregistered_stale_or_out_of_range_choice(
        self,
        source: str,
        option: int,
        pane_text: str,
    ) -> None:
        assert (
            DEFAULT_PLAN_KEYSTROKES.resolve_native_option_for_pane(
                source,
                option,
                pane_text,
            )
            is None
        )


class TestAgyPlanMenu:
    """AGY artifact review: ctrl+r opens Action required, y approves, n rejects."""

    def test_agy_is_registered(self) -> None:
        assert DEFAULT_PLAN_KEYSTROKES.has_source("agy") is True
        assert DEFAULT_PLAN_KEYSTROKES.requires_pane("agy") is True

    def test_registered_options(self) -> None:
        assert DEFAULT_PLAN_KEYSTROKES.registered_options("agy") == frozenset(
            {"approve_yolo", "approve_act", REQUEST_CHANGES_OPTION_ID}
        )

    @pytest.mark.parametrize(
        ("option_id", "expected"),
        [
            ("approve_yolo", _agy_approve()),
            ("approve_act", _agy_approve()),
            (REQUEST_CHANGES_OPTION_ID, _agy_reject()),
        ],
    )
    def test_plan_menu_mapping(self, option_id: str, expected: PlanKeystrokeSequence) -> None:
        assert (
            DEFAULT_PLAN_KEYSTROKES.resolve_for_pane("agy", option_id, _AGY_PLAN_MENU_PANE)
            == expected
        )

    def test_matcher_requires_action_required_header(self) -> None:
        assert DEFAULT_PLAN_KEYSTROKES.resolve_for_pane("agy", "approve_act", "") is None
        assert (
            DEFAULT_PLAN_KEYSTROKES.resolve_for_pane("agy", "approve_act", "ordinary prompt")
            is None
        )

    def test_native_options_map_approve_and_reject(self) -> None:
        assert (
            DEFAULT_PLAN_KEYSTROKES.resolve_native_option_for_pane(
                "agy",
                1,
                _AGY_PLAN_MENU_PANE,
            )
            == _agy_approve()
        )
        assert (
            DEFAULT_PLAN_KEYSTROKES.resolve_native_option_for_pane(
                "agy",
                2,
                _AGY_PLAN_MENU_PANE,
            )
            == _agy_reject()
        )


class TestDefaultRegistry:
    def test_all_clis_registered(self) -> None:
        # Every managed CLI now has a captured native plan-menu mapping: claude
        # (#15727), codex (#15728), droid (#15729), grok (#15731), qwen
        # (#15732), and agy (#20755). No per-CLI source remains pending.
        for source in ("claude", "codex", "droid", "grok", "qwen", "agy"):
            assert DEFAULT_PLAN_KEYSTROKES.has_source(source) is True
