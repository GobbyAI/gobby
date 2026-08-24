"""One bounded criteria review per evidence state, instead of per attempt.

The close checklist's criteria review is a single LLM round trip that dominates
close latency. Nothing about it depends on how many times ``close_task`` is
called: the verdict is a function of the rendered review prompt and the
evidence behind it, both of which the validator already fingerprints. Keying
the verdict on that pair turns a preview followed by an unchanged close into
one review, and turns repeated blocked attempts into none — while any
task-attributed edit or commit-set change moves the evidence fingerprint and
invalidates the memo on its own (#20866).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from gobby.storage.task_close_reviews import TaskCloseReviewStore
from gobby.tasks.close_verdict import CloseVerdict, CloseVerdictParseError, parse_close_verdict


class CloseVerdictMemo(Protocol):
    """One task's verdict memo, addressed by the review and evidence fingerprints."""

    def get(
        self,
        *,
        review_fingerprint: str,
        evidence_fingerprint: str,
    ) -> CloseVerdict | None:
        """Return the verdict already reviewed for this evidence state, if any."""
        ...

    def put(
        self,
        *,
        review_fingerprint: str,
        evidence_fingerprint: str,
        verdict: CloseVerdict,
    ) -> None:
        """Record a freshly reviewed verdict against this evidence state."""
        ...


class TaskCloseVerdictMemo:
    """Persist one task's criteria verdicts in ``task_close_reviews``.

    The identity a memo row needs — task, ref, calling session, close
    arguments — belongs to the close attempt, not to the validator, so the
    caller binds it here and the validator only supplies fingerprints and a
    verdict.
    """

    def __init__(
        self,
        store: TaskCloseReviewStore,
        *,
        task_id: str,
        task_ref: str,
        caller_session_id: str,
        close_arguments: Mapping[str, Any],
        criteria: Sequence[str],
    ) -> None:
        self._store = store
        self._task_id = task_id
        self._task_ref = task_ref
        self._caller_session_id = caller_session_id
        self._close_arguments = dict(close_arguments)
        self._criteria = tuple(criteria)

    def get(
        self,
        *,
        review_fingerprint: str,
        evidence_fingerprint: str,
    ) -> CloseVerdict | None:
        payload = self._store.get_memoized_verdict(
            task_id=self._task_id,
            review_fingerprint=review_fingerprint,
            evidence_fingerprint=evidence_fingerprint,
        )
        if payload is None:
            return None
        try:
            return parse_close_verdict(payload, list(self._criteria))
        except CloseVerdictParseError:
            # A memo that no longer parses against the task's current criteria
            # is not a verdict; treat it as a miss and review again rather than
            # blocking or closing on it.
            return None

    def put(
        self,
        *,
        review_fingerprint: str,
        evidence_fingerprint: str,
        verdict: CloseVerdict,
    ) -> None:
        self._store.memoize_verdict(
            task_id=self._task_id,
            task_ref=self._task_ref,
            caller_session_id=self._caller_session_id,
            close_arguments=self._close_arguments,
            review_fingerprint=review_fingerprint,
            evidence_fingerprint=evidence_fingerprint,
            verdict=verdict.to_dict(),
            valid=verdict.valid,
        )


__all__ = ["CloseVerdictMemo", "TaskCloseVerdictMemo"]
