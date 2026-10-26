"""The stop gate's reduced view of one session's validation runs.

The gate's unresolved-failure check reads a green only through its command signature
(command, categories, segments) and its order, and reads a failure only while no green
covers it. The ledger therefore keeps one green per signature, the latest-ordered, and
every failure no green covers. The gate decides the same over those runs as over the
full history, at a cost bounded by open failures and distinct signatures.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import astuple
from typing import Self

from gobby.tasks.transcript_evidence_models import TranscriptValidationRun
from gobby.workflows.validation_cover import (
    cargo_package_selection_failed,
    green_covers_failure,
    ran_in_scratchpad,
)

_Signature = tuple[object, ...]
_Change = tuple[TranscriptValidationRun | None, TranscriptValidationRun]


class TranscriptValidationLedger:
    """Greens by signature and open failures for one session, bound to one project tree."""

    def __init__(self, project_path: str | None) -> None:
        self.project_path = project_path
        self._greens: dict[_Signature, TranscriptValidationRun] = {}
        self._open: dict[int, TranscriptValidationRun] = {}

    @classmethod
    def of(cls, project_path: str | None, runs: Iterable[TranscriptValidationRun]) -> Self:
        ledger = cls(project_path)
        ledger._absorb(runs)
        return ledger

    def apply(self, changes: Sequence[_Change]) -> bool:
        """Fold ``(previous, current)`` replacements in.

        Returns False when a green left the set or moved earlier, since closure then
        stops being monotone; the caller rebuilds from the held runs.
        """
        added: list[TranscriptValidationRun] = []
        for previous, current in changes:
            if previous is not None:
                if _is_green(previous) and not _keeps_green(previous, current):
                    return False
                self._open.pop(previous.run_id, None)
            added.append(current)
        self._absorb(added)
        return True

    def reduced(self) -> tuple[TranscriptValidationRun, ...]:
        """Open failures and one green per signature, in creation order."""
        return tuple(
            sorted((*self._open.values(), *self._greens.values()), key=lambda run: run.order)
        )

    def _absorb(self, runs: Iterable[TranscriptValidationRun]) -> None:
        """Fold runs in with greens first, so each failure is judged against the final greens."""
        fresh = [run for run in runs if run.categories]
        greens = [run for run in fresh if run.outcome == "success"]
        for green in greens:
            signature = _signature(green)
            held = self._greens.get(signature)
            if held is None or green.order >= held.order:
                self._greens[signature] = green
        # Open failures were already judged against every earlier green, so only these close them.
        self._open = {
            run_id: failure
            for run_id, failure in self._open.items()
            if not any(_closes(green, failure, self.project_path) for green in greens)
        }
        for failure in (run for run in fresh if run.outcome == "failure"):
            if not _eligible(failure, self.project_path):
                continue
            if not any(
                _closes(green, failure, self.project_path) for green in self._greens.values()
            ):
                self._open[failure.run_id] = failure


def _is_green(run: TranscriptValidationRun) -> bool:
    return bool(run.categories) and run.outcome == "success"


def _keeps_green(previous: TranscriptValidationRun, current: TranscriptValidationRun) -> bool:
    return (
        _is_green(current)
        and _signature(current) == _signature(previous)
        and current.order >= previous.order
    )


def _signature(run: TranscriptValidationRun) -> _Signature:
    return (
        run.command,
        run.categories,
        tuple(astuple(segment) for segment in run.validation_segments),
    )


def _closes(
    green: TranscriptValidationRun, failure: TranscriptValidationRun, project_path: str | None
) -> bool:
    return bool(set(green.categories) & set(failure.categories)) and green_covers_failure(
        green, failure, project_path
    )


def _eligible(failure: TranscriptValidationRun, project_path: str | None) -> bool:
    return not cargo_package_selection_failed(failure) and not ran_in_scratchpad(
        failure, project_path
    )
