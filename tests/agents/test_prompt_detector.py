"""Tests for gobby.agents.prompt_detector module.

Tests for the PromptDetector that identifies blocking CLI prompts
(e.g. folder trust dialogs) in tmux pane output.
"""

from __future__ import annotations

import pytest

from gobby.agents.prompt_detector import PromptDetector

pytestmark = pytest.mark.unit


class TestDetectTrustPrompt:
    """Tests for trust prompt pattern matching."""

    def test_detects_claude_trust_prompt(self) -> None:
        """Claude Code's exact trust prompt text is detected."""
        detector = PromptDetector()
        output = (
            "╭──────────────────────────────────────╮\n"
            "│ Do you trust the files in this folder?│\n"
            "│                                      │\n"
            "│ 1. Trust Folder                      │\n"
            "│ 2. Trust parent Folder                │\n"
            "│ 3. Don't Trust                       │\n"
            "╰──────────────────────────────────────╯\n"
        )
        assert detector.detect_trust_prompt(output) is True

    def test_detects_new_claude_workspace_prompt(self) -> None:
        """Claude Code's newer 'Is this a project...' prompt is detected."""
        detector = PromptDetector()
        output = "Is this a project you created or one you trust?\n"
        assert detector.detect_trust_prompt(output) is True

    def test_detects_case_insensitive(self) -> None:
        """Detection is case-insensitive."""
        detector = PromptDetector()
        assert detector.detect_trust_prompt("do you trust the files in /some/path?") is True
        assert detector.detect_trust_prompt("DO YOU TRUST THE FILES") is True

    def test_detects_trust_folder_variant(self) -> None:
        """Detects 'Trust Folder' / 'trust folder' patterns."""
        detector = PromptDetector()
        assert detector.detect_trust_prompt("Trust parent Folder") is True
        assert detector.detect_trust_prompt("1. Trust Folder") is True
        assert detector.detect_trust_prompt("trust this folder") is True

    def test_no_match_on_normal_output(self) -> None:
        """Normal agent output does not trigger detection."""
        detector = PromptDetector()
        assert detector.detect_trust_prompt("Running tests...\n$ pytest -v\n") is False
        assert detector.detect_trust_prompt("Reading file /src/main.py\n") is False
        assert detector.detect_trust_prompt("") is False

    def test_no_match_on_idle_prompt(self) -> None:
        """Idle prompt (handled by IdleDetector) does not trigger."""
        detector = PromptDetector()
        assert detector.detect_trust_prompt("❯\n") is False
        assert detector.detect_trust_prompt("$\n") is False

    def test_detects_prompt_embedded_in_output(self) -> None:
        """Trust prompt surrounded by other output is still detected."""
        detector = PromptDetector()
        output = (
            "Starting Claude Code...\n"
            "Loading configuration...\n"
            "Do you trust the files in this folder?\n"
            "1. Trust Folder\n"
            "2. Trust parent Folder\n"
        )
        assert detector.detect_trust_prompt(output) is True


class TestDetectLoopPrompt:
    """Tests for loop detection pattern matching."""

    def test_detects_stuck_in_loop(self) -> None:
        detector = PromptDetector()
        assert detector.detect_loop_prompt("It seems like I'm stuck in a loop.") is True

    def test_detects_repeating_myself(self) -> None:
        detector = PromptDetector()
        assert detector.detect_loop_prompt("I think I'm repeating myself.") is True

    def test_detects_potential_loop(self) -> None:
        detector = PromptDetector()
        assert detector.detect_loop_prompt("Potential loop detected. Continue? (y/n)") is True

    def test_detects_seems_stuck(self) -> None:
        detector = PromptDetector()
        assert detector.detect_loop_prompt("It seems to be stuck.") is True
        assert detector.detect_loop_prompt("The agent seem to be looping.") is True
        assert detector.detect_loop_prompt("This seems to be repeating.") is True

    def test_case_insensitive(self) -> None:
        detector = PromptDetector()
        assert detector.detect_loop_prompt("STUCK IN A LOOP") is True
        assert detector.detect_loop_prompt("Potential Loop Detected") is True

    def test_no_match_on_normal_output(self) -> None:
        detector = PromptDetector()
        assert detector.detect_loop_prompt("Running tests...\n$ pytest -v\n") is False
        assert detector.detect_loop_prompt("Loop iteration 5 complete\n") is False
        assert detector.detect_loop_prompt("") is False

    def test_embedded_in_output(self) -> None:
        detector = PromptDetector()
        output = "Processing files...\nWarning: potential loop detected\nContinue? (y/n)\n"
        assert detector.detect_loop_prompt(output) is True


