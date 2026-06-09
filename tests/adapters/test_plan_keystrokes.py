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

    def test_resolve_for_pane_falls_back_to_static(self) -> None:
        registry = PlanKeystrokeRegistry()
        seq = PlanKeystrokeSequence((PlanKeystroke("1"),))
        registry.register("static", "approve_yolo", seq)
        # No resolver, so pane_text is ignored and the static map is used.
        assert registry.requires_pane("static") is False
        assert registry.resolve_for_pane("static", "approve_yolo", "anything") is seq

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

    def test_approve_without_option_is_none(self) -> None:
        assert resolve_action_option_id("codex", "approve", None) is None

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


class TestDefaultRegistry:
    def test_remaining_clis_pending(self) -> None:
        # Claude (#15727) is registered; the other per-CLI child tasks populate
        # these. This asserts the contract so an accidental population is caught.
        for source in ("codex", "droid", "gemini", "grok", "qwen"):
            assert DEFAULT_PLAN_KEYSTROKES.has_source(source) is False
            assert DEFAULT_PLAN_KEYSTROKES.requires_pane(source) is False
            assert DEFAULT_PLAN_KEYSTROKES.resolve(source, "approve_yolo") is None
