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


class TestDefaultRegistry:
    def test_default_has_no_per_cli_values_yet(self) -> None:
        # The framework ships empty; per-CLI child tasks populate these. This
        # asserts the contract so an accidental population is caught here.
        for source in ("claude", "codex", "droid", "gemini", "grok", "qwen"):
            assert DEFAULT_PLAN_KEYSTROKES.has_source(source) is False
            assert DEFAULT_PLAN_KEYSTROKES.resolve(source, "approve_yolo") is None
