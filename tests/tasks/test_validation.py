"""TaskValidator prompt contracts and task-close criteria review continuity."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from gobby.config.tasks import TaskValidationConfig
from gobby.llm import LLMService
from gobby.mcp_proxy.tools.tasks._context import RegistryContext
from gobby.mcp_proxy.tools.tasks._lifecycle_validation import evaluate_criteria_review
from gobby.prompts.loader import PromptLoader
from gobby.prompts.models import PromptTemplate, parse_frontmatter
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.task_close_reviews import TaskCloseReviewStore
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.close_verdict_memo import TaskCloseVerdictMemo
from gobby.tasks.criteria_contract import split_validation_criteria
from gobby.tasks.generation_schemas import TASK_CLOSE_VALIDATION_SCHEMA
from gobby.tasks.validation import TaskValidator

pytestmark = pytest.mark.integration

_SESSION_ID = "00000000-0000-4000-8000-000000002145"
_CRITERION = "The close response reports complete evidence requirements."
_GAP = "Exercise the close adapter instead of only the review engine."
_REQUIRED_EVIDENCE = (
    "Invoke close_task through the real rule and adapter, then capture the MCP response receipt."
)
_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2] / "src/gobby/install/shared/prompts/validation/validate.md"
)


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


def test_prompt_and_generation_schema_define_optional_required_evidence() -> None:
    template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "must state the complete evidence set" in template
    assert "Requirements already stated in the prior review" in template
    assert "Do not add an implementation or evidence requirement absent from" in template

    criteria_schema = TASK_CLOSE_VALIDATION_SCHEMA["properties"]["criteria"]["items"]
    assert criteria_schema["properties"]["required_evidence"] == {"type": ["string", "null"]}
    assert criteria_schema["properties"]["state"]["enum"] == [
        "satisfied",
        "gap",
        "pending_external",
    ]
    assert "required_evidence" not in criteria_schema["required"]


def _render_context(
    _path: str,
    context: dict[str, Any] | None = None,
    strict: bool = False,
) -> str:
    del strict
    return json.dumps(context or {}, sort_keys=True, default=str)


def _validator(temp_db: HubDatabase) -> tuple[TaskValidator, AsyncMock]:
    llm_service = MagicMock(spec=LLMService)
    call_json_feature = AsyncMock(
        side_effect=[
            {
                "status": "invalid",
                "criteria": [
                    {
                        "index": 1,
                        "satisfied": False,
                        "gap": _GAP,
                        "required_evidence": _REQUIRED_EVIDENCE,
                    }
                ],
                "feedback": "The criterion needs stronger evidence.",
            },
            {
                "status": "invalid",
                "criteria": [
                    {
                        "index": 1,
                        "satisfied": False,
                        "gap": _GAP,
                        "required_evidence": _REQUIRED_EVIDENCE,
                    }
                ],
                "feedback": "The criterion still needs stronger evidence.",
            },
        ]
    )
    llm_service.call_json_feature = call_json_feature
    validator = TaskValidator(TaskValidationConfig(), llm_service, db=temp_db)
    cast(Any, validator._loader).render = _render_context
    return validator, call_json_feature


async def _evaluate(
    *,
    task: Task,
    manager: LocalTaskManager,
    validator: TaskValidator,
    memo: TaskCloseVerdictMemo,
    commit_sha: str,
) -> Any:
    ctx = cast(RegistryContext, SimpleNamespace(task_manager=manager))
    return await evaluate_criteria_review(
        task=task,
        task_validator=validator,
        ctx=ctx,
        resolved_id=task.id,
        changes_summary="Added complete close-review evidence requirements.",
        diff_text=f"diff --git a/src/review.py b/src/review.py\n+commit = '{commit_sha}'",
        checklist_facts={"validation_commands": "passed", "commit_shas": [commit_sha]},
        validation_config=None,
        reason="completed",
        verdict_memo=memo,
    )


async def test_rejection_evidence_reaches_response_but_not_the_next_review(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
) -> None:
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id=sample_project["id"],
        title="Preserve close-review requirements",
        category="code",
        implementation_domain="backend",
        validation_criteria=_CRITERION,
    )
    validator, call_json_feature = _validator(temp_db)
    memo = TaskCloseVerdictMemo(
        TaskCloseReviewStore(temp_db),
        task_id=task.id,
        task_ref=f"#{task.seq_num}",
        caller_session_id=_SESSION_ID,
        close_arguments={"reason": "completed"},
        criteria=split_validation_criteria(task.validation_criteria or ""),
    )

    await _evaluate(
        task=task,
        manager=manager,
        validator=validator,
        memo=memo,
        commit_sha="first",
    )
    refreshed = manager.get_task(task.id)
    assert refreshed is not None
    result = await _evaluate(
        task=refreshed,
        manager=manager,
        validator=validator,
        memo=memo,
        commit_sha="second",
    )

    second_prompt = json.loads(call_json_feature.await_args_list[1].args[1])
    assert second_prompt["prior_requirements"] == (
        "No requirements were stated by a prior rejected review."
    )
    complete_requirement = f"{_GAP} Required evidence: {_REQUIRED_EVIDENCE}"
    assert result.extra["blocking_reasons"] == [complete_requirement]
    assert result.extra["required_actions"] == [complete_requirement]
    assert result.extra["verdict"]["criteria"][0]["required_evidence"] == _REQUIRED_EVIDENCE

    refreshed = manager.get_task(task.id)
    assert refreshed is not None
    await _evaluate(
        task=refreshed,
        manager=manager,
        validator=validator,
        memo=memo,
        commit_sha="second",
    )
    assert call_json_feature.await_count == 2


_DELIVERABLE_FACTS: dict[str, Any] = {
    "commit_count": 1,
    "commit_shas": ["abc1234"],
    "had_attributed_edits": True,
    "attributed_paths": ["src/gobby/tasks/validation.py"],
    "claim_started_at": "2026-09-03T05:00:00+00:00",
}


def _prepare(
    temp_db: HubDatabase,
    checklist_facts: dict[str, Any],
) -> Any:
    validator = TaskValidator(
        TaskValidationConfig(),
        cast(LLMService, SimpleNamespace(call_json_feature=AsyncMock())),
        temp_db,
    )
    return validator.prepare_task_review(
        title="Stable fingerprint",
        changes_summary="summary",
        validation_criteria="Focused tests pass.",
        diff_text="diff --git a/x b/x",
        checklist_facts=checklist_facts,
        test_bodies="def test_x() -> None:\n    assert True",
    )


def test_additive_transcript_evidence_does_not_stale_a_launched_review(
    temp_db: HubDatabase,
) -> None:
    """Running another validation command must not void an in-flight verdict.

    The transcript-derived facts change on every command the launching session
    runs. Keying the fingerprints on them made a session void its own verdict
    simply by continuing to work (#21675).
    """
    at_launch = _prepare(
        temp_db,
        {
            **_DELIVERABLE_FACTS,
            "validation_commands": [{"command": "pytest tests/tasks/", "outcome": "success"}],
            "transcript_operational_actions": ["pytest"],
            "acceptance_artifacts": {"findings": [], "test_references": ["a::test_b"]},
            "tdd_evidence": {"red_runs": [], "green_runs": ["pytest tests/tasks/"]},
        },
    )
    at_verdict = _prepare(
        temp_db,
        {
            **_DELIVERABLE_FACTS,
            "validation_commands": [
                {"command": "pytest tests/tasks/", "outcome": "success"},
                {"command": "ruff check src/", "outcome": "success"},
            ],
            "transcript_operational_actions": ["pytest", "ruff"],
            "acceptance_artifacts": {"findings": [], "test_references": ["a::test_b"]},
            "tdd_evidence": {
                "red_runs": [],
                "green_runs": ["pytest tests/tasks/", "ruff check src/"],
            },
        },
    )

    assert at_verdict.evidence_fingerprint == at_launch.evidence_fingerprint
    assert at_verdict.review_fingerprint == at_launch.review_fingerprint


def test_reviewer_still_reads_the_transcript_facts_excluded_from_the_fingerprint(
    temp_db: HubDatabase,
) -> None:
    """Narrowing the fingerprint must not narrow what the reviewer is shown."""
    prepared = _prepare(
        temp_db,
        {**_DELIVERABLE_FACTS, "transcript_operational_actions": ["gobby-cutover-marker"]},
    )

    assert "gobby-cutover-marker" in prepared.prompt


def test_reviewer_prompt_marks_gate10_validation_runs_authoritative(
    temp_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
    mock_validation_prompt_loader: MagicMock,
) -> None:
    """Gate 10's run record reaches the reviewer as the authority on command runs."""
    # tests/tasks/conftest.py stubs the loader with a variables-only render;
    # render the bundled template through the real loader so its guidance is
    # what is judged.
    frontmatter, body = parse_frontmatter(_TEMPLATE_PATH.read_text(encoding="utf-8"))
    bundled = PromptTemplate.from_frontmatter("validation/validate", frontmatter, body)
    loader = PromptLoader(db=temp_db)
    monkeypatch.setattr(loader, "load", lambda _path: bundled)
    mock_validation_prompt_loader.render.side_effect = loader.render
    run = {
        "category": "test",
        "command": "uv run pytest tests/tasks/test_validation.py -q",
        "completed_at": "2026-09-03T05:10:00+00:00",
        "outcome": "success",
        "exit_code": 0,
    }
    prepared = _prepare(
        temp_db,
        {
            **_DELIVERABLE_FACTS,
            "validation_commands": {"latest_outcomes": {"test": "success"}, "latest_runs": [run]},
        },
    )

    prompt = prepared.prompt
    assert run["command"] in prompt
    assert run["completed_at"] in prompt
    assert '"exit_code":0' in prompt
    assert "criterion_commands` normalizes both the criterion and transcript commands" in prompt
    assert "report every command gap in one verdict" in prompt
    assert "a log, receipt, or other file committed to the repository as proof of a" in prompt
    assert "command run" in prompt
    assert "receipt or artifact that must result" not in prompt


def test_claim_start_change_does_not_move_review_fingerprints(temp_db: HubDatabase) -> None:
    at_launch = _prepare(temp_db, dict(_DELIVERABLE_FACTS))
    after_claim_change = _prepare(
        temp_db,
        {**_DELIVERABLE_FACTS, "claim_started_at": "2026-09-03T06:00:00+00:00"},
    )

    assert after_claim_change.evidence_fingerprint == at_launch.evidence_fingerprint
    assert after_claim_change.review_fingerprint == at_launch.review_fingerprint


def test_new_commit_after_launch_still_stales_the_review(temp_db: HubDatabase) -> None:
    at_launch = _prepare(temp_db, dict(_DELIVERABLE_FACTS))
    with_new_commit = _prepare(
        temp_db,
        {**_DELIVERABLE_FACTS, "commit_count": 2, "commit_shas": ["abc1234", "def5678"]},
    )

    assert with_new_commit.evidence_fingerprint != at_launch.evidence_fingerprint
    assert with_new_commit.review_fingerprint != at_launch.review_fingerprint


def test_new_attributed_edit_after_launch_still_stales_the_review(
    temp_db: HubDatabase,
) -> None:
    at_launch = _prepare(temp_db, dict(_DELIVERABLE_FACTS))
    with_new_edit = _prepare(
        temp_db,
        {
            **_DELIVERABLE_FACTS,
            "attributed_paths": [
                "src/gobby/tasks/validation.py",
                "src/gobby/tasks/close_checklist.py",
            ],
        },
    )

    assert with_new_edit.evidence_fingerprint != at_launch.evidence_fingerprint
    assert with_new_edit.review_fingerprint != at_launch.review_fingerprint
