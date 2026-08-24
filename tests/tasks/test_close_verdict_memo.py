"""Fingerprint-keyed memoization of the bounded task-close criteria verdict."""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.tasks import TaskValidationConfig
from gobby.llm import LLMService
from gobby.storage.hub.protocol import HubDatabase
from gobby.tasks.close_verdict import CloseVerdict
from gobby.tasks.close_verdict_memo import CloseVerdictMemo
from gobby.tasks.validation import TaskValidator

pytestmark = pytest.mark.unit


class _RecordingMemo:
    """In-memory stand-in for the persisted memo, keyed by the fingerprint pair."""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], CloseVerdict] = {}
        self.lookups: list[tuple[str, str]] = []
        self.threads: set[int] = set()

    def get(
        self,
        *,
        review_fingerprint: str,
        evidence_fingerprint: str,
    ) -> CloseVerdict | None:
        self.threads.add(threading.get_ident())
        self.lookups.append((review_fingerprint, evidence_fingerprint))
        return self.entries.get((review_fingerprint, evidence_fingerprint))

    def put(
        self,
        *,
        review_fingerprint: str,
        evidence_fingerprint: str,
        verdict: CloseVerdict,
    ) -> None:
        self.threads.add(threading.get_ident())
        self.entries[(review_fingerprint, evidence_fingerprint)] = verdict


def _validator() -> tuple[TaskValidator, AsyncMock]:
    llm_service = MagicMock(spec=LLMService)
    llm_service.call_json_feature = AsyncMock(
        return_value={
            "status": "valid",
            "criteria": [{"index": 1, "satisfied": True, "gap": None}],
            "feedback": "Complete.",
        }
    )
    validator = TaskValidator(
        TaskValidationConfig(),
        llm_service,
        db=MagicMock(spec=HubDatabase),
    )
    return validator, llm_service.call_json_feature


async def _validate(
    validator: TaskValidator,
    memo: CloseVerdictMemo | None,
    **overrides: Any,
) -> CloseVerdict:
    kwargs: dict[str, Any] = {
        "task_id": "task-1",
        "title": "Memoize the close verdict",
        "changes_summary": "Implemented the memo.",
        "validation_criteria": "1. The verdict is memoized.",
        "diff_text": "diff --git a/x b/x",
        "checklist_facts": {"commit_count": 1, "commit_shas": ["abc"]},
        "verdict_memo": memo,
    }
    kwargs.update(overrides)
    return await validator.validate_task(**kwargs)


@pytest.mark.asyncio
async def test_unchanged_evidence_serves_the_memo_without_a_second_llm_call() -> None:
    validator, call_json_feature = _validator()
    memo = _RecordingMemo()

    first = await _validate(validator, memo)
    second = await _validate(validator, memo)

    assert call_json_feature.await_count == 1
    assert second == first
    assert second.valid is True
    assert len(memo.entries) == 1
    assert memo.lookups[0] == memo.lookups[1]


@pytest.mark.asyncio
async def test_changed_evidence_misses_the_memo_and_runs_a_fresh_review() -> None:
    validator, call_json_feature = _validator()
    memo = _RecordingMemo()

    await _validate(validator, memo)
    await _validate(
        validator,
        memo,
        checklist_facts={"commit_count": 2, "commit_shas": ["abc", "def"]},
    )

    assert call_json_feature.await_count == 2
    assert len(memo.entries) == 2
    assert memo.lookups[0] != memo.lookups[1]


@pytest.mark.asyncio
async def test_memo_reads_and_writes_stay_off_the_event_loop() -> None:
    validator, _call_json_feature = _validator()
    memo = _RecordingMemo()

    await _validate(validator, memo)
    await _validate(validator, memo)

    assert memo.threads, "the memo must be reached"
    assert threading.get_ident() not in memo.threads


@pytest.mark.asyncio
async def test_review_without_a_memo_still_runs_exactly_one_review() -> None:
    validator, call_json_feature = _validator()

    await _validate(validator, None)
    await _validate(validator, None)

    assert call_json_feature.await_count == 2


@pytest.mark.asyncio
async def test_criteria_review_bounds_the_provider_fallback_chain() -> None:
    validator, call_json_feature = _validator()
    config = TaskValidationConfig()

    await _validate(validator, None)

    await_args = call_json_feature.await_args
    assert await_args is not None
    kwargs = await_args.kwargs
    assert kwargs["total_timeout_seconds"] == config.close_review_total_timeout_seconds
    assert config.close_review_total_timeout_seconds > 0
