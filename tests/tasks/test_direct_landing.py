"""Declared direct-integration permissions permit self-authored task delivery."""

import json
from pathlib import Path
from typing import Any

import pytest

from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.tasks.attestations import AttestationSubmission, record_attestation
from gobby.tasks.close_receipts import list_close_receipts
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.landing_case import LandingCase, make_landing_case

pytestmark = pytest.mark.unit


@pytest.fixture
def case(temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path) -> LandingCase:
    return make_landing_case(temp_db, sample_project, tmp_path)


def grant(case: LandingCase, capabilities: list[str], paths: list[str]) -> str:
    name = "arbitrary-direct-author"
    rules = {
        "declared-path-authority": "not facts.get('integration_path_grant')",
        "implementation_approval_missing": (
            "latest_attestation('implementation').get('result') != 'approved' or "
            "(latest_attestation('implementation').get('reference') or {}).get('value') "
            "!= facts.get('candidate_commit_sha')"
        ),
        "security_review_missing": (
            "'security-review' in task.get('labels', []) and not any("
            "record.get('kind') == 'security-review' and record.get('result') == 'approved' and "
            "'approve_security_review' in record.get('agent_capabilities', []) and "
            "(record.get('reference') or {}).get('value') == facts.get('candidate_commit_sha') "
            "for record in attestations)"
        ),
    }
    for rule, condition in rules.items():
        RuleDefinitionManager(case.db).create(
            rule,
            {
                "event": "task_land",
                "when": condition,
                "effects": [{"type": "block", "reason": rule}],
            },
            project_id=case.project_id,
        )
    definition = AgentDefinitionManager(case.db).create(
        name,
        {
            "name": name,
            "prompts": {"agent": "Implement and deliver owned work."},
            "workflows": {"rule_selectors": {"include": list(rules)}},
            "capabilities": capabilities,
            "direct_integration_commit_paths": paths,
            "task_attestation_writes": ["*"],
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


async def test_receiptless_direct_commit_derives_immutable_scope(case: LandingCase) -> None:
    sha = case.candidate("external-delivery", {"src/owned.py": "implemented = True\n"})
    task = case.task()
    case.link(task, sha)
    case.implement(task, sha)
    grant(case, ["land_tasks", "direct_integration_commit"], ["src/**"])
    # An external Git update supplies no daemon-verified original landing scope.
    case.git("merge", "--ff-only", sha)

    result = await case.land(task, sha, caller=case.claimant)

    assert result["landed"] is True, result
    assert list_close_receipts(case.db, task.id)


async def test_receiptless_root_commit_has_proven_scope(case: LandingCase) -> None:
    task = case.task()
    case.link(task, case.base)
    case.implement(task, case.base)
    grant(case, ["land_tasks", "direct_integration_commit"], ["README.md"])
    result = await case.land(task, case.base, caller=case.claimant)
    assert result["landed"] is True, result


async def test_receiptless_task_retains_earlier_linked_commit_scope(case: LandingCase) -> None:
    first = case.candidate("first-change", {"src/auth.py": "protected = True\n"})
    last = case.candidate("last-change", {"docs/readme.md": "Documented\n"}, base=first)
    task = case.task()
    case.db.execute(
        "UPDATE tasks SET commits = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
        (json.dumps([first, last]), task.id),
    )
    case.implement(task, last)
    grant(case, ["land_tasks", "direct_integration_commit"], ["docs/**"])
    case.git("merge", "--ff-only", last)

    result = await case.land(task, last, caller=case.claimant)

    assert result["landed"] is False, result
    assert not list_close_receipts(case.db, task.id)


async def test_receiptless_merge_has_first_parent_scope(case: LandingCase) -> None:
    from gobby.tasks.land_commit import candidate_scope_paths

    branch = case.candidate("merged-change", {"src/auth.py": "protected = True\n"})
    case.git("merge", "--no-ff", branch, "-m", "External merge")
    merged = case.git("rev-parse", "HEAD")

    assert await candidate_scope_paths(case.repo, merged, merged, []) == ["src/auth.py"]


async def test_reflog_recovery_retains_scope_base(case: LandingCase) -> None:
    sha = case.candidate("recover-source", {"src/owned.py": "implemented = True\n"})
    task = case.task()
    case.link(task, sha)
    case.implement(task, sha)
    grant(case, ["land_tasks", "direct_integration_commit"], ["src/**"])
    assert (await case.land(task, sha, caller=case.claimant))["landed"] is True
    case.db.execute(
        "DELETE FROM task_comments WHERE task_id = %s AND author_type = %s",
        (task.id, "close_receipt"),
    )
    recovered = await case.land(task, sha, caller=case.claimant)
    assert recovered["landed"] is True, recovered
    receipt = list_close_receipts(case.db, task.id)[0]
    assert receipt.facts["integration_base"] == case.base
    assert receipt.facts["provenance"] == "reflog"


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

    reviewer = case.security_seat("arbitrary-boundary-auditor")
    record_attestation(
        case.db,
        task_id=task.id,
        session_id=reviewer,
        submission=AttestationSubmission(
            kind="security-review",
            result="approved",
            summary="Checked protected boundary.",
            reference={"scheme": "git", "value": sha},
        ),
    )
    landed = await case.land(task, sha, caller=case.claimant)

    assert landed["landed"] is True, landed
    assert case.git("rev-parse", "trunk") == sha


async def test_merge_delivery_closes_with_source_approvals_and_integrated_checks(
    case: LandingCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gobby.mcp_proxy.tools.internal import InternalToolRegistry
    from gobby.mcp_proxy.tools.tasks._context import RegistryContext
    from gobby.mcp_proxy.tools.tasks._lifecycle_close_tool import register_close_task
    from gobby.storage.tasks import LocalTaskManager

    sha = case.candidate("source-work", {"src/owned.py": "implemented = True\n"})
    case.direct({"docs/unrelated.md": "upstream\n"})
    task = case.task(labels=["security-review"])
    case.link(task, sha)
    case.implement(task, sha)
    definition_id = grant(case, ["land_tasks", "direct_integration_commit"], ["src/**"])
    auditor = case.security_seat("arbitrary-boundary-auditor")
    record_attestation(
        case.db,
        task_id=task.id,
        session_id=auditor,
        submission=AttestationSubmission(
            kind="security-review",
            result="approved",
            summary="Boundary reviewed.",
            reference={"scheme": "git", "value": sha},
        ),
    )
    landed = await case.land(task, sha, caller=case.claimant)
    assert landed["landed"] and landed["mode"] == "merge", landed
    integrated = landed["landed_tip"]
    assert integrated != sha
    close_condition = (
        "not facts.get('candidate_paths_available') or facts.get('candidate_paths') != ['src/owned.py'] or "
        "not any(receipt.get('facts').get('landed_tip') == facts.get('candidate_commit_sha') and "
        "all(latest_attestation(kind).get('reference', {}).get('value') == receipt.get('commit_sha') "
        "for kind in ['implementation', 'security-review']) and "
        "latest_attestation('validation').get('reference', {}).get('value') == facts.get('candidate_commit_sha') and "
        "timestamp(latest_attestation('validation').get('created_at')) > timestamp(receipt.get('recorded_at')) "
        "for receipt in facts.get('landing_receipts', []))"
    )
    RuleDefinitionManager(case.db).create(
        "integrated-checks",
        {
            "event": "task_close",
            "when": close_condition,
            "effects": [{"type": "block", "reason": "Validate the integrated delivery."}],
        },
        project_id=case.project_id,
    )
    definitions = AgentDefinitionManager(case.db)
    definition = definitions.get(definition_id)
    definitions.update(
        definition_id,
        definition_json={
            **definition.definition_json,
            "workflows": {"rule_selectors": {"include": ["integrated-checks"]}},
        },
    )
    registry = InternalToolRegistry("gobby-tasks")
    register_close_task(registry, RegistryContext(task_manager=LocalTaskManager(case.db)))
    monkeypatch.setattr(
        "gobby.mcp_proxy.tools.tasks._lifecycle_close.get_current_session_id", lambda: case.claimant
    )
    refused = await registry.call(
        "close_task",
        {
            "task_id": task.id,
            "commit_sha": integrated,
            "project_path": str(case.repo),
            "preview": True,
        },
    )
    assert not refused.get("allowed", False), refused
    record_attestation(
        case.db,
        task_id=task.id,
        session_id=case.claimant,
        submission=AttestationSubmission(
            kind="validation",
            result="approved",
            summary="Integrated tree checked.",
            reference={"scheme": "git", "value": integrated},
            evidence={"stage": "validate"},
        ),
    )
    closed = await registry.call(
        "close_task", {"task_id": task.id, "commit_sha": integrated, "project_path": str(case.repo)}
    )
    assert closed.get("closed") is True, closed
