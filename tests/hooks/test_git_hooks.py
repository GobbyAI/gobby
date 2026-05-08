"""Tests for git merge hook manager.

Exercises the real MergeHookManager with real callback functions.
No mocking needed -- this is pure in-process logic.
"""

from __future__ import annotations

import pytest

from gobby.hooks.git import (
    MergeHookManager,
    get_merge_hook_manager,
    reset_merge_hook_manager,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_singleton() -> None:
    """Reset the global singleton before each test."""
    reset_merge_hook_manager()


# =============================================================================
# Helper callbacks
# =============================================================================


def _pre_hook_pass(wt: str, s: str, t: str) -> bool:
    return True


def _pre_hook_block(wt: str, s: str, t: str) -> bool:
    return False


def _post_hook_noop(rid: str, success: bool) -> None:
    pass


# =============================================================================
# Registration / Unregistration
# =============================================================================


class TestRegistration:
    def test_register_pre_merge(self) -> None:
        mgr = MergeHookManager()
        mgr.register_pre_merge(_pre_hook_pass)
        assert mgr.pre_merge_hook_count == 1

    def test_register_multiple_pre_merge(self) -> None:
        mgr = MergeHookManager()
        mgr.register_pre_merge(_pre_hook_pass)
        mgr.register_pre_merge(_pre_hook_block)
        assert mgr.pre_merge_hook_count == 2

    def test_register_post_merge(self) -> None:
        mgr = MergeHookManager()
        mgr.register_post_merge(_post_hook_noop)
        assert mgr.post_merge_hook_count == 1

    def test_register_multiple_post_merge(self) -> None:
        mgr = MergeHookManager()

        def hook1(rid: str, success: bool) -> None:
            pass

        def hook2(rid: str, success: bool) -> None:
            pass

        mgr.register_post_merge(hook1)
        mgr.register_post_merge(hook2)
        assert mgr.post_merge_hook_count == 2

    def test_unregister_pre_merge_success(self) -> None:
        mgr = MergeHookManager()
        mgr.register_pre_merge(_pre_hook_pass)
        assert mgr.unregister_pre_merge(_pre_hook_pass) is True
        assert mgr.pre_merge_hook_count == 0

    def test_unregister_pre_merge_not_found(self) -> None:
        mgr = MergeHookManager()
        assert mgr.unregister_pre_merge(_pre_hook_pass) is False

    def test_unregister_post_merge_success(self) -> None:
        mgr = MergeHookManager()
        mgr.register_post_merge(_post_hook_noop)
        assert mgr.unregister_post_merge(_post_hook_noop) is True
        assert mgr.post_merge_hook_count == 0

    def test_unregister_post_merge_not_found(self) -> None:
        mgr = MergeHookManager()
        assert mgr.unregister_post_merge(_post_hook_noop) is False


# =============================================================================
# Hook counts
# =============================================================================


class TestHookCounts:
    def test_initial_counts_zero(self) -> None:
        mgr = MergeHookManager()
        assert mgr.pre_merge_hook_count == 0
        assert mgr.post_merge_hook_count == 0

    def test_counts_after_register_and_unregister(self) -> None:
        mgr = MergeHookManager()
        mgr.register_pre_merge(_pre_hook_pass)
        mgr.register_post_merge(_post_hook_noop)
        assert mgr.pre_merge_hook_count == 1
        assert mgr.post_merge_hook_count == 1
        mgr.unregister_pre_merge(_pre_hook_pass)
        mgr.unregister_post_merge(_post_hook_noop)
        assert mgr.pre_merge_hook_count == 0
        assert mgr.post_merge_hook_count == 0


# =============================================================================
# Pre-merge hook execution
# =============================================================================


class TestRunPreMergeHooks:
    def test_no_hooks_allows(self) -> None:
        mgr = MergeHookManager()
        allowed, reason = mgr.run_pre_merge_hooks("wt1", "feature", "main")
        assert allowed is True
        assert reason is None

    def test_all_pass(self) -> None:
        mgr = MergeHookManager()

        def hook1(wt: str, s: str, t: str) -> bool:
            return True

        def hook2(wt: str, s: str, t: str) -> bool:
            return True

        mgr.register_pre_merge(hook1)
        mgr.register_pre_merge(hook2)
        allowed, reason = mgr.run_pre_merge_hooks("wt1", "feature", "main")
        assert allowed is True
        assert reason is None

    def test_one_blocks(self) -> None:
        mgr = MergeHookManager()
        mgr.register_pre_merge(_pre_hook_pass)
        mgr.register_pre_merge(_pre_hook_block)
        allowed, reason = mgr.run_pre_merge_hooks("wt1", "feature", "main")
        assert allowed is False
        assert reason is not None
        assert "_pre_hook_block" in reason

    def test_first_hook_blocks_skips_rest(self) -> None:
        """When the first hook blocks, subsequent hooks are not called."""
        mgr = MergeHookManager()
        calls: list[str] = []

        def blocker(wt: str, s: str, t: str) -> bool:
            calls.append("blocker")
            return False

        def should_not_run(wt: str, s: str, t: str) -> bool:
            calls.append("second")
            return True

        mgr.register_pre_merge(blocker)
        mgr.register_pre_merge(should_not_run)
        allowed, reason = mgr.run_pre_merge_hooks("wt1", "feat", "main")
        assert allowed is False
        assert calls == ["blocker"]

    def test_receives_correct_arguments(self) -> None:
        mgr = MergeHookManager()
        received: list[tuple[str, str, str]] = []

        def capture(wt: str, s: str, t: str) -> bool:
            received.append((wt, s, t))
            return True

        mgr.register_pre_merge(capture)
        mgr.run_pre_merge_hooks("wt-123", "feature/x", "main")
        assert received == [("wt-123", "feature/x", "main")]

    def test_exception_continues_to_next(self) -> None:
        mgr = MergeHookManager()

        def bad_hook(wt: str, s: str, t: str) -> bool:
            raise RuntimeError("boom")

        mgr.register_pre_merge(bad_hook)
        mgr.register_pre_merge(_pre_hook_pass)
        allowed, reason = mgr.run_pre_merge_hooks("wt1", "feature", "main")
        # Exception is swallowed, execution continues to the passing hook
        assert allowed is True
        assert reason is None

    def test_exception_does_not_block(self) -> None:
        """An exception in a hook does NOT count as blocking."""
        mgr = MergeHookManager()

        def bad_hook(wt: str, s: str, t: str) -> bool:
            raise ValueError("invalid")

        mgr.register_pre_merge(bad_hook)
        allowed, reason = mgr.run_pre_merge_hooks("wt1", "feat", "main")
        assert allowed is True

    def test_blocking_reason_contains_hook_name(self) -> None:
        mgr = MergeHookManager()

        def my_validation_hook(wt: str, s: str, t: str) -> bool:
            return False

        mgr.register_pre_merge(my_validation_hook)
        _, reason = mgr.run_pre_merge_hooks("wt1", "feat", "main")
        assert "my_validation_hook" in reason


# =============================================================================
# Post-merge hook execution
# =============================================================================


class TestRunPostMergeHooks:
    def test_no_hooks(self) -> None:
        mgr = MergeHookManager()
        mgr.run_post_merge_hooks("res1", True)
        assert mgr.post_merge_hook_count == 0

    def test_calls_all_hooks(self) -> None:
        mgr = MergeHookManager()
        calls: list[tuple[str, bool]] = []

        def hook1(rid: str, success: bool) -> None:
            calls.append((rid, success))

        def hook2(rid: str, success: bool) -> None:
            calls.append((rid, success))

        mgr.register_post_merge(hook1)
        mgr.register_post_merge(hook2)
        mgr.run_post_merge_hooks("res1", True)
        assert len(calls) == 2
        assert calls[0] == ("res1", True)
        assert calls[1] == ("res1", True)

    def test_calls_with_failure(self) -> None:
        mgr = MergeHookManager()
        calls: list[tuple[str, bool]] = []

        def hook(rid: str, success: bool) -> None:
            calls.append((rid, success))

        mgr.register_post_merge(hook)
        mgr.run_post_merge_hooks("res-fail", False)
        assert calls == [("res-fail", False)]

    def test_exception_continues_to_next(self) -> None:
        mgr = MergeHookManager()
        calls: list[str] = []

        def bad_hook(rid: str, success: bool) -> None:
            raise RuntimeError("boom")

        def good_hook(rid: str, success: bool) -> None:
            calls.append(rid)

        mgr.register_post_merge(bad_hook)
        mgr.register_post_merge(good_hook)
        mgr.run_post_merge_hooks("res1", False)
        assert calls == ["res1"]

    def test_all_hooks_exception(self) -> None:
        """Even if all hooks raise, run_post_merge_hooks does not raise."""
        mgr = MergeHookManager()

        def bad1(rid: str, success: bool) -> None:
            raise RuntimeError("boom1")

        def bad2(rid: str, success: bool) -> None:
            raise ValueError("boom2")

        mgr.register_post_merge(bad1)
        mgr.register_post_merge(bad2)
        mgr.run_post_merge_hooks("res1", True)
        assert mgr.post_merge_hook_count == 2


# =============================================================================
# Singleton management
# =============================================================================


class TestSingleton:
    def test_get_returns_same_instance(self) -> None:
        mgr1 = get_merge_hook_manager()
        mgr2 = get_merge_hook_manager()
        assert mgr1 is mgr2

    def test_reset_creates_new_instance(self) -> None:
        mgr1 = get_merge_hook_manager()
        reset_merge_hook_manager()
        mgr2 = get_merge_hook_manager()
        assert mgr1 is not mgr2

    def test_reset_clears_hooks(self) -> None:
        mgr = get_merge_hook_manager()
        mgr.register_pre_merge(_pre_hook_pass)
        mgr.register_post_merge(_post_hook_noop)
        reset_merge_hook_manager()
        mgr2 = get_merge_hook_manager()
        assert mgr2.pre_merge_hook_count == 0
        assert mgr2.post_merge_hook_count == 0

    def test_get_after_double_reset(self) -> None:
        reset_merge_hook_manager()
        reset_merge_hook_manager()
        mgr = get_merge_hook_manager()
        assert isinstance(mgr, MergeHookManager)


# =============================================================================
# Init state
# =============================================================================


class TestInit:
    def test_fresh_manager_empty(self) -> None:
        mgr = MergeHookManager()
        assert mgr.pre_merge_hook_count == 0
        assert mgr.post_merge_hook_count == 0
