"""Closure-reason threading contracts for task-close criteria review."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.tasks import TaskValidationConfig
from gobby.llm import LLMService
from gobby.storage.hub.protocol import HubDatabase
from gobby.tasks.generation_schemas import TASK_CLOSE_VALIDATION_SCHEMA
from gobby.tasks.validation import NO_WORK_CLOSE_REASONS, TaskValidator

pytestmark = pytest.mark.unit

_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2] / "src/gobby/install/shared/prompts/validation/validate.md"
)


def _validator() -> tuple[TaskValidator, MagicMock]:
    llm_service = MagicMock(spec=LLMService)
    llm_service.call_json_feature = AsyncMock(
        return_value={"status": "valid", "criteria": [], "feedback": "Complete."}
    )
    validator = TaskValidator(
        TaskValidationConfig(),
        llm_service,
        db=MagicMock(spec=HubDatabase),
    )
    return validator, llm_service


def _capture_context(captured: dict[str, Any]) -> Callable[..., str]:
    def _render(
        _path: str,
        context: dict[str, Any] | None = None,
        strict: bool = False,
    ) -> str:
        del strict
        assert context is not None
        captured.update(context)
        return "rendered prompt"

    return _render


async def _run(validator: TaskValidator, **overrides: Any) -> None:
    kwargs: dict[str, Any] = {
        "task_id": "task-1",
        "title": "Close-reason threading",
        "changes_summary": "Superseded by the vault cutover.",
        "validation_criteria": "The wiki gap is corrected.",
        "diff_text": None,
        "checklist_facts": {"commit_count": 0},
    }
    kwargs.update(overrides)
    await validator.validate_task(**kwargs)


@pytest.mark.asyncio
async def test_closure_reason_reaches_prompt_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator, llm_service = _validator()
    captured: dict[str, Any] = {}
    monkeypatch.setattr(validator._loader, "render", _capture_context(captured))

    await _run(validator, closure_reason="obsolete")

    assert captured["closure_reason"] == "obsolete"
    assert (
        llm_service.call_json_feature.await_args.kwargs["json_schema"]
        == TASK_CLOSE_VALIDATION_SCHEMA
    )


@pytest.mark.asyncio
async def test_closure_reason_defaults_to_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator, _ = _validator()
    captured: dict[str, Any] = {}
    monkeypatch.setattr(validator._loader, "render", _capture_context(captured))

    await _run(validator)

    assert captured["closure_reason"] == "completed"


@pytest.mark.asyncio
async def test_blank_closure_reason_normalizes_to_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validator, _ = _validator()
    captured: dict[str, Any] = {}
    monkeypatch.setattr(validator._loader, "render", _capture_context(captured))

    await _run(validator, closure_reason="   ")

    assert captured["closure_reason"] == "completed"


def test_bundled_template_declares_and_renders_closure_reason() -> None:
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")

    assert 'version: "3.2"' in template
    assert "closure_reason:" in template
    assert "{{ closure_reason | untrusted }}" in template
    assert "transcript_operational_actions" in template
    assert "Diff and\ntest evidence alone cannot satisfy those actions." in template
    for reason in sorted(NO_WORK_CLOSE_REASONS):
        assert f"`{reason}`" in template
