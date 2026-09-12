"""Tests for gobby.agents.prompt_detector module.

Tests for the PromptDetector that identifies blocking CLI prompts
(e.g. folder trust dialogs) in tmux pane output.
"""

from __future__ import annotations

import pytest

from gobby.agents.prompt_detector import PromptDetector

from .detection_test_support import BundledDetectionRegistry


def make_detector() -> PromptDetector:
    return PromptDetector(BundledDetectionRegistry(), "claude")


pytestmark = pytest.mark.unit


WORKSPACE_TRUST_PANE = (
    "\u2500" * 80 + "\n"
    " Accessing workspace:\n"
    "\n"
    " /private/tmp/gobby-ap-hsat03zs/gobby/ask/run/scratch/investigator-0\n"
    "\n"
    " Quick safety check: Is this a project you created or one you trust?\n"
    "\n"
    " Security guide\n"
    "\n"
    " \u276f No, exit\n"
    "   Yes, I trust this folder\n"
    "\n"
    " Enter to confirm \u00b7 Esc to cancel\n"
)


# The same dialog as the runtime delivers it. Both pane sources keep SGR: tmux
# captures with ``-e``, and the native host's "text" snapshot is
# ``recent_unwrapped_ansi``. Native Ask probe attempt 26 is the evidence: its
# highlighted row arrived as " \x1b[0m\x1b[38;5;153m\u276f No, exit\x1b[0m",
# reproduced verbatim below, and the monitor answered it with the bare Enter
# that selects "No, exit".
WORKSPACE_TRUST_PANE_WITH_SGR = (
    "\x1b[0m\x1b[38;5;220m" + "\u2500" * 80 + "\x1b[0m\n"
    " \x1b[0m\x1b[1m\x1b[38;5;220mAccessing workspace:\x1b[0m\n"
    "\n"
    " \x1b[0m\x1b[1m/private/tmp/gobby-ap-ne0hsfgx/gobby/ask/run/scratch/investigator-0\x1b[0m\n"
    "\n"
    " Quick safety check: Is this a project you created or one you trust? (Like your\n"
    " own code, a well-known open source project, or work from your team). If not,\n"
    " take a moment to review what's in this folder first.\n"
    "\n"
    " Claude Code'll be able to read, edit, and execute files here.\n"
    "\n"
    " \x1b[0m\x1b[38;5;246mSecurity guide\x1b[0m\n"
    "\n"
    " \x1b[0m\x1b[38;5;153m\u276f No, exit\x1b[0m\n"
    "   Yes, I trust this folder\n"
    "\n"
    " \x1b[0m\x1b[38;5;246mEnter to confirm \u00b7 Esc to cancel\x1b[0m\n"
)


class TestTrustDismissKeys:
    """Tests for the key sequence that answers a trust prompt affirmatively."""

    def test_navigates_to_the_affirmative_row_when_decline_is_selected(self) -> None:
        """Claude's workspace dialog opens on "No, exit", so Enter alone quits."""
        detector = make_detector()

        assert detector.trust_dismiss_keys(WORKSPACE_TRUST_PANE) == ("down", "enter")

    def test_navigates_to_the_affirmative_row_through_the_highlight_sequence(self) -> None:
        """The highlight that marks the selected row is drawn before the marker."""
        detector = make_detector()

        assert detector.trust_dismiss_keys(WORKSPACE_TRUST_PANE_WITH_SGR) == ("down", "enter")

    def test_confirms_directly_when_the_affirmative_row_is_selected(self) -> None:
        """A dialog already sitting on the trust row only needs Enter."""
        detector = make_detector()
        output = " \u276f Yes, I trust this folder\n   No, exit\n"

        assert detector.trust_dismiss_keys(output) == ("enter",)

    def test_moves_up_when_the_affirmative_row_is_above_the_selection(self) -> None:
        """Selection can start below the trust row."""
        detector = make_detector()
        output = "   Yes, I trust this folder\n \u276f No, exit\n"

        assert detector.trust_dismiss_keys(output) == ("up", "enter")

    def test_skips_a_parent_folder_row(self) -> None:
        """Trusting the parent would expose sibling workspaces."""
        detector = make_detector()
        output = " \u276f No, exit\n   Yes, trust parent folder\n   Yes, I trust this folder\n"

        assert detector.trust_dismiss_keys(output) == ("down", "down", "enter")

    def test_falls_back_to_enter_without_a_selection_marker(self) -> None:
        """Enumerated dialogs documenting Enter as acceptance keep working."""
        detector = make_detector()
        output = (
            "Do you trust the files in this folder?\n"
            "1. Trust Folder\n"
            "2. Trust parent Folder\n"
            "3. Don't Trust\n"
        )

        assert detector.trust_dismiss_keys(output) == ("enter",)

    def test_falls_back_to_enter_when_no_row_grants_trust(self) -> None:
        """An unrecognized option list must not be navigated blindly."""
        detector = make_detector()
        output = " \u276f Review the folder\n   Open the security guide\n"

        assert detector.trust_dismiss_keys(output) == ("enter",)


