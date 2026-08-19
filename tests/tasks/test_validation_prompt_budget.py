"""Prompt-budget contracts for task-close criteria review."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.tasks import TaskValidationConfig
from gobby.llm import LLMService
from gobby.storage.hub.protocol import HubDatabase
from gobby.tasks.validation import TaskValidator, ValidationPromptTooLarge


def _validator(config: TaskValidationConfig) -> tuple[TaskValidator, MagicMock]:
    llm_service = MagicMock(spec=LLMService)
    llm_service.call_json_feature = AsyncMock(
        return_value={"status": "valid", "criteria": [], "feedback": "Complete."}
    )
    validator = TaskValidator(
        config,
        llm_service,
        db=MagicMock(spec=HubDatabase),
    )
    return validator, llm_service


def _render_context(
    _path: str,
    context: dict[str, Any] | None = None,
    strict: bool = False,
) -> str:
    del strict
    assert context is not None
    return "\n\n".join(f"{key}:\n{value}" for key, value in context.items())


@pytest.mark.asyncio
async def test_prompt_between_legacy_and_default_limits_reaches_llm_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator, llm_service = _validator(TaskValidationConfig())
    monkeypatch.setattr(validator._loader, "render", _render_context)
    paths = [f"src/module_{index:02d}.py" for index in range(35)]
    diff_text = "".join(
        (
            f"diff --git a/{path} b/{path}\n"
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            "@@ -0,0 +1 @@\n"
            f"+VALUE_{index:02d} = True\n"
        )
        for index, path in enumerate(paths)
    )
    criteria = tuple(
        (
            f"Criterion {index:02d} token-{index:02d} proves the configured task-close review "
            "behavior while retaining complete acceptance evidence across every rendered "
            "criterion, linked file, validation boundary, and deterministic result marker."
        )
        for index in range(45)
    )
    validation_criteria = "\n".join(f"- {criterion}" for criterion in criteria)
    tdd_summary = (
        "TDD summary: RED=focused failure; GREEN=minimal pass; "
        "REFACTOR=cleanup complete; FINAL_GREEN=exact focused command passed."
    )

    await validator.validate_task(
        task_id="task-1",
        title="Raise prompt budget",
        changes_summary=tdd_summary,
        validation_criteria=validation_criteria,
        diff_text=diff_text,
        checklist_facts={"validation_commands": "focused tests passed"},
    )

    prompt = llm_service.call_json_feature.await_args.args[1]
    assert 10_000 < len(prompt) < 32_000
    assert all(criterion in prompt for criterion in criteria)
    assert all(f"- {path} (" in prompt for path in paths)
    assert tdd_summary in prompt


@pytest.mark.unit
@pytest.mark.asyncio
async def test_long_changes_summary_reaches_llm_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator, llm_service = _validator(TaskValidationConfig())
    monkeypatch.setattr(validator._loader, "render", _render_context)
    summary = "changed " + ("token " * 800).strip()

    await validator.validate_task(
        task_id="task-1",
        title="Keep summary",
        changes_summary=summary,
        validation_criteria="- Prove the close review sees the full summary.",
        diff_text="diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+ok\n",
        checklist_facts={"validation_commands": "focused tests passed"},
    )

    prompt = llm_service.call_json_feature.await_args.args[1]
    assert summary in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_limit", [None, 8_000])
async def test_prompt_at_exact_limit_reaches_llm(
    monkeypatch: pytest.MonkeyPatch,
    configured_limit: int | None,
) -> None:
    config = (
        TaskValidationConfig()
        if configured_limit is None
        else TaskValidationConfig(close_review_prompt_max_chars=configured_limit)
    )
    expected_limit = config.close_review_prompt_max_chars

    def render_at_limit(
        _path: str,
        context: dict[str, Any] | None = None,
        strict: bool = False,
    ) -> str:
        del context, strict
        return "x" * expected_limit

    validator, llm_service = _validator(config)
    monkeypatch.setattr(validator._loader, "render", render_at_limit)

    await validator.validate_task(
        task_id="task-1",
        title="Use the exact prompt budget",
        changes_summary="Implemented.",
        validation_criteria="The exact configured limit is accepted.",
        diff_text=None,
        checklist_facts={},
    )

    llm_service.call_json_feature.assert_awaited_once()
    assert llm_service.call_json_feature.await_args.args[1] == "x" * expected_limit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured_limit", "prompt_chars"),
    [(None, 32_001), (8_000, 8_001)],
)
async def test_oversized_prompt_fails_before_llm_with_actionable_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    configured_limit: int | None,
    prompt_chars: int,
) -> None:
    config = (
        TaskValidationConfig()
        if configured_limit is None
        else TaskValidationConfig(close_review_prompt_max_chars=configured_limit)
    )
    expected_limit = config.close_review_prompt_max_chars

    def render_oversized(
        _path: str,
        context: dict[str, Any] | None = None,
        strict: bool = False,
    ) -> str:
        del context, strict
        return "x" * prompt_chars

    validator, llm_service = _validator(config)
    monkeypatch.setattr(validator._loader, "render", render_oversized)

    with pytest.raises(ValidationPromptTooLarge) as exc_info:
        await validator.validate_task(
            task_id="task-1",
            title="Raise prompt budget",
            changes_summary="Implemented.",
            validation_criteria="The configured limit is enforced.",
            diff_text=None,
            checklist_facts={},
        )

    message = str(exc_info.value)
    assert str(prompt_chars) in message
    assert str(expected_limit) in message
    assert "gobby-tasks.validation.close_review_prompt_max_chars" in message
    assert "preserve every validation criterion" in message
    llm_service.call_json_feature.assert_not_awaited()
