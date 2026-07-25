"""Validation-miss lesson injection tests."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, cast

import pytest
from pytest_mock import MockerFixture

from gobby.config.tasks import TaskValidationConfig
from gobby.llm import LLMService
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import validate_leaf_task_with_llm
from gobby.prompts import PromptLoader
from gobby.prompts.models import PromptTemplate, parse_frontmatter
from gobby.review_learning.service import ReviewLearningService
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.validation import TaskValidator
from gobby.tasks.validation_history import ValidationHistoryManager
from gobby.tasks.validation_models import Issue, IssueSeverity, IssueType

pytestmark = pytest.mark.integration

_LESSON_MESSAGE = """<review-guidance>
- matched lesson class [epic-qa:validation-miss:focused-tests]
  Do: Require the focused regression before accepting the fix
</review-guidance>"""
_LESSON_TEMPLATE_BLOCK = """{% if lessons_section %}Prior validation-miss lessons:
{{ lessons_section | untrusted }}

{% endif %}Task: {{ title | untrusted }}"""
_ISSUE_LOCATION = "src/gobby/tasks/validation.py:TaskValidator.validate_task"
_PROMPT_PATH = Path("src/gobby/install/shared/prompts/validation/validate.md")


class _RecallService:
    """Small ReviewLearningService test seam."""

    def __init__(
        self,
        *,
        message: str = "",
        error: Exception | None = None,
    ) -> None:
        self.message = message
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def recall_review_lessons_by_class(
        self,
        *,
        lesson_domain: str,
        lesson_types: list[str],
        limit: int,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "lesson_domain": lesson_domain,
                "lesson_types": lesson_types,
                "limit": limit,
            }
        )
        if self.error is not None:
            raise self.error
        return {
            "count": 1 if self.message else 0,
            "lessons": [{"message": self.message}] if self.message else [],
            "message": self.message,
        }


def _task(manager: LocalTaskManager, project_id: str, title: str) -> Task:
    return manager.create_task(
        project_id=project_id,
        title=title,
        category="code",
        description="Exercise validator lesson injection.",
        validation_criteria="Focused validation evidence is complete.",
    )


def _validator(
    db: HubDatabase,
    mocker: MockerFixture,
    responses: list[dict[str, object]],
) -> tuple[TaskValidator, Any]:
    llm = mocker.MagicMock(spec=LLMService)
    llm.call_json_feature = mocker.AsyncMock(side_effect=responses)
    config = TaskValidationConfig(enabled=True, candidates=["claude/test-model"])
    validator = TaskValidator(config, cast(LLMService, llm), db=db)
    template = _prompt_template()
    loader = PromptLoader(db=db)
    mocker.patch.object(loader, "load", return_value=template)
    validator._loader = loader
    return validator, llm.call_json_feature


def _prompt_template() -> PromptTemplate:
    frontmatter, content = parse_frontmatter(_PROMPT_PATH.read_text(encoding="utf-8"))
    return PromptTemplate.from_frontmatter(
        "validation/validate",
        frontmatter,
        content,
        _PROMPT_PATH,
    )


def _context(manager: LocalTaskManager, service: _RecallService) -> RegistryContext:
    return RegistryContext(
        task_manager=manager,
        review_learning_service=cast(ReviewLearningService, service),
    )


def _valid_response() -> dict[str, object]:
    return {
        "status": "valid",
        "feedback": "Focused validation passes.",
        "blocking_reasons": [],
        "criterion_results": [
            {
                "criterion": "Focused validation evidence is complete.",
                "status": "satisfied",
                "evidence_ids": ["evidence-1"],
                "explanation": "The focused receipt records a successful current result.",
            }
        ],
        "issues": [],
        "current_failure_evidence": [],
    }


@pytest.mark.asyncio
async def test_lessons_section_empty_safe(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    mocker: MockerFixture,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _task(manager, sample_project["id"], "Empty lesson injection")
    service = _RecallService()
    validator, call_json = _validator(temp_db, mocker, [_valid_response()])

    result = await validate_leaf_task_with_llm(
        task,
        validator,
        "structured evidence",
        _context(manager, service),
        task.id,
        validator.config,
        verification_receipt_text="EVIDENCE_ID: evidence-1\nOUTCOME: success",
        admissible_evidence_ids=["evidence-1"],
        read_only=True,
    )

    prompt = cast(str, call_json.await_args.args[1])
    assert result.can_close is True
    assert service.calls == [
        {
            "lesson_domain": "code",
            "lesson_types": ["validation-miss"],
            "limit": 3,
        }
    ]
    assert "Prior validation-miss lessons:" not in prompt
    assert "Task: <untrusted_content>" in prompt

    template = _prompt_template()
    assert _LESSON_TEMPLATE_BLOCK in template.content
    context = template.get_default_context()
    context.update(
        {
            "title": "Byte-identical empty rendering",
            "criteria_text": "criteria",
            "changes_section": "changes",
            "lessons_section": "",
        }
    )
    loader = PromptLoader(db=temp_db)
    rendered = loader._render_jinja(template.content, context)
    legacy = loader._render_jinja(
        template.content.replace(
            _LESSON_TEMPLATE_BLOCK,
            "Task: {{ title | untrusted }}",
        ),
        context,
    )
    assert rendered == legacy


@pytest.mark.asyncio
async def test_issues_contract_preserved(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    mocker: MockerFixture,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _task(manager, sample_project["id"], "Lesson-backed issue")
    service = _RecallService(message=_LESSON_MESSAGE)
    validator, call_json = _validator(
        temp_db,
        mocker,
        [
            {
                "status": "invalid",
                "feedback": "Focused regression evidence is missing.",
                "blocking_reasons": ["Focused regression was not run."],
                "issues": [
                    {
                        "title": "Focused regression missing",
                        "type": "test_failure",
                        "severity": "major",
                        "location": _ISSUE_LOCATION,
                    }
                ],
                "current_failure_evidence": ["Focused regression was not run."],
            }
        ],
    )

    result = await validate_leaf_task_with_llm(
        task,
        validator,
        "structured evidence",
        _context(manager, service),
        task.id,
        validator.config,
    )

    prompt = cast(str, call_json.await_args.args[1])
    history = ValidationHistoryManager(temp_db).get_iteration_history(task.id)
    assert result.can_close is False
    assert "Prior validation-miss lessons:" in prompt
    assert "Require the focused regression before accepting the fix" in prompt
    assert history[0].issues == [
        Issue(
            issue_type=IssueType.TEST_FAILURE,
            severity=IssueSeverity.MAJOR,
            title="Focused regression missing",
            location=_ISSUE_LOCATION,
        )
    ]


@pytest.mark.asyncio
async def test_recall_failure_diagnostic(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    mocker: MockerFixture,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = LocalTaskManager(temp_db)
    task = _task(manager, sample_project["id"], "Recall diagnostic")
    validator, call_json = _validator(
        temp_db,
        mocker,
        [_valid_response(), _valid_response()],
    )

    empty_result = await validate_leaf_task_with_llm(
        task,
        validator,
        "structured evidence",
        _context(manager, _RecallService()),
        task.id,
        validator.config,
        verification_receipt_text="EVIDENCE_ID: evidence-1\nOUTCOME: success",
        admissible_evidence_ids=["evidence-1"],
        read_only=True,
    )
    with caplog.at_level(logging.WARNING):
        failed_result = await validate_leaf_task_with_llm(
            task,
            validator,
            "structured evidence",
            _context(
                manager,
                _RecallService(error=RuntimeError("project scope is unresolved")),
            ),
            task.id,
            validator.config,
            verification_receipt_text="EVIDENCE_ID: evidence-1\nOUTCOME: success",
            admissible_evidence_ids=["evidence-1"],
            read_only=True,
        )

    empty_prompt = cast(str, call_json.await_args_list[0].args[1])
    failed_prompt = cast(str, call_json.await_args_list[1].args[1])
    assert empty_prompt == failed_prompt
    assert empty_result.extra is not None
    assert empty_result.extra.get("diagnostics") is None
    assert failed_result.extra is not None
    assert failed_result.extra["diagnostics"] == [
        {
            "code": "lesson-recall-failed",
            "severity": "warning",
            "detail": "project scope is unresolved",
        }
    ]
    assert caplog.text.count("Validation lesson recall failed") == 1
