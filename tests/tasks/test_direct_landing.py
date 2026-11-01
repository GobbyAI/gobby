"""Declared direct-integration permissions permit self-authored task delivery."""

from pathlib import Path
from typing import Any

import pytest

from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.tasks.close_receipts import list_close_receipts
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.landing_case import LandingCase, make_landing_case
from tests.fixtures.review_approvals import approve_review

pytestmark = pytest.mark.unit


@pytest.fixture
def case(temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path) -> LandingCase:
    return make_landing_case(temp_db, sample_project, tmp_path)


def grant(case: LandingCase, capabilities: list[str], paths: list[str]) -> str:
    name = "solo-author"
    definition = AgentDefinitionManager(case.db).create(
        name,
        {
            "name": name,
            "prompts": {"agent": "Implement and deliver owned work."},
            "workflows": {"rule_selectors": {"include": []}},
            "capabilities": capabilities,
            "direct_integration_commit_paths": paths,
            "task_step_writes": ["implementation", "closing"],
        },
        project_id=case.project_id,
    )
    SessionVariableManager(case.db).set_variable(case.claimant, "_agent_type", name)
    return definition.id


@pytest.mark.parametrize(
    ("capabilities", "paths", "allowed"),
    [
        (["land_tasks", "direct_integration_commit"], ["**"], True),
        (["land_tasks", "direct_integration_commit"], ["src/**"], True),
        (["land_tasks", "direct_integration_commit"], ["docs/**"], False),
        (["land_tasks"], ["**"], False),
        (["direct_integration_commit"], ["**"], False),
    ],
)
async def test_self_landing_uses_declared_authority(
    case: LandingCase, capabilities: list[str], paths: list[str], allowed: bool
) -> None:
    sha = case.candidate("own-delivery", {"src/owned.py": "implemented = True\n"})
    task = case.task()
    case.link(task, sha)
    case.implement(task, sha)
    grant(case, capabilities, paths)

    result = await case.land(task, sha, caller=case.claimant)

    assert result["landed"] is allowed, result
    assert case.git("rev-parse", "trunk") == (sha if allowed else case.base)
    receipts = list_close_receipts(case.db, task.id)
    assert bool(receipts) is allowed
    if allowed:
        replay = await case.land(task, sha, caller=case.claimant)
        assert replay["landed"] is True


@pytest.mark.parametrize("remaining_paths", [["docs/**"], []])
async def test_replay_rechecks_original_candidate_paths(
    case: LandingCase, remaining_paths: list[str]
) -> None:
    sha = case.candidate("own-delivery", {"src/owned.py": "implemented = True\n"})
    # The original integration tip has a disjoint change outside the direct grant.
    upstream = case.direct({"docs/upstream.md": "upstream\n"})
    task = case.task()
    case.link(task, sha)
    case.implement(task, sha)
    definition_id = grant(case, ["land_tasks", "direct_integration_commit"], ["src/**"])
    landed = await case.land(task, sha, caller=case.claimant)
    assert landed["landed"] is True, landed
    assert case.git("merge-base", "trunk", upstream) == upstream
    assert (await case.land(task, sha, caller=case.claimant))["landed"] is True

    definitions = AgentDefinitionManager(case.db)
    definition = definitions.get(definition_id)
    assert definition is not None
    definitions.update(
        definition_id,
        definition_json={
            **definition.definition_json,
            "direct_integration_commit_paths": remaining_paths,
        },
    )
    receipts = list_close_receipts(case.db, task.id)
    replay = await case.land(task, sha, caller=case.claimant)
    assert replay["landed"] is False, replay
    assert list_close_receipts(case.db, task.id) == receipts


async def test_direct_replay_requires_scope_provenance(case: LandingCase) -> None:
    sha = case.candidate("external-delivery", {"src/owned.py": "implemented = True\n"})
    task = case.task()
    case.link(task, sha)
    case.implement(task, sha)
    grant(case, ["land_tasks", "direct_integration_commit"], ["src/**"])
    # An external Git update supplies no daemon-verified original landing scope.
    case.git("merge", "--ff-only", sha)

    result = await case.land(task, sha, caller=case.claimant)

    assert result["landed"] is False, result
    assert list_close_receipts(case.db, task.id) == []


@pytest.mark.parametrize("implementation", ["missing", "stale"])
async def test_direct_landing_requires_implementation_at_candidate(
    case: LandingCase, implementation: str
) -> None:
    sha = case.candidate("own-delivery", {"src/owned.py": "implemented = True\n"})
    task = case.task()
    if implementation == "stale":
        case.link(task, case.base)
        case.implement(task, case.base)
    case.link(task, sha)
    grant(case, ["land_tasks", "direct_integration_commit"], ["src/**"])

    result = await case.land(task, sha, caller=case.claimant)

    assert result["landed"] is False, result
    assert "implementation_approval_missing" in result["blockers"]
    assert case.git("rev-parse", "trunk") == case.base
    assert list_close_receipts(case.db, task.id) == []


async def test_direct_landing_preserves_required_security_approval(case: LandingCase) -> None:
    sha = case.candidate("own-delivery", {"src/owned.py": "implemented = True\n"})
    task = case.task(labels=["security-review"])
    case.link(task, sha)
    case.implement(task, sha)
    grant(case, ["land_tasks", "direct_integration_commit"], ["src/**"])

    refused = await case.land(task, sha, caller=case.claimant)

    assert refused["landed"] is False, refused
    assert refused["blockers"] == ["security_review_missing"]
    assert case.git("rev-parse", "trunk") == case.base
    assert list_close_receipts(case.db, task.id) == []

    reviewer = case.security_reviewer()
    approve_review(case.db, task_id=task.id, reviewer_session_id=reviewer, commit_sha=sha)
    landed = await case.land(task, sha, caller=case.claimant)

    assert landed["landed"] is True, landed
    assert case.git("rev-parse", "trunk") == sha
