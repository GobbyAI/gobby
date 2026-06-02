from __future__ import annotations

import pytest

from gobby.review_learning.service import ReviewLearningService

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_raw_test_failure_without_verified_fix_records_nothing(
    fake_memory_manager,
    fake_task_manager,
) -> None:
    service = ReviewLearningService(fake_memory_manager, fake_task_manager)

    result = await service.record(
        source_kind="test_failure",
        source="pytest",
        source_review="run-1",
        decision="confirmed",
        finding={
            "title": "pytest failure",
            "pattern_id": "pytest-fixture-state",
            "principle": "Isolate fixture state",
        },
        evidence={"failure": "test failed"},
    )

    assert result["skipped_reason"] == "missing_verified_fix"
    assert fake_memory_manager.memories == []


@pytest.mark.asyncio
async def test_test_failure_with_verified_fix_records_lesson(
    fake_memory_manager, fake_task_manager
) -> None:
    service = ReviewLearningService(fake_memory_manager, fake_task_manager)

    result = await service.record(
        source_kind="test_failure",
        source="pytest",
        source_review="run-1",
        decision="confirmed",
        finding={
            "title": "pytest failure",
            "pattern_id": "pytest-fixture-state",
            "principle": "Isolate fixture state",
        },
        evidence={"commit_sha": "abc123"},
    )

    assert result["lesson_id"] == "mem-1"
    assert len(fake_memory_manager.memories) == 1
