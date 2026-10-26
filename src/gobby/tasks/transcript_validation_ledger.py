"""The stop gate's reduced view of one session's validation runs.

The gate's unresolved-failure check reads a green only through its command signature
(command, categories, segments) and its order, and reads a failure only while no green
covers it. The ledger therefore judges every failure against every green it is given,
then keeps the open failures and the newest ``GREEN_WINDOW`` greens, one per signature.
A green outside that window no longer covers a failure judged on a later stop.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import astuple
from typing import Self

from gobby.tasks.transcript_evidence_models import TranscriptValidationRun
from gobby.workflows.validation_cover import (
    cargo_package_selection_failed,
    green_covers_failure,
    ran_in_scratchpad,
)

#: Greens a reduced view keeps, newest first.
GREEN_WINDOW = 64

_Signature = tuple[object, ...]


class TranscriptValidationLedger:
    """Open failures and the newest greens of one session, bound to one project tree."""

    def __init__(self, project_path: str | None) -> None:
        self.project_path = project_path
        self._greens: tuple[TranscriptValidationRun, ...] = ()
        self._open: tuple[TranscriptValidationRun, ...] = ()

    @classmethod
    def of(cls, project_path: str | None, runs: Iterable[TranscriptValidationRun]) -> Self:
        """Judge each failure in ``runs`` against every green in them, then keep the window."""
        ledger = cls(project_path)
        fresh = [run for run in runs if run.categories]
        newest: dict[_Signature, TranscriptValidationRun] = {}
        for run in fresh:
            if run.outcome != "success":
                continue
            signature = _signature(run)
            held = newest.get(signature)
            if held is None or run.order >= held.order:
                newest[signature] = run
        greens = tuple(newest.values())
        ledger._open = tuple(
            failure
            for failure in fresh
            if failure.outcome == "failure"
            and _eligible(failure, project_path)
            and not any(_closes(green, failure, project_path) for green in greens)
        )
        ledger._greens = tuple(sorted(greens, key=lambda run: run.order)[-GREEN_WINDOW:])
        return ledger

    def reduced(self) -> tuple[TranscriptValidationRun, ...]:
        """Open failures and the green window, in creation order."""
        return tuple(sorted((*self._open, *self._greens), key=lambda run: run.order))


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
