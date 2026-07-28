from __future__ import annotations

import logging
from collections.abc import Callable
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.config.tasks import TaskValidationConfig
from gobby.llm.service import LLMService
from gobby.tasks.validation import TaskValidator


async def test_close_review_diagnostics_log_at_debug(
    caplog: pytest.LogCaptureFixture,
    make_task_validator: Callable[..., TaskValidator],
) -> None:
    llm_service = cast(LLMService, MagicMock(spec=LLMService))
    call_json_feature = AsyncMock(
        return_value={"status": "valid", "feedback": "All criteria are satisfied."}
    )
    validator = make_task_validator(
        TaskValidationConfig(enabled=True),
        llm_service,
    )

    with (
        patch.object(validator._loader, "render", return_value="bounded review prompt"),
        patch.object(llm_service, "call_json_feature", call_json_feature),
        caplog.at_level(logging.DEBUG, logger="gobby.tasks.validation"),
    ):
        verdict = await validator.validate_task(
            task_id="task-123",
            title="Reduce INFO noise",
            changes_summary="Demoted routine success diagnostics.",
            validation_criteria="Routine close review diagnostics emit at DEBUG.",
            diff_text=None,
            checklist_facts={"validation": "passed"},
        )

    assert verdict.valid is True
    review_record = next(
        record
        for record in caplog.records
        if record.getMessage().startswith("Running bounded close criteria review")
    )
    assert review_record.levelno == logging.DEBUG
    assert "prompt_chars=" in review_record.getMessage()
    assert not any(record.levelno == logging.INFO for record in caplog.records)
