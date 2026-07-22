"""Detect and auto-dismiss blocking CLI prompts in tmux pane output.

When agents are spawned in clone/worktree directories, CLI tools like
Claude Code show a "Do you trust the files in this folder?" prompt that
blocks execution. This detector identifies those prompts so the lifecycle
monitor can dismiss them by sending the appropriate key sequence.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal

from gobby.agents.detection.provider import DetectionRegistry, resolve_manifest

PromptKind = Literal["approval", "trust", "question", "stall"]


@dataclass(frozen=True, slots=True)
class DetectedPrompt:
    """Structured prompt data safe to publish to attention clients."""

    kind: PromptKind
    excerpt: str
    options: tuple[dict[str, object], ...]
    fingerprint: str

    def to_payload(self) -> dict[str, object]:
        """Return the JSON-compatible episode payload."""
        return {
            "kind": self.kind,
            "excerpt": self.excerpt,
            "options": [dict(option) for option in self.options],
            "fingerprint": self.fingerprint,
        }


class PromptDetector:
    """Detects blocking CLI prompts (e.g. folder trust, loop detection) in tmux pane output.

    Separate from ``IdleDetector`` — that handles idle-at-prompt vs working.
    This handles interactive prompts that block agent startup or execution.
    """

    # Key sequence to send: Enter to accept "Trust Folder" (option 1).
    # Do NOT use "2\n" (Trust parent Folder) — that would trust the
    # parent directory, granting access to sibling clone directories
    # when multiple dev pipelines run in parallel.
    TRUST_DISMISS_KEYS = "\n"

    # Key sequence to dismiss loop detection: "yes, continue"
    LOOP_DISMISS_KEYS = "y\n"

    # Key sequence to approve prompts whose visible text says Enter approves/proceeds.
    APPROVAL_DISMISS_KEYS = "\n"
    ENTER_KEY = "Enter"
    PROMPT_EXCERPT_LINES = 12
    PROMPT_EXCERPT_CHARS = 4096
    ENUMERATED_OPTION_PATTERN = re.compile(
        r"(?<!\d)(?P<option>[1-9]\d{0,2})[.)]\s+"
        r"(?P<label>.+?)"
        r"(?=(?:\s*/\s*|\s{2,})(?:[>›❯*•-]\s*)?[1-9]\d{0,2}[.)]\s+|$)"
    )

    def __init__(self, registry: DetectionRegistry, provider_id: str | None = None) -> None:
        self._registry = registry
        self._provider_id = provider_id.strip().lower() if provider_id is not None else None
        self._providers: dict[str, PromptDetector] = {}
        self._dismissed: set[str] = set()
        self._loop_counts: dict[str, int] = {}
        self._loop_prompt_fingerprints: dict[str, set[str]] = {}
        self._approval_fingerprints: dict[str, str] = {}

    def for_provider(self, provider_id: str) -> PromptDetector:
        """Return the cached detector bound to one provider."""

        normalized = provider_id.strip().lower()
        if self._provider_id == normalized:
            return self
        cached = self._providers.get(normalized)
        if cached is None:
            cached = PromptDetector(self._registry, normalized)
            cached._dismissed = self._dismissed
            cached._loop_counts = self._loop_counts
            cached._loop_prompt_fingerprints = self._loop_prompt_fingerprints
            cached._approval_fingerprints = self._approval_fingerprints
            self._providers[normalized] = cached
        return cached

    @property
    def provider_id(self) -> str | None:
        return self._provider_id

    def detect_trust_prompt(self, pane_output: str) -> bool:
        """Return True if pane output contains a folder trust prompt."""
        return self._matches("trust_prompt", pane_output)

    def detect_loop_prompt(self, pane_output: str) -> bool:
        """Return True if pane output contains a loop detection prompt."""
        return self._matches("loop_prompt", pane_output)

    def detect_approval_prompt(self, pane_output: str) -> bool:
        """Return True when Enter is explicitly shown as an approval action."""
        return self._matches("approval_prompt", pane_output)

    def detect_queued_message_prompt(self, pane_output: str) -> bool:
        """Return True when the CLI shows a queued-message editing prompt."""
        return self._matches("queued_message", pane_output)

    def detect_queued_continuation_prompt(self, pane_output: str) -> bool:
        """Return True when a Gobby continuation message is queued at a CLI prompt."""
        return self._matches("queued_continuation", pane_output)

    def _matches(self, rule_id: str, pane_output: str) -> bool:
        if not pane_output:
            return False
        if self._provider_id is None:
            raise RuntimeError("PromptDetector must be bound with for_provider() before detection")
        manifest = resolve_manifest(self._registry, self._provider_id)
        if manifest is None:
            return False
        return manifest.match_rule(rule_id, pane_output).match is not None

    def detect_prompt(self, pane_output: str) -> DetectedPrompt | None:
        """Detect an actionable prompt and return its structured payload."""
        if self.detect_approval_prompt(pane_output):
            return self.prompt_payload(pane_output, kind="approval")
        if self.detect_trust_prompt(pane_output):
            return self.prompt_payload(pane_output, kind="trust")
        if len(self._enumerated_options(pane_output)) >= 2:
            return self.prompt_payload(pane_output, kind="question")
        return None

    def prompt_payload(self, pane_output: str, *, kind: PromptKind) -> DetectedPrompt:
        """Build a bounded structured payload for a known prompt kind."""
        lines = pane_output.splitlines()[-self.PROMPT_EXCERPT_LINES :]
        excerpt = "\n".join(lines).strip()
        if len(excerpt) > self.PROMPT_EXCERPT_CHARS:
            excerpt = excerpt[-self.PROMPT_EXCERPT_CHARS :]
        return DetectedPrompt(
            kind=kind,
            excerpt=excerpt,
            options=self._enumerated_options(excerpt),
            fingerprint=self.pane_fingerprint(pane_output),
        )

    def _enumerated_options(self, pane_output: str) -> tuple[dict[str, object], ...]:
        options: dict[int, str] = {}
        for raw_line in pane_output.splitlines():
            line = raw_line.strip(" │╭╮╰╯─")
            for match in self.ENUMERATED_OPTION_PATTERN.finditer(line):
                option = int(match.group("option"))
                label = match.group("label").strip(" │")
                if label:
                    options.setdefault(option, label)
        return tuple({"option": option, "label": label} for option, label in options.items())

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

    def mark_loop_prompt_dismissed(self, run_id: str, pane_output: str) -> None:
        """Record a loop prompt fingerprint handled for this run."""
        fingerprints = self._loop_prompt_fingerprints.setdefault(run_id, set())
        fingerprints.add(self._loop_fingerprint(pane_output))

    def was_loop_prompt_dismissed(self, run_id: str, pane_output: str) -> bool:
        """Return True if this run already handled the same loop prompt."""
        return self._loop_fingerprint(pane_output) in self._loop_prompt_fingerprints.get(
            run_id, set()
        )

    def clear(self, run_id: str) -> None:
        """Remove tracking state for an agent (on cleanup)."""
        for detector in self._providers.values():
            detector.clear(run_id)
        self._dismissed.discard(run_id)
        self._loop_counts.pop(run_id, None)
        self._loop_prompt_fingerprints.pop(run_id, None)
        self._approval_fingerprints.pop(run_id, None)

    def pane_fingerprint(self, pane_output: str) -> str:
        """Return the stable fingerprint used for pane-backed prompt episodes."""
        return self._pane_fingerprint(pane_output)

    def _approval_fingerprint(self, pane_output: str) -> str:
        return self._pane_fingerprint(pane_output)

    def _loop_fingerprint(self, pane_output: str) -> str:
        return self._pane_fingerprint(pane_output)

    def _pane_fingerprint(self, pane_output: str) -> str:
        lines = [line.strip() for line in pane_output.splitlines() if line.strip()]
        normalized = " ".join(lines[-12:]).lower()
        normalized = re.sub(r"\s+", " ", normalized)
        return hashlib.sha256(normalized.encode()).hexdigest()
