"""Validation outcomes shared by transcript parsing, caching and found-work checks."""

from __future__ import annotations

from collections.abc import Iterable
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
    """One shell outcome, with empty categories for non-validation commands."""

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
    output_recovered_from: str | None = None
    output_recovered_at: datetime | None = None
    provider_call_id: str | None = None
    background_job_id: str | None = None
    background_output_path: str | None = None
    background_monitor_id: str | None = None
    background_description: str | None = None
    #: The shell tool call's absolute working directory, when the provider recorded one.
    workdir: str | None = None
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
class TranscriptEvidence:
    """Validation outcomes derived from one or more transcript sources."""

    validation_runs: tuple[TranscriptValidationRun, ...] = ()
    command_runs: tuple[TranscriptValidationRun, ...] = ()
    attempted_paths: tuple[str, ...] = ()
    sessions: tuple[str, ...] = ()
    degraded_capabilities: tuple[str, ...] = ()
    latest_record_at: datetime | None = field(default=None, compare=False)


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
