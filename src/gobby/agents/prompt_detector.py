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

# An ``ansi`` pane snapshot carries SGR sequences (tmux ``-e``, the native
# host's ``recent_unwrapped_ansi``). A reader that positions on a character —
# the selection marker below — has to work on the visible text.
_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


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

    # Selection-list dialogs mark the highlighted row, and the highlighted row is
    # not always the affirmative one: Claude Code's workspace trust dialog opens on
    # "No, exit". Confirming the default there quits the agent instead of dismissing
    # the prompt, so the affirmative row has to be selected before Enter. A row that
    # trusts the PARENT directory is never that row — it would grant access to
    # sibling clone directories when several dev pipelines run in parallel.
    SELECTION_MARKERS = "\u276f\u203a\u25b6\u25cf\u2022"
    TRUST_OPTION_SCAN_LINES = 40
    MAX_SELECTION_OPTIONS = 8
    MAX_OPTION_LABEL_CHARS = 100
    AFFIRMATIVE_TRUST_PATTERN = re.compile(r"(?i)\b(?:yes|trust|proceed|accept)\b")
    DECLINE_TRUST_PATTERN = re.compile(r"(?i)\b(?:no|exit|quit|cancel|don'?t|never)\b")
    PARENT_TRUST_PATTERN = re.compile(r"(?i)\bparent\b")
    DOWN_KEY = "down"
    UP_KEY = "up"
    ENTER_KEY_NAME = "enter"

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

    def trust_dismiss_keys(self, pane_output: str) -> tuple[str, ...]:
        """Return the key sequence that answers a visible trust prompt affirmatively.

        Falls back to a bare Enter whenever the pane shows no navigable selection
        list, which is what every prompt that documents Enter as acceptance needs.
        """
        labels, selected = self._selection_options(pane_output)
        if selected is None:
            return (self.ENTER_KEY_NAME,)
        target = self._affirmative_option(labels)
        if target is None or target == selected:
            return (self.ENTER_KEY_NAME,)
        step = self.DOWN_KEY if target > selected else self.UP_KEY
        return (step,) * abs(target - selected) + (self.ENTER_KEY_NAME,)

    def _affirmative_option(self, labels: list[str]) -> int | None:
        """Return the index of the row that grants trust, or None when ambiguous."""
        for index, label in enumerate(labels):
            if self.DECLINE_TRUST_PATTERN.search(label):
                continue
            if self.PARENT_TRUST_PATTERN.search(label):
                continue
            if self.AFFIRMATIVE_TRUST_PATTERN.search(label):
                return index
        return None

    def _selection_options(self, excerpt: str) -> tuple[list[str], int | None]:
        """Return the marked selection block's labels and the selected row index.

        The block is the run of non-blank lines around the marked row, which is how
        these dialogs separate their options from the surrounding explanation. The
        scan reads a wider tail than ``_prompt_excerpt`` keeps, because a dialog
        whose rows fall outside the window would otherwise answer with the bare
        Enter that quits the agent, and it reads the visible text, because the
        marker is preceded by the colour sequence that highlights its row.
        """
        lines = [
            _ANSI_ESCAPE_RE.sub("", line)
            for line in excerpt.splitlines()[-self.TRUST_OPTION_SCAN_LINES :]
        ]
        marked = next(
            (index for index, line in enumerate(lines) if self._is_selected_option(line)),
            None,
        )
        if marked is None:
            return ([], None)
        start = marked
        while start > 0 and lines[start - 1].strip():
            start -= 1
        end = marked
        while end + 1 < len(lines) and lines[end + 1].strip():
            end += 1
        labels = [self._option_label(line) for line in lines[start : end + 1]]
        if len(labels) > self.MAX_SELECTION_OPTIONS:
            return ([], None)
        if any(not label or len(label) > self.MAX_OPTION_LABEL_CHARS for label in labels):
            return ([], None)
        return (labels, marked - start)

    def _is_selected_option(self, line: str) -> bool:
        stripped = line.strip(" \u2502\u256d\u256e\u2570\u256f\u2500")
        return bool(stripped) and stripped[0] in self.SELECTION_MARKERS

    def _option_label(self, line: str) -> str:
        label = line.strip(" \u2502\u256d\u256e\u2570\u256f\u2500")
        if label and label[0] in self.SELECTION_MARKERS:
            label = label[1:]
        return label.strip()

    def detect_loop_prompt(self, pane_output: str) -> bool:
        """Return True if pane output contains a loop detection prompt."""
        return self._matches("loop_prompt", pane_output)

    def detect_approval_prompt(self, pane_output: str) -> bool:
        """Return True when the pane displays a characterized approval action."""
        return self._matches("approval_prompt", pane_output) or self._matches(
            "artifact_approval", pane_output
        )

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
        if len(self._enumerated_options(self._prompt_excerpt(pane_output))) >= 2:
            return self.prompt_payload(pane_output, kind="question")
        return None

    def prompt_payload(self, pane_output: str, *, kind: PromptKind) -> DetectedPrompt:
        """Build a bounded structured payload for a known prompt kind."""
        excerpt = self._prompt_excerpt(pane_output)
        return DetectedPrompt(
            kind=kind,
            excerpt=excerpt,
            options=self._enumerated_options(excerpt),
            fingerprint=self.pane_fingerprint(pane_output),
        )

    def classification_payload(self, *, kind: PromptKind, label: str) -> DetectedPrompt:
        """Build a stable payload for a classification not tied to pane chrome."""
        excerpt = " ".join(label.split())[: self.PROMPT_EXCERPT_CHARS]
        fingerprint = hashlib.sha256(f"{kind}\0{excerpt}".encode()).hexdigest()
        return DetectedPrompt(
            kind=kind,
            excerpt=excerpt,
            options=(),
            fingerprint=fingerprint,
        )

    def _prompt_excerpt(self, pane_output: str) -> str:
        lines = pane_output.splitlines()[-self.PROMPT_EXCERPT_LINES :]
        excerpt = "\n".join(lines).strip()
        if len(excerpt) > self.PROMPT_EXCERPT_CHARS:
            excerpt = excerpt[-self.PROMPT_EXCERPT_CHARS :]
        return excerpt

    def _enumerated_options(self, pane_output: str) -> tuple[dict[str, object], ...]:
        options: dict[int, str] = {}
        for raw_line in pane_output.splitlines():
            line = raw_line.strip(" │╭╮╰╯─")
            for match in self.ENUMERATED_OPTION_PATTERN.finditer(line):
                option = int(match.group("option"))
                label = match.group("label").strip(" │")
                if label:
                    options.setdefault(option, label)
        return tuple(
            {"option": option, "label": label} for option, label in sorted(options.items())
        )

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