class TestDetectTrustPrompt:
    """Tests for trust prompt pattern matching."""

    def test_detects_claude_trust_prompt(self) -> None:
        """Claude Code's exact trust prompt text is detected."""
        detector = make_detector()
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
        detector = make_detector()
        output = "Is this a project you created or one you trust?\n"
        assert detector.detect_trust_prompt(output) is True

    def test_detects_case_insensitive(self) -> None:
        """Detection is case-insensitive."""
        detector = make_detector()
        assert detector.detect_trust_prompt("do you trust the files in /some/path?") is True
        assert detector.detect_trust_prompt("DO YOU TRUST THE FILES") is True

    def test_detects_trust_folder_variant(self) -> None:
        """Detects 'Trust Folder' / 'trust folder' patterns."""
        detector = make_detector()
        assert detector.detect_trust_prompt("Trust parent Folder") is True
        assert detector.detect_trust_prompt("1. Trust Folder") is True
        assert detector.detect_trust_prompt("trust this folder") is True

    def test_no_match_on_normal_output(self) -> None:
        """Normal agent output does not trigger detection."""
        detector = make_detector()
        assert detector.detect_trust_prompt("Running tests...\n$ pytest -v\n") is False
        assert detector.detect_trust_prompt("Reading file /src/main.py\n") is False
        assert detector.detect_trust_prompt("") is False

    def test_no_match_on_idle_prompt(self) -> None:
        """Idle prompt (handled by IdleDetector) does not trigger."""
        detector = make_detector()
        assert detector.detect_trust_prompt("❯\n") is False
        assert detector.detect_trust_prompt("$\n") is False

    def test_detects_prompt_embedded_in_output(self) -> None:
        """Trust prompt surrounded by other output is still detected."""
        detector = make_detector()
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
        detector = make_detector()
        prompt = "It seems like I'm stuck in a loop.\nContinue? (y/n)\n"
        assert detector.detect_loop_prompt(prompt) is True

    def test_detects_repeating_myself(self) -> None:
        detector = make_detector()
        prompt = "I think I'm repeating myself.\nPress y to continue\n"
        assert detector.detect_loop_prompt(prompt) is True

    def test_detects_potential_loop(self) -> None:
        detector = make_detector()
        assert detector.detect_loop_prompt("Potential loop detected. Continue? (y/n)") is True

    def test_detects_seems_stuck(self) -> None:
        detector = make_detector()
        assert detector.detect_loop_prompt("It seems to be stuck.\nContinue? (y/n)") is True
        assert detector.detect_loop_prompt("The agent seem to be looping.\nContinue? [y/n]") is True
        assert (
            detector.detect_loop_prompt("This seems to be repeating.\nType yes to proceed") is True
        )

    def test_case_insensitive(self) -> None:
        detector = make_detector()
        assert detector.detect_loop_prompt("STUCK IN A LOOP\nCONTINUE? (Y/N)") is True
        assert detector.detect_loop_prompt("Potential Loop Detected\ncontinue? [Y/N]") is True

    def test_no_match_on_normal_output(self) -> None:
        detector = make_detector()
        assert detector.detect_loop_prompt("Running tests...\n$ pytest -v\n") is False
        assert detector.detect_loop_prompt("Loop iteration 5 complete\n") is False
        assert detector.detect_loop_prompt("It seems like I'm stuck in a loop.") is False
        assert detector.detect_loop_prompt("Potential loop detected.") is False
        assert detector.detect_loop_prompt("") is False

    def test_embedded_in_output(self) -> None:
        detector = make_detector()
        output = "Processing files...\nWarning: potential loop detected\nContinue? (y/n)\n"
        assert detector.detect_loop_prompt(output) is True


