"""Research reports require owner authority and an Orchestrator's report-SHA review."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

import pytest

from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.step_records import StepSubmission, record_task_step, task_step_requirements
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.engine.core import RuleEngine
from gobby.workflows.state_manager import SessionVariableManager
from tests.tasks.test_close_invariants import _set_close_policy, git
from tests.tasks.test_step_records import _Seats
from tests.workflows.test_seat_rules import _decide, _load_roles, _proxy_call

pytestmark = pytest.mark.unit


async def test_seat_close_rule_admits_researcher_done_request(temp_db: HubDatabase) -> None:
    _load_roles(temp_db)
    result = await _decide(
        RuleEngine(temp_db),
        _proxy_call("gobby-tasks", "close_task", {"task_id": "#25238", "reason": "done"}),
        {"_agent_type": "researcher"},
    )
    assert result.decision == "allow", result.reason


@dataclass
class Report:
    db: HubDatabase
    manager: LocalTaskManager
    repo: Path
    task: Task
    researcher: str
    orchestrator: str
    peer: str
    sha: str


@pytest.fixture(params=[("researcher", "orchestrator"), ("fixture-author", "fixture-approver")])
def report(
    temp_db: HubDatabase, sample_git_project: dict[str, Any], request: pytest.FixtureRequest
) -> Report:
    project_id = sample_git_project["id"]
    repo = Path(sample_git_project["repo_path"])
    _set_close_policy(temp_db, project_id, {"enforce_step_records": True})
    seats = _Seats(temp_db, project_id)
    author_name, approver_name = cast(tuple[str, str], request.param)
    researcher = seats.add(
        author_name,
        ["implementation", "closing"],
        capabilities=("close_without_work",),
        definition_fields={
            "task_step_scope": {"task_types": ["research_spike"], "own_claim_only": True},
            "task_close_scope": {
                "task_types": ["research_spike"],
                "categories": ["research"],
                "own_claim_only": True,
            },
            "task_close_reasons": ["done"],
            "required_review_capabilities": ["approve_report_review"],
        },
    )
    orchestrator = seats.add(approver_name, ["review"], capabilities=("approve_report_review",))
    peer = seats.add("research-reviewer", ["review"])
    manager = LocalTaskManager(temp_db)
    task = manager.create_task(
        project_id,
        "Investigate the routed question",
        task_type="research_spike",
        category="research",
        validation_criteria="The report answers the question.",
    )
    manager.claim_task(task.id, researcher)
    path = repo / ".gobby/plans/research/report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Report\nEvidence and findings.\n")
    git(repo, "add", str(path))
    git(repo, "commit", "-m", "research report")
    return Report(
        temp_db,
        manager,
        repo,
        manager.get_task(task.id),
        researcher,
        orchestrator,
        peer,
        git(repo, "rev-parse", "HEAD"),
    )


async def _link_and_record_owner_steps(report: Report) -> None:
    registry = create_task_registry(report.manager)
    with session_context_for_test(report.researcher):
        linked = await registry.call(
            "link_commit", {"task_id": report.task.id, "commit_sha": report.sha}
        )
        assert "error" not in linked, linked
    task = report.manager.get_task(report.task.id)
    required = task_step_requirements(report.db, task).required
    assert "review" in required
    for step in required:
        if step != "review":
            record_task_step(
                report.db,
                task_id=task.id,
                session_id=report.researcher,
                step=step,
                submission=StepSubmission(
                    status="approved",
                    epoch=task.review_epoch,
                    round_number=1,
                    commit_sha=report.sha,
                    feedback="The report is committed and complete.",
                ),
            )


def _review(report: Report, *, author: str, status: str = "approved") -> None:
    submission = StepSubmission.model_validate(
        {
            "status": status,
            "epoch": report.task.review_epoch,
            "round_number": 1,
            "commit_sha": report.sha,
            "feedback": "Reviewed the report and primary evidence.",
        }
    )
    record_task_step(
        report.db,
        task_id=report.task.id,
        session_id=author,
        step="review",
        submission=submission,
    )


@pytest.mark.parametrize("review", ["approved", "missing", "rejected", "older_sha", "other_task"])
async def test_researcher_close_requires_orchestrator_approval_for_report_sha(
    report: Report, review: str
) -> None:
    await _link_and_record_owner_steps(report)
    if review == "other_task":
        other = report.manager.create_task(
            report.task.project_id,
            "A different research question",
            task_type="research_spike",
            category="research",
            validation_criteria="Another report answers another question.",
        )
        report.manager.claim_task(other.id, report.researcher)
        other_report = replace(report, task=report.manager.get_task(other.id))
        await _link_and_record_owner_steps(other_report)
        _review(other_report, author=report.orchestrator)
    elif review != "missing":
        _review(
            report,
            author=report.orchestrator,
            status="rejected" if review == "rejected" else "approved",
        )
    if review == "older_sha":
        path = report.repo / ".gobby/plans/research/report.md"
        path.write_text("# Revised report\nNew findings require another review.\n")
        git(report.repo, "add", str(path))
        git(report.repo, "commit", "-m", "revise report")
        report.sha = git(report.repo, "rev-parse", "HEAD")
        await _link_and_record_owner_steps(report)
    registry = create_task_registry(report.manager)
    with session_context_for_test(report.researcher):
        result = await registry.call(
            "close_task", {"task_id": report.task.id, "commit_sha": report.sha, "reason": "done"}
        )
    assert (result.get("closed") is True) == (review == "approved"), result
    assert (report.manager.get_task(report.task.id).closed_at is not None) == (review == "approved")


async def test_research_reviewer_approval_does_not_authorize_researcher_close(
    report: Report,
) -> None:
    await _link_and_record_owner_steps(report)
    _review(report, author=report.peer)
    registry = create_task_registry(report.manager)
    with session_context_for_test(report.researcher):
        result = await registry.call(
            "close_task", {"task_id": report.task.id, "commit_sha": report.sha, "reason": "done"}
        )
    assert result.get("closed") is not True, result
    assert report.manager.get_task(report.task.id).closed_at is None


@pytest.mark.parametrize("revoke_after_review", [False, True])
async def test_report_review_grant_is_captured_at_approval(
    report: Report, revoke_after_review: bool
) -> None:
    await _link_and_record_owner_steps(report)
    definitions = AgentDefinitionManager(report.db)
    role = SessionVariableManager(report.db).get_variables(report.orchestrator)["_agent_type"]
    definition = definitions.get_by_name(role, report.task.project_id)
    assert definition is not None
    if revoke_after_review:
        _review(report, author=report.orchestrator)
    definitions.update(
        definition.id, definition_json={**definition.definition_json, "capabilities": []}
    )
    if not revoke_after_review:
        _review(report, author=report.orchestrator)
    with session_context_for_test(report.researcher):
        result = await create_task_registry(report.manager).call(
            "close_task", {"task_id": report.task.id, "commit_sha": report.sha}
        )
    assert (result.get("closed") is True) is revoke_after_review


@pytest.mark.parametrize("enabled", [True, False])
async def test_close_uses_enabled_definition_policy_instead_of_author_name(
    report: Report, enabled: bool
) -> None:
    await _link_and_record_owner_steps(report)
    _review(report, author=report.peer)
    definitions = AgentDefinitionManager(report.db)
    role = SessionVariableManager(report.db).get_variables(report.researcher)["_agent_type"]
    definition = definitions.get_by_name(role, report.task.project_id)
    assert definition is not None
    definitions.update(
        definition.id,
        enabled=enabled,
        definition_json={
            **definition.definition_json,
            "task_close_scope": {},
            "required_review_capabilities": [],
        },
    )
    report.manager.claim_task(report.task.id, report.peer, force=True)
    with session_context_for_test(report.researcher):
        result = await create_task_registry(report.manager).call(
            "close_task", {"task_id": report.task.id, "commit_sha": report.sha}
        )
    assert (result.get("closed") is True) is enabled


async def test_own_claim_close_replay_survives_claim_release(report: Report) -> None:
    await _link_and_record_owner_steps(report)
    _review(report, author=report.orchestrator)
    registry = create_task_registry(report.manager)
    with session_context_for_test(report.researcher):
        first = await registry.call(
            "close_task", {"task_id": report.task.id, "commit_sha": report.sha}
        )
        assert first.get("closed") is True
        assert report.manager.get_task(report.task.id).claimed_by_session_id is None
        replay = await registry.call(
            "close_task", {"task_id": report.task.id, "commit_sha": report.sha}
        )
    assert replay.get("closed") is True


@pytest.mark.parametrize("target", ["foreign_spike", "code_task", "research_non_spike"])
async def test_researcher_cannot_close_tasks_outside_its_claimed_research_spike(
    report: Report, target: str
) -> None:
    await _link_and_record_owner_steps(report)
    _review(report, author=report.orchestrator)
    # Preserve complete evidence while changing only the authority boundary under test.
    if target == "foreign_spike":
        report.manager.claim_task(report.task.id, report.peer, force=True)
    else:
        report.manager.update_task(
            report.task.id,
            category="code" if target == "code_task" else "research",
            task_type="research_spike" if target == "code_task" else "task",
        )
    registry = create_task_registry(report.manager)
    with session_context_for_test(report.researcher):
        result = await registry.call(
            "close_task", {"task_id": report.task.id, "commit_sha": report.sha, "reason": "done"}
        )
    assert result.get("closed") is not True, result
    assert report.manager.get_task(report.task.id).closed_at is None


@pytest.mark.parametrize("reason", ["moot", "superseded", "wont_do"])
async def test_researcher_close_allows_only_done(report: Report, reason: str) -> None:
    await _link_and_record_owner_steps(report)
    _review(report, author=report.orchestrator)
    registry = create_task_registry(report.manager)
    with session_context_for_test(report.researcher):
        result = await registry.call(
            "close_task",
            {
                "task_id": report.task.id,
                "commit_sha": report.sha,
                "reason": reason,
                "superseded_by": "#123",
            },
        )
    assert result.get("closed") is not True, result
    assert report.manager.get_task(report.task.id).closed_at is None
