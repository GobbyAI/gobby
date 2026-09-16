"""Evidence types derived from provider transcripts.

These are the vocabulary the close path speaks: what a session ran, what it edited,
and the failure to read a transcript at all. They sit below transcript parsing and
below the derivation cache so both can depend on them without depending on each
other.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Any

from gobby.tasks.transcript_outcomes import (
    EvidenceOutcome,
    classify_validation_command_equivalence,
)


@dataclass(frozen=True)
class TranscriptValidationSegment:
    """One classified validation segment of a shell command."""

    #: Normalized argv text with wrappers and env assignments stripped.
    command: str
    categories: tuple[str, ...]
    segment_index: int = field(default=0, compare=False)
    #: Matcher metadata; a ``bounded_inputs`` segment reads only ``languages`` code.
    languages: tuple[str, ...] = field(default=(), compare=False)
    bounded_inputs: bool = field(default=False, compare=False)


@dataclass(frozen=True)
class TranscriptValidationRun:
    """One shell outcome, with empty categories for review-only commands."""

    session_id: str
    source: str
    command: str
    categories: tuple[str, ...]
    matcher_id: str
    label: str
    outcome: EvidenceOutcome
    started_at: datetime
    completed_at: datetime
    order: int
    exit_code: int | None = None
    unknown_reason: str | None = None
    output: str | None = None
    output_truncated: bool = False
    #: Every validation segment of ``command`` in order, each with its own
    #: categories; ``categories`` above is their union. Empty only for runs
    #: built without classification.
    validation_segments: tuple[TranscriptValidationSegment, ...] = ()
    core_command: str | None = field(init=False)
    wrapped: bool = field(init=False)
    wrapper_reason: str | None = field(init=False)

    def __post_init__(self) -> None:
        equivalence = classify_validation_command_equivalence(self.command)
        object.__setattr__(self, "core_command", equivalence.core_command)
        object.__setattr__(self, "wrapped", equivalence.wrapped)
        object.__setattr__(self, "wrapper_reason", equivalence.wrapper_reason)


@dataclass(frozen=True)
class TranscriptEdit:
    """One task-attributed edit observed in a transcript."""

    session_id: str
    source: str
    path: str
    timestamp: datetime
    order: int
    tool_name: str


@dataclass(frozen=True)
class TranscriptEvidence:
    """Validation runs and task edits derived from one or more sessions."""

    validation_runs: tuple[TranscriptValidationRun, ...] = ()
    command_runs: tuple[TranscriptValidationRun, ...] = ()
    edits: tuple[TranscriptEdit, ...] = ()
    attempted_paths: tuple[str, ...] = ()
    sessions: tuple[str, ...] = ()
    degraded_capabilities: tuple[str, ...] = ()
    # Observations outside the task link window never enter credit-bearing collections.
    excluded_runs: tuple[TranscriptValidationRun, ...] = ()
    #: gcode's indexed language per task-edited path, attached by close evaluation.
    edit_languages: Mapping[str, str] = field(default_factory=dict, compare=False)
    #: Newest provider record timestamp these transcripts actually contained. Close
    #: compares it against live provider activity to tell a transcript still being
    #: flushed apart from one that genuinely holds no evidence (#22367). It describes
    #: the read rather than the evidence, and a transcript that only grew outside the
    #: credit window must not make two otherwise identical evidence sets differ.
    latest_record_at: datetime | None = field(default=None, compare=False)

    def summary(self) -> dict[str, Any]:
        """Return bounded deterministic facts for checklist diagnostics."""
        outcome_counts = {"success": 0, "failure": 0, "unknown": 0}
        category_successes: dict[str, int] = {}
        for run in self.validation_runs:
            outcome_counts[run.outcome] += 1
            if run.outcome == "success":
                for category in run.categories:
                    category_successes[category] = category_successes.get(category, 0) + 1
        return {
            "sessions": list(self.sessions),
            "validation_run_count": len(self.validation_runs),
            "command_run_count": len(self.command_runs),
            "outcomes": outcome_counts,
            "successful_categories": category_successes,
            "task_edit_count": len(self.edits),
            "latest_task_edit_at": (
                max(edit.timestamp for edit in self.edits).isoformat() if self.edits else None
            ),
            "degraded_capabilities": list(self.degraded_capabilities),
        }


class TranscriptEvidenceUnavailable(RuntimeError):
    """Raised when no readable transcript exists for a required session."""

    def __init__(
        self,
        message: str,
        *,
        source: str,
        attempted_paths: Iterable[str],
    ) -> None:
        super().__init__(message)
        self.source = source
        self.attempted_paths = tuple(dict.fromkeys(attempted_paths))
        self.retry_after = 5

    def __reduce__(self) -> tuple[Any, ...]:
        """Preserve required keyword arguments across the evidence process pool."""
        constructor = partial(type(self), source=self.source, attempted_paths=self.attempted_paths)
        return constructor, self.args, self.__dict__
