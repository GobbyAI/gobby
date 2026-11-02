"""Landing uses selected policies and generic evidence, without fixed workflow gates."""

import json
from pathlib import Path
from typing import Any

import pytest

import gobby.tasks.land_commit as landing
from gobby.mcp_proxy.tools.tasks import create_task_registry
from gobby.storage.definitions.agents import AgentDefinitionManager
from gobby.storage.definitions.rules import RuleDefinitionManager
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.tasks import LocalTaskManager
from gobby.tasks.attestations import AttestationSubmission, record_attestation
from gobby.tasks.close_receipts import list_close_receipts
from gobby.tasks.closure_snapshot import PreparedClosure, prepare_closure
from gobby.tasks.land_commit import land_candidate
from gobby.utils.daemon_git import GitFailed, GitOk
from gobby.utils.session_context import session_context_for_test
from tests.fixtures.agent_sessions import AgentSessions
from tests.fixtures.landing_case import make_landing_case

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.mark.parametrize("mutation", ["unlink", "claim", "link-protected"])
async def test_landing_checks_links_and_scope_from_the_admitted_task(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    case = make_landing_case(temp_db, sample_project, tmp_path)
    seats = AgentSessions(temp_db, case.project_id)
    caller = seats.add(
        "owner-delivery",
        capabilities=("land_tasks",),
        definition_fields={"task_land_scope": {"own_claim_only": True}},
    )
    successor = seats.add("new-owner")
    protected = case.candidate("protected", {"src/auth.py": "protected = True\n"})
    sha = case.candidate("candidate", {"chapter.txt": "Final manuscript.\n"}, base=protected)
    task = case.task("Deliver manuscript")
    manager = LocalTaskManager(temp_db)
    manager.claim_task(task.id, caller, force=True)
    case.link(task, sha)
    original = prepare_closure

    def change_before_admission(*args: Any, **kwargs: Any) -> PreparedClosure:
        if mutation == "unlink":
            links = manager.get_task(task.id).commits
            assert links
            manager.unlink_commit(task.id, links[0])
        elif mutation == "claim":
            manager.claim_task(task.id, successor, force=True)
        else:
            temp_db.execute(
                "UPDATE tasks SET commits = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                (json.dumps([protected, sha]), task.id),
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(landing, "prepare_closure", change_before_admission)
    result = await land_candidate(temp_db, task=task, caller_session_id=caller, commit_sha=sha)
    assert result.get("landed") is False, result
    assert case.git("rev-parse", "trunk") == case.base


async def test_path_grant_comes_from_the_admitted_definition(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_landing_case(temp_db, sample_project, tmp_path)
    caller = AgentSessions(temp_db, case.project_id).add(
        "changing-grant",
        capabilities=("land_tasks", "direct_integration_commit"),
        definition_fields={
            "direct_integration_commit_paths": ["**"],
            "workflows": {"rule_selectors": {"include": ["tag:delivery"]}},
        },
    )
    RuleDefinitionManager(temp_db).create(
        "require-declared-paths",
        {
            "event": "task_land",
            "when": "facts.get('integration_path_grant') != True",
            "effects": [{"type": "block", "reason": "Path grant required."}],
        },
        project_id=case.project_id,
        tags=["delivery"],
    )
    sha = case.candidate("candidate", {"chapter.txt": "Final manuscript.\n"})
    task = case.task("Deliver manuscript")
    case.link(task, sha)
    definitions = AgentDefinitionManager(temp_db)
    definition = definitions.get_by_name("changing-grant", case.project_id)
    assert definition is not None
    original = prepare_closure

    def narrow_before_admission(*args: Any, **kwargs: Any) -> PreparedClosure:
        definitions.update(
            definition.id,
            definition_json={
                **definition.definition_json,
                "direct_integration_commit_paths": ["docs/**"],
            },
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(landing, "prepare_closure", narrow_before_admission)
    result = await land_candidate(temp_db, task=task, caller_session_id=caller, commit_sha=sha)
    assert result.get("blockers") == ["Path grant required."]
    assert case.git("rev-parse", "trunk") == case.base


async def test_landing_receipt_records_the_request_admission_snapshot(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = make_landing_case(temp_db, sample_project, tmp_path)
    caller = AgentSessions(temp_db, case.project_id).add(
        "delivery-author",
        ["permission"],
        results=["granted", "rejected"],
        capabilities=("land_tasks",),
        definition_fields={"workflows": {"rule_selectors": {"include": ["tag:delivery"]}}},
    )
    RuleDefinitionManager(temp_db).create(
        "required-delivery-permission",
        {
            "event": "task_land",
            "when": "latest_attestation('permission').get('result') != 'granted'",
            "effects": [{"type": "block", "reason": "Permission required."}],
        },
        project_id=case.project_id,
        tags=["delivery"],
    )
    sha = case.candidate("candidate", {"chapter.txt": "Final manuscript.\n"})
    task = case.task("Deliver manuscript")
    case.link(task, sha)
    approval, _ = record_attestation(
        temp_db,
        task_id=task.id,
        session_id=caller,
        submission=AttestationSubmission(kind="permission", result="granted", summary="Approved."),
    )
    original_prepare, original_merge = prepare_closure, landing._merge_tree
    admitted: list[PreparedClosure] = []

    def capture(*args: Any, **kwargs: Any) -> PreparedClosure:
        prepared = original_prepare(*args, **kwargs)
        admitted.append(prepared)
        return prepared

    async def correct_after_admission(main: Path, tip: str, candidate: str) -> GitOk | GitFailed:
        record_attestation(
            temp_db,
            task_id=task.id,
            session_id=caller,
            submission=AttestationSubmission(
                kind="permission",
                result="rejected",
                summary="A later correction affects subsequent requests.",
                supersedes_id=approval.id,
            ),
        )
        return await original_merge(main, tip, candidate)

    monkeypatch.setattr(landing, "prepare_closure", capture)
    monkeypatch.setattr(landing, "_merge_tree", correct_after_admission)
    result = await land_candidate(temp_db, task=task, caller_session_id=caller, commit_sha=sha)
    assert result.get("landed") is True, result
    receipt = list_close_receipts(temp_db, task.id)[0]
    assert receipt.facts["admission_basis"] == "request_snapshot"
    assert receipt.facts["admission_policy_revision"] == admitted[0].decision.policy_revision
    assert receipt.facts["admission_snapshot_revision"] == admitted[0].decision.snapshot_revision
    replay = await land_candidate(temp_db, task=task, caller_session_id=caller, commit_sha=sha)
    assert replay.get("blockers") == ["Permission required."]


async def test_authorized_cross_project_delivery_uses_the_same_policy_boundary(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    case = make_landing_case(temp_db, sample_project, tmp_path)
    coordinating_project = LocalProjectManager(temp_db).create("delivery-coordination")
    caller = AgentSessions(temp_db, coordinating_project.id).add(
        "foreign-delivery-agent", capabilities=("land_tasks", "coordinate_tasks")
    )
    sha = case.candidate("candidate", {"chapter.txt": "Final manuscript.\n"})
    task = case.task("Deliver manuscript")
    case.link(task, sha)

    result = await land_candidate(temp_db, task=task, caller_session_id=caller, commit_sha=sha)

    assert result.get("landed") is True, result
    assert case.git("rev-parse", "trunk") == sha


async def test_linking_a_commit_does_not_infer_a_coding_workflow(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    case = make_landing_case(temp_db, sample_project, tmp_path)
    caller = AgentSessions(temp_db, case.project_id).add("manuscript-author")
    sha = case.candidate("candidate", {"chapter.txt": "Final manuscript.\n"})
    task = case.task("Deliver manuscript")
    manager = LocalTaskManager(temp_db)
    manager.claim_task(task.id, caller, force=True)
    with session_context_for_test(caller):
        linked = await create_task_registry(manager).call(
            "link_commit", {"task_id": task.id, "commit_sha": sha, "candidate": True}
        )
    assert "error" not in linked, linked
    assert sha.startswith(linked["declared_candidate"])
    assert temp_db.fetchone("SELECT id FROM task_comments WHERE task_id = %s", (task.id,)) is None


async def test_landing_has_no_implicit_implementation_or_review_requirements(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    case = make_landing_case(temp_db, sample_project, tmp_path)
    caller = AgentSessions(temp_db, case.project_id).add(
        "self-delivery", capabilities=("land_tasks",)
    )
    sha = case.candidate("candidate", {"chapter.txt": "Final manuscript.\n"})
    task = case.task("Deliver manuscript")
    case.link(task, sha)

    result = await land_candidate(temp_db, task=task, caller_session_id=caller, commit_sha=sha)

    assert result.get("landed") is True, result
    assert case.git("rev-parse", "trunk") == sha


async def test_landing_obeys_selected_evidence_policy(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    case = make_landing_case(temp_db, sample_project, tmp_path)
    caller = AgentSessions(temp_db, case.project_id).add(
        "delivery-author",
        ["release-permission"],
        results=["granted", "rejected"],
        capabilities=("land_tasks",),
        definition_fields={"workflows": {"rule_selectors": {"include": ["tag:delivery"]}}},
    )
    RuleDefinitionManager(temp_db).create(
        "arbitrary-delivery-check",
        {
            "event": "task_land",
            "when": "latest_attestation('release-permission').get('result') != 'granted'",
            "effects": [{"type": "block", "reason": "Delivery permission is required."}],
        },
        project_id=case.project_id,
        tags=["delivery"],
    )
    sha = case.candidate("candidate", {"chapter.txt": "Final manuscript.\n"})
    task = case.task("Deliver manuscript")
    case.link(task, sha)
    blocked = await land_candidate(temp_db, task=task, caller_session_id=caller, commit_sha=sha)
    assert blocked.get("blockers") == ["Delivery permission is required."]
    assert case.git("rev-parse", "trunk") == case.base

    record_attestation(
        temp_db,
        task_id=task.id,
        session_id=caller,
        submission=AttestationSubmission(
            kind="release-permission", result="granted", summary="Operator release criteria met."
        ),
    )
    landed = await land_candidate(temp_db, task=task, caller_session_id=caller, commit_sha=sha)
    assert landed.get("landed") is True, landed
    assert case.git("rev-parse", "trunk") == sha
