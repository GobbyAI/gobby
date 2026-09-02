"""Current TaskValidator contracts retained at the canonical validation test target."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from gobby.config.tasks import TaskValidationConfig
from gobby.llm import LLMService
from gobby.storage.hub.protocol import HubDatabase
from gobby.tasks.validation import TaskValidator

pytestmark = pytest.mark.unit


def test_ordinary_prompt_preparation_preserves_changes_summary_without_provider(
    temp_db: HubDatabase,
) -> None:
    provider_call = AsyncMock()
    validator = TaskValidator(
        TaskValidationConfig(),
        cast(LLMService, SimpleNamespace(call_json_feature=provider_call)),
        temp_db,
    )

    prepared = validator.prepare_task_review(
        title="Ordinary close",
        changes_summary="ordinary changes summary",
        validation_criteria="Focused tests pass.",
        diff_text="small diff",
        checklist_facts={"validation_run_count": 1},
    )

    assert prepared.prompt_chars < prepared.prompt_limit
    assert "ordinary changes summary" in prepared.prompt
    provider_call.assert_not_awaited()
