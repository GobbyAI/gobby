"""Idle agent classification via tmux pane content analysis.

Secondary idle detection layer: classifies *why* an agent is idle by
examining the last few lines of its tmux pane (prompt, context full, etc.).
The primary idle signal is session updated_at in lifecycle_monitor.py;
pane analysis only runs when the session appears stale.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Literal

from gobby.agents.detection.matcher import CompiledManifest, composer_region
from gobby.agents.detection.provider import DetectionRegistry, resolve_manifest

# Pane lines a composer probe captures: enough to hold the frame around a
# multi-line draft plus the status lines a provider draws below it.
COMPOSER_PROBE_LINES = 40

ComposerState = Literal["empty", "draft", "unknown"]

# Box edge, then the provider's prompt marker, then the draft text; the trailing
# box edge Droid draws after the text is stripped separately.
_COMPOSER_ROW_RE = re.compile(r"^\s*│?\s*[❯›>$]\s*(?P<text>.*?)\s*│?\s*$")

# A CSI sequence (SGR when it ends in ``m``), an OSC string, or a two-byte escape.
_ESCAPE_RE = re.compile(
    r"\x1b(?:\[(?P<params>[0-?]*)[ -/]*(?P<final>[@-~])|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])"
)
_SGR_PARAMS_RE = re.compile(r"[0-9;:]*")


def plain_text(snapshot: str) -> str:
    """Return ``snapshot`` with every escape sequence removed and all text kept."""
    return _ESCAPE_RE.sub("", snapshot)


def composer_text(snapshot: str) -> str:
    """Return ``snapshot`` as plain text with faint-rendered text blanked.

    Claude Code's prompt suggestion and the Codex and Droid placeholders sit in
    an empty composer drawn faint (SGR 2), while typed text is never faint, so an
    ``ansi`` snapshot tells them apart where plain text cannot. Droid draws its
    cursor as a reverse-video cell over the placeholder's first character, so a
    reverse cell followed by faint text is blanked with it. A snapshot without
    escape sequences is returned unchanged.
    """
    if "\x1b" not in snapshot:
        return snapshot
    faint = reverse = False
    rendered: list[str] = []
    for line in snapshot.split("\n"):
        cells: list[tuple[str, bool, bool]] = []
        position = 0
        for escape in _ESCAPE_RE.finditer(line):
            cells.extend((char, faint, reverse) for char in line[position : escape.start()])
            position = escape.end()
            params = escape.group("params")
            if escape.group("final") == "m" and _SGR_PARAMS_RE.fullmatch(params):
                faint, reverse = _apply_sgr(params, faint, reverse)
        cells.extend((char, faint, reverse) for char in line[position:])
        rendered.append(
            "".join(
                " " if dim or (inverse and index + 1 < len(cells) and cells[index + 1][1]) else char
                for index, (char, dim, inverse) in enumerate(cells)
            )
        )
    return "\n".join(rendered)


def _apply_sgr(params: str, faint: bool, reverse: bool) -> tuple[bool, bool]:
    codes = params.split(";")
    index = 0
    while index < len(codes):
        head = codes[index].split(":", 1)[0]
        code = int(head) if head else 0
        if code in (38, 48, 58) and ":" not in codes[index]:
            # 5;n and 2;r;g;b colour arguments are separate parameters, not attributes.
            form = codes[index + 1] if index + 1 < len(codes) else ""
            index += {"5": 3, "2": 5}.get(form, 1)
            continue
        if code == 0:
            faint = reverse = False
        elif code in (2, 22):
            faint = code == 2
        elif code in (7, 27):
            reverse = code == 7
        index += 1
    return faint, reverse


@dataclass(frozen=True)
class ComposerRead:
    """What a pane snapshot says about the provider's composer.

    ``empty`` and ``draft`` are positive reads of a visible composer frame;
    ``unknown`` covers no snapshot, no frame, or a frame the manifest cannot
    classify, and callers fall back to the blind drain.
    """

    state: ComposerState
    line: str = ""


@dataclass
class IdleState:
    """Tracks idle state for a single agent."""

    first_idle_at: float | None = None
    reprompt_count: int = 0
    last_reprompt_at: float | None = None


class IdleDetector:
    """Detects idle agents by pattern-matching tmux pane output.

    Three detection modes:
    1. **Idle prompt** — agent is sitting at ❯ or $ prompt (repromptable)
    2. **Context full** — agent hit context limits (immediate fail, reprompt won't help)
    3. **Active** — agent is still working (no action needed)
    """

    REPROMPT_MESSAGE = (
        "Continue working on your task. When your work is complete, call "
        "gobby-agents:end_agent_run with current_state and next_steps to end this agent run."
    )

    def __init__(self, registry: DetectionRegistry, provider_id: str | None = None) -> None:
        self._registry = registry
        self._provider_id = provider_id.strip().lower() if provider_id is not None else None
        self._providers: dict[str, IdleDetector] = {}
        self._states: dict[str, IdleState] = {}

    def for_provider(self, provider_id: str) -> IdleDetector:
        """Return the cached detector bound to one provider."""

        normalized = provider_id.strip().lower()
        if self._provider_id == normalized:
            return self
        cached = self._providers.get(normalized)
        if cached is None:
            cached = IdleDetector(self._registry, normalized)
            cached._states = self._states
            self._providers[normalized] = cached
        return cached

    def _manifest(self) -> CompiledManifest | None:
        if self._provider_id is None:
            raise RuntimeError("IdleDetector must be bound with for_provider() before detection")
        return resolve_manifest(self._registry, self._provider_id)

    def get_state(self, run_id: str) -> IdleState:
        """Get or create idle state for an agent."""
        if run_id not in self._states:
            self._states[run_id] = IdleState()
        return self._states[run_id]

    def clear_state(self, run_id: str) -> None:
        """Remove tracking state for an agent (on cleanup)."""
        for detector in self._providers.values():
            detector.clear_state(run_id)
        self._states.pop(run_id, None)

    def unsubmitted_input_fingerprint(self, pane_output: str) -> str | None:
        """Fingerprint normalized draft lines typed at a provider prompt."""
        manifest = self._manifest()
        if manifest is None:
            return None
        if manifest.match_rule("queued_continuation", pane_output).match is not None:
            return None
        if manifest.match_rule("queued_message", pane_output).match is not None:
            return None

        draft_lines: list[str] = []
        for line in pane_output.splitlines():
            stripped = line.strip()
            if manifest.match_rule("stalled_input", stripped).match is not None:
                draft_lines.append(" ".join(stripped.split()))
        if not draft_lines:
            return None
        return hashlib.sha256("\n".join(draft_lines).encode()).hexdigest()

    def has_unsubmitted_input(self, pane_output: str) -> bool:
        """Return whether pane output shows text typed at a prompt but not submitted."""
        return self.unsubmitted_input_fingerprint(pane_output) is not None

    def composer_read(self, pane_output: str | None) -> ComposerRead:
        """Classify the composer frame at the bottom of ``pane_output``.

        A ``draft`` carries the text after the prompt marker on the marker row.
        Anything short of a positive read is ``unknown``. Probes pass an ``ansi``
        snapshot so faint suggestion and placeholder text reads as empty.
        """
        if pane_output is None:
            return ComposerRead("unknown")
        pane_output = composer_text(pane_output)
        manifest = self._manifest()
        if manifest is None:
            return ComposerRead("unknown")
        if manifest.match_rule("composer_draft", pane_output).match is not None:
            for line in composer_region(pane_output).splitlines():
                row = _COMPOSER_ROW_RE.match(line)
                if row is not None and row.group("text"):
                    return ComposerRead("draft", row.group("text"))
            return ComposerRead("draft")
        if manifest.match_rule("composer_empty", pane_output).match is not None:
            return ComposerRead("empty")
        return ComposerRead("unknown")

    def turn_in_flight_fingerprint(self, pane_output: str) -> str | None:
        """Fingerprint the provider's live turn indicator, when one is rendered.

        Claude Code and Codex keep an elapsed-time spinner above the prompt for the
        whole turn (``✻ Grooving… (7m 12s · still thinking with xhigh effort)``,
        ``• Working (4m 58s • esc to interrupt)``), including long thinking phases
        that write nothing to the transcript. The fingerprint follows the counter,
        so a live turn keeps changing it while a frozen CLI does not.
        """
        manifest = self._manifest()
        if manifest is None:
            return None
        lines = [
            " ".join(line.split())
            for line in pane_output.splitlines()
            if manifest.match_rule("turn_in_flight", line.strip()).match is not None
        ]
        if not lines:
            return None
        return hashlib.sha256("\n".join(lines).encode()).hexdigest()

    def has_turn_in_flight(self, pane_output: str) -> bool:
        """Return whether the pane shows a provider turn still running."""
        return self.turn_in_flight_fingerprint(pane_output) is not None

    def detect(self, pane_output: str) -> str:
        """Classify pane output as 'idle', 'context_full', or 'active'.

        Args:
            pane_output: Last few lines captured from the tmux pane.

        Returns:
            One of: 'idle', 'context_full', 'active'
        """
        manifest = self._manifest()
        if manifest is None:
            return "unknown"

        lines = pane_output.strip().splitlines()
        if not lines:
            return "active"

        if manifest.match_rule("context_full", pane_output).match is not None:
            return "context_full"
        if manifest.match_rule("stop_hook_blocked", pane_output).match is not None:
            return "idle"
        has_queued = manifest.match_rule("queued_message", pane_output).match is not None
        has_active = manifest.match_rule("active_work", pane_output).match is not None
        if has_queued and has_active:
            return "active"
        # A visible composer frame is the idle signal regardless of what the
        # provider draws below it; status bars are operator-configurable, so the
        # bottom-line walk cannot rely on recognising them.
        if self.composer_read(pane_output).state != "unknown":
            return "idle"
        for line in reversed(lines):
            stripped = line.strip()
            if not stripped:
                continue
            if manifest.match_rule("status_bar", stripped).match is not None:
                continue
            if manifest.match_rule("idle_prompt", stripped).match is not None:
                return "idle"
            if manifest.match_rule("stalled_input", stripped).match is not None:
                return "idle"
            break
        return "active"

    def should_reprompt(
        self,
        run_id: str,
        idle_timeout_seconds: int,
        max_reprompt_attempts: int,
    ) -> bool:
        """Check if an idle agent should be reprompted.

        Returns True if the agent has been idle long enough and hasn't
        exceeded max reprompt attempts.
        """
        state = self.get_state(run_id)
        now = time.monotonic()

        if state.first_idle_at is None:
            state.first_idle_at = now
            return False

        elapsed = now - state.first_idle_at
        if elapsed < idle_timeout_seconds:
            return False

        if state.reprompt_count >= max_reprompt_attempts:
            return False

        return True

    def should_fail(self, run_id: str, max_reprompt_attempts: int) -> bool:
        """Check if an idle agent should be failed (exhausted reprompts)."""
        state = self.get_state(run_id)
        return state.reprompt_count >= max_reprompt_attempts

    def record_reprompt(self, run_id: str) -> None:
        """Record that a reprompt was sent."""
        state = self.get_state(run_id)
        state.reprompt_count += 1
        state.last_reprompt_at = time.monotonic()
        # Reset idle timer so we wait again before next reprompt
        state.first_idle_at = time.monotonic()

    def reset_idle(self, run_id: str) -> None:
        """Reset idle tracking when agent becomes active again."""
        state = self.get_state(run_id)
        state.first_idle_at = None
        state.reprompt_count = 0
        state.last_reprompt_at = None