class TestDetectApprovalPrompt:
    """Tests for approval prompt pattern matching."""

    def test_detects_enter_to_approve_command(self) -> None:
        detector = PromptDetector()
        output = "Approval required\nPress Enter to approve this command\n"
        assert detector.detect_approval_prompt(output) is True

    def test_detects_enter_to_allow_tool_request(self) -> None:
        detector = PromptDetector()
        output = "Permission required for tool request\nEnter to allow and continue\n"
        assert detector.detect_approval_prompt(output) is True

    def test_detects_codex_tui_tool_call_confirmation(self) -> None:
        detector = PromptDetector()
        output = (
            "Field 1/1\n"
            "Tool call needs your approval. Reason: Request contains encrypted reasoning "
            "and a tool call; requires user confirmation to proceed.\n"
            "› 1. Allow   Run the tool and continue.\n"
            "  2. Cancel  Cancel this tool call\n"
            "enter to submit | esc to cancel\n"
        )
        assert detector.detect_approval_prompt(output) is True

    def test_no_match_without_approval_context(self) -> None:
        detector = PromptDetector()
        assert detector.detect_approval_prompt("Press Enter to continue\n") is False
        assert detector.detect_approval_prompt("enter to submit | esc to cancel\n") is False

    def test_no_match_without_enter_approval_action(self) -> None:
        detector = PromptDetector()
        assert detector.detect_approval_prompt("Approval status: tests passed\n") is False

    def test_trust_and_loop_prompts_do_not_match_approval(self) -> None:
        detector = PromptDetector()
        trust_output = "Do you trust the files in this folder?\n1. Trust Folder\n"
        loop_output = "Potential loop detected. Continue? (y/n)\n"

        assert detector.detect_trust_prompt(trust_output) is True
        assert detector.detect_approval_prompt(trust_output) is False
        assert detector.detect_loop_prompt(loop_output) is True
        assert detector.detect_approval_prompt(loop_output) is False


class TestDismissedTracking:
    """Tests for the dismissed state tracking."""

    def test_not_dismissed_by_default(self) -> None:
        detector = PromptDetector()
        assert detector.was_dismissed("run-123") is False

    def test_mark_dismissed(self) -> None:
        detector = PromptDetector()
        detector.mark_dismissed("run-123")
        assert detector.was_dismissed("run-123") is True

    def test_clear_removes_tracking(self) -> None:
        detector = PromptDetector()
        detector.mark_dismissed("run-123")
        detector.clear("run-123")
        assert detector.was_dismissed("run-123") is False

    def test_clear_nonexistent_is_noop(self) -> None:
        """Clearing a run_id that was never tracked doesn't raise."""
        detector = PromptDetector()
        detector.clear("run-never-seen")  # Should not raise

    def test_independent_tracking(self) -> None:
        """Dismissed state is per-agent, not global."""
        detector = PromptDetector()
        detector.mark_dismissed("run-a")
        assert detector.was_dismissed("run-a") is True
        assert detector.was_dismissed("run-b") is False

    def test_mark_dismissed_idempotent(self) -> None:
        """Marking the same run_id twice doesn't raise or change state."""
        detector = PromptDetector()
        detector.mark_dismissed("run-123")
        detector.mark_dismissed("run-123")
        assert detector.was_dismissed("run-123") is True

    def test_approval_prompt_fingerprint_tracking(self) -> None:
        detector = PromptDetector()
        prompt = "Approval required\nPress Enter to approve command A\n"

        assert detector.was_approval_prompt_dismissed("run-1", prompt) is False
        detector.mark_approval_prompt_dismissed("run-1", prompt)
        assert detector.was_approval_prompt_dismissed("run-1", prompt) is True
        assert (
            detector.was_approval_prompt_dismissed(
                "run-1",
                "Approval required\nPress Enter to approve command B\n",
            )
            is False
        )

    def test_clear_removes_approval_prompt_fingerprint(self) -> None:
        detector = PromptDetector()
        prompt = "Approval required\nPress Enter to approve command A\n"
        detector.mark_approval_prompt_dismissed("run-1", prompt)

        detector.clear("run-1")

        assert detector.was_approval_prompt_dismissed("run-1", prompt) is False