class TestDetectApprovalPrompt:
    """Tests for approval prompt pattern matching."""

    def test_detects_enter_to_approve_command(self) -> None:
        detector = make_detector()
        output = "Approval required\nPress Enter to approve this command\n"
        assert detector.detect_approval_prompt(output) is True

    def test_detects_enter_to_allow_tool_request(self) -> None:
        detector = make_detector()
        output = "Permission required for tool request\nEnter to allow and continue\n"
        assert detector.detect_approval_prompt(output) is True

    def test_detects_codex_tui_tool_call_confirmation(self) -> None:
        detector = make_detector()
        output = (
            "Field 1/1\n"
            "Tool call needs your approval. Reason: Request contains encrypted reasoning "
            "and a tool call; requires user confirmation to proceed.\n"
            "› 1. Allow   Run the tool and continue.\n"
            "  2. Cancel  Cancel this tool call\n"
            "enter to submit | esc to cancel\n"
        )
        assert detector.detect_approval_prompt(output) is True

    def test_detects_agy_artifact_review_action(self) -> None:
        detector = PromptDetector(BundledDetectionRegistry(), "agy")
        output = (
            "Action required (1 left)\n"
            "› □ new plan_create_plan1_txt.md   open  approve reject\n\n"
            "Keyboard: ↑/↓ Navigate  y/n Approve/reject  shift+a Approve all  "
            "p Preview  ctrl+g open in editor  esc Done\n"
        )
        assert detector.detect_approval_prompt(output) is True

    def test_no_match_without_approval_context(self) -> None:
        detector = make_detector()
        assert detector.detect_approval_prompt("Press Enter to continue\n") is False
        assert detector.detect_approval_prompt("enter to submit | esc to cancel\n") is False

    def test_no_match_without_enter_approval_action(self) -> None:
        detector = make_detector()
        assert detector.detect_approval_prompt("Approval status: tests passed\n") is False

    def test_trust_and_loop_prompts_do_not_match_approval(self) -> None:
        detector = make_detector()
        trust_output = "Do you trust the files in this folder?\n1. Trust Folder\n"
        loop_output = "Potential loop detected. Continue? (y/n)\n"

        assert detector.detect_trust_prompt(trust_output) is True
        assert detector.detect_approval_prompt(trust_output) is False
        assert detector.detect_loop_prompt(loop_output) is True
        assert detector.detect_approval_prompt(loop_output) is False


class TestStructuredPromptDetection:
    """Tests for prompt payloads consumed by attention clients."""

    def test_detects_bounded_enumerated_prompt_payload(self) -> None:
        detector = make_detector()
        history = "\n".join(f"history line {index}" for index in range(20))
        pane_output = (
            f"{history}\n"
            "Tool call needs your approval.\n"
            "1. Allow / 2. Cancel\n"
            "Press Enter to approve this command\n"
        )

        detected = detector.detect_prompt(pane_output)

        assert detected is not None
        assert detected.kind == "approval"
        assert detected.options == (
            {"option": 1, "label": "Allow"},
            {"option": 2, "label": "Cancel"},
        )
        assert detected.fingerprint == detector.pane_fingerprint(pane_output)
        assert len(detected.excerpt.splitlines()) <= detector.PROMPT_EXCERPT_LINES
        assert "history line 0" not in detected.excerpt
        assert detected.to_payload() == {
            "kind": "approval",
            "excerpt": detected.excerpt,
            "options": [
                {"option": 1, "label": "Allow"},
                {"option": 2, "label": "Cancel"},
            ],
            "fingerprint": detected.fingerprint,
        }

    def test_ignores_enumerated_history_outside_prompt_excerpt(self) -> None:
        detector = make_detector()
        pane_output = "\n".join(
            [
                "1. Historical step one",
                "2. Historical step two",
                *(f"ordinary output {index}" for index in range(detector.PROMPT_EXCERPT_LINES)),
            ]
        )

        assert detector.detect_prompt(pane_output) is None

    def test_sorts_enumerated_options_numerically(self) -> None:
        detector = make_detector()

        detected = detector.detect_prompt("Choose a response:\n10. Later\n2. Soon\n1. Now\n")

        assert detected is not None
        assert [option["option"] for option in detected.options] == [1, 2, 10]


