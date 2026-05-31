"""Detect and auto-dismiss blocking CLI prompts in tmux pane output.

When agents are spawned in clone/worktree directories, CLI tools like
Claude Code show a "Do you trust the files in this folder?" prompt that
blocks execution. This detector identifies those prompts so the lifecycle
monitor can dismiss them by sending the appropriate key sequence.
"""

from __future__ import annotations

import hashlib
import re


class PromptDetector:
    """Detects blocking CLI prompts (e.g. folder trust, loop detection) in tmux pane output.

    Separate from ``IdleDetector`` — that handles idle-at-prompt vs working.
    This handles interactive prompts that block agent startup or execution.
    """

    TRUST_PROMPT_PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(r"Do you trust the files", re.IGNORECASE),
        re.compile(r"Is this a project you created or one you trust", re.IGNORECASE),
        re.compile(r"Trust.*Folder", re.IGNORECASE),
    )

    # Key sequence to send: Enter to accept "Trust Folder" (option 1).
    # Do NOT use "2\n" (Trust parent Folder) — that would trust the
    # parent directory, granting access to sibling clone directories
    # when multiple dev pipelines run in parallel.
    TRUST_DISMISS_KEYS = "\n"

    LOOP_DETECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(r"stuck in a loop", re.IGNORECASE),
        re.compile(r"repeating myself", re.IGNORECASE),
        re.compile(r"potential loop detected", re.IGNORECASE),
        re.compile(r"seems? to be (?:stuck|looping|repeating)", re.IGNORECASE),
    )

    # Key sequence to dismiss loop detection: "yes, continue"
    LOOP_DISMISS_KEYS = "y\n"

    APPROVAL_CONTEXT_PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(
            r"\b(?:approval|permission|allow|approve|permit|confirmation)\b",
            re.IGNORECASE,
        ),
        re.compile(r"\b(?:run|execute|apply)\s+(?:this\s+)?(?:command|tool|action)", re.IGNORECASE),
    )
    APPROVAL_ENTER_PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(
            r"\b(?:press|hit)\s+(?:enter|return)\s+to\s+"
            r"(?:approve|allow|permit|proceed|continue|accept|confirm)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:approve|allow|permit|proceed|continue|accept|confirm)\b"
            r".{0,40}\b(?:enter|return)\b",
            re.IGNORECASE | re.DOTALL,
        ),
        re.compile(
            r"\b(?:enter|return)\b.{0,40}\b"
            r"(?:approve|allow|permit|proceed|continue|accept|confirm)\b",
            re.IGNORECASE | re.DOTALL,
        ),
        re.compile(
            r"\b(?:enter|return)\b\s+to\s+submit\b",
            re.IGNORECASE,
        ),
    )

    # Key sequence to approve prompts whose visible text says Enter approves/proceeds.
    APPROVAL_DISMISS_KEYS = "\n"
    ENTER_KEY = "Enter"
    EDIT_QUEUED_MESSAGE_KEY = "Up"
    QUEUED_CONTINUATION_PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(r"Continue working on your task", re.IGNORECASE),
        re.compile(r"active Gobby step workflow is not complete", re.IGNORECASE),
    )
    QUEUED_MESSAGE_PROMPT_PATTERNS: tuple[re.Pattern[str], ...] = (
        re.compile(r"queued messages", re.IGNORECASE),
        re.compile(r"Press up to edit queued messages", re.IGNORECASE),
    )

    def __init__(self) -> None:
        self._dismissed: set[str] = set()
        self._loop_counts: dict[str, int] = {}
        self._approval_fingerprints: dict[str, str] = {}

    def detect_trust_prompt(self, pane_output: str) -> bool:
        """Return True if pane output contains a folder trust prompt."""
        for pattern in self.TRUST_PROMPT_PATTERNS:
            if pattern.search(pane_output):
                return True
        return False

    def detect_loop_prompt(self, pane_output: str) -> bool:
        """Return True if pane output contains a loop detection prompt."""
        for pattern in self.LOOP_DETECTION_PATTERNS:
            if pattern.search(pane_output):
                return True
        return False

    def detect_approval_prompt(self, pane_output: str) -> bool:
        """Return True when Enter is explicitly shown as an approval action."""
        if not pane_output:
            return False

        has_context = any(pattern.search(pane_output) for pattern in self.APPROVAL_CONTEXT_PATTERNS)
        if not has_context:
            return False

        return any(pattern.search(pane_output) for pattern in self.APPROVAL_ENTER_PATTERNS)

    def detect_queued_continuation_prompt(self, pane_output: str) -> bool:
        """Return True when a Gobby continuation message is queued at a CLI prompt."""
        if not pane_output:
            return False

        has_continuation = any(
            pattern.search(pane_output) for pattern in self.QUEUED_CONTINUATION_PATTERNS
        )
        if not has_continuation:
            return False

        return any(pattern.search(pane_output) for pattern in self.QUEUED_MESSAGE_PROMPT_PATTERNS)

    def record_loop_dismiss(self, run_id: str) -> int:
        """Record loop prompt dismissal. Returns the new count."""
        self._loop_counts[run_id] = self._loop_counts.get(run_id, 0) + 1
        return self._loop_counts[run_id]

    def mark_dismissed(self, run_id: str) -> None:
        """Record that we already dismissed this agent's trust prompt."""
        self._dismissed.add(run_id)

    def was_dismissed(self, run_id: str) -> bool:
        """Check if this agent's trust prompt was already dismissed."""
        return run_id in self._dismissed

    def mark_approval_prompt_dismissed(self, run_id: str, pane_output: str) -> None:
        """Record the specific approval prompt already handled for this run."""
        self._approval_fingerprints[run_id] = self._approval_fingerprint(pane_output)

    def was_approval_prompt_dismissed(self, run_id: str, pane_output: str) -> bool:
        """Return True if this run already handled the same approval prompt."""
        return self._approval_fingerprints.get(run_id) == self._approval_fingerprint(pane_output)

    def clear(self, run_id: str) -> None:
        """Remove tracking state for an agent (on cleanup)."""
        self._dismissed.discard(run_id)
        self._loop_counts.pop(run_id, None)
        self._approval_fingerprints.pop(run_id, None)

    def _approval_fingerprint(self, pane_output: str) -> str:
        lines = [line.strip() for line in pane_output.splitlines() if line.strip()]
        normalized = " ".join(lines[-12:]).lower()
        normalized = re.sub(r"\s+", " ", normalized)
        return hashlib.sha256(normalized.encode()).hexdigest()