class TestDetectQueuedMessagePrompt:
    """Tests for queued-message prompt detection."""

    def test_detects_claude_queued_message_prompt(self) -> None:
        detector = make_detector()

        assert detector.detect_queued_message_prompt("❯ Press up to edit queued messages\n")

    def test_detects_queued_messages_without_full_instruction(self) -> None:
        detector = make_detector()
        output = (
            'submit_for_review(stage_name="planning").\n'
            "Finish the required Gobby lifecycle MCP transition, then call end_agent_run.\n"
            "❯ Press up to edit queued messages\n"
        )

        assert detector.detect_queued_message_prompt(output)
        assert not detector.detect_queued_continuation_prompt(output)

    def test_qwen_plain_queued_message_is_not_a_continuation(self) -> None:
        detector = PromptDetector(BundledDetectionRegistry(), "qwen")
        plain_queue = "❯ Press up to edit queued messages\n"
        continuation = (
            "Continue working on your task. Your active Gobby step workflow is not complete.\n"
        )

        assert detector.detect_queued_message_prompt(plain_queue)
        assert not detector.detect_queued_continuation_prompt(plain_queue)
        assert detector.detect_queued_continuation_prompt(continuation)

    def test_no_match_on_normal_output(self) -> None:
        detector = make_detector()

        assert not detector.detect_queued_message_prompt("Running tests...\n")


class TestDismissedTracking:
    """Tests for the dismissed state tracking."""

    def test_not_dismissed_by_default(self) -> None:
        detector = make_detector()
        assert detector.was_dismissed("run-123") is False

    def test_mark_dismissed(self) -> None:
        detector = make_detector()
        detector.mark_dismissed("run-123")
        assert detector.was_dismissed("run-123") is True

    def test_clear_removes_tracking(self) -> None:
        detector = make_detector()
        detector.mark_dismissed("run-123")
        detector.clear("run-123")
        assert detector.was_dismissed("run-123") is False

    def test_clear_nonexistent_is_noop(self) -> None:
        """Clearing a run_id that was never tracked doesn't raise."""
        detector = make_detector()
        result = detector.clear("run-never-seen")
        assert result is None
        assert detector.was_dismissed("run-never-seen") is False

    def test_independent_tracking(self) -> None:
        """Dismissed state is per-agent, not global."""
        detector = make_detector()
        detector.mark_dismissed("run-a")
        assert detector.was_dismissed("run-a") is True
        assert detector.was_dismissed("run-b") is False

    def test_mark_dismissed_idempotent(self) -> None:
        """Marking the same run_id twice doesn't raise or change state."""
        detector = make_detector()
        detector.mark_dismissed("run-123")
        detector.mark_dismissed("run-123")
        assert detector.was_dismissed("run-123") is True

    def test_approval_prompt_fingerprint_tracking(self) -> None:
        detector = make_detector()
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
        detector = make_detector()
        prompt = "Approval required\nPress Enter to approve command A\n"
        detector.mark_approval_prompt_dismissed("run-1", prompt)

        detector.clear("run-1")

        assert detector.was_approval_prompt_dismissed("run-1", prompt) is False

    def test_loop_prompt_fingerprint_tracking(self) -> None:
        detector = make_detector()
        prompt = "Potential loop detected\nContinue? (y/n)\n"

        assert detector.was_loop_prompt_dismissed("run-1", prompt) is False
        detector.mark_loop_prompt_dismissed("run-1", prompt)
        assert detector.was_loop_prompt_dismissed("run-1", prompt) is True
        assert (
            detector.was_loop_prompt_dismissed(
                "run-1",
                "Potential loop detected\nType y to continue\n",
            )
            is False
        )
        assert detector.was_loop_prompt_dismissed("run-2", prompt) is False

    def test_clear_removes_loop_prompt_fingerprints(self) -> None:
        detector = make_detector()
        prompt = "Potential loop detected\nContinue? (y/n)\n"
        detector.mark_loop_prompt_dismissed("run-1", prompt)

        detector.clear("run-1")

        assert detector.was_loop_prompt_dismissed("run-1", prompt) is False
