"""Tests for structured interactive plan vote artifacts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gobby.hooks.events import HookEvent, HookEventType, SessionSource
from gobby.mcp_proxy.tools.plans import create_plan_registry
from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_io import ensure_checkpoint
from gobby.plans.review_evidence_models import PreparedReviewEvidence, ReviewEvidenceError
from gobby.plans.review_requirements import REQUEST_ANCHOR_VARIABLE, build_request_anchor
from gobby.plans.vote_artifacts import (
    PLAN_VOTE_INTERACTION_CONTEXT_VARIABLE,
    PLAN_VOTE_INTERACTION_RECEIPT_VARIABLE,
    build_plan_vote_artifact,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import (
    reset_current_agent_run_id,
    session_context_for_test,
    set_current_agent_run_id,
)
from gobby.workflows.observer_mcp import detect_mcp_call
from gobby.workflows.reserved_variables import is_reserved_workflow_variable
from gobby.workflows.state_manager import SessionVariableManager
from tests.review_coverage_helpers import coverage_attestation
from tests.review_telemetry_helpers import enriched_telemetry

pytestmark = pytest.mark.unit


def _payload(*finding_ids: str) -> dict[str, object]:
    return {
        "items": [
            {
                "finding_id": finding_id,
                "target_section_id": "1.1",
                "full_item_text": f"{finding_id}: detail. Proposed edit: edit {finding_id}",
                "proposed_edit_text": f"edit {finding_id}",
            }
            for finding_id in finding_ids
        ]
    }


def _votes(*finding_ids: str) -> list[dict[str, object]]:
    return [
        {
            "vote_id": f"vote-{index}",
            "finding_id": finding_id,
            "decision": "accept",
        }
        for index, finding_id in enumerate(finding_ids, start=1)
    ]


def _canonical_digest(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def _observe_interaction(
    db: HubDatabase,
    session_id: str,
    evidence: PreparedReviewEvidence,
    round_kind: str,
    interaction_tool: str,
    *finding_ids: str,
    decisions: dict[str, str] | None = None,
    payload: dict[str, object] | None = None,
) -> None:
    tool_output: dict[str, object] = {
        "answers": decisions or dict.fromkeys(finding_ids, "accept")
    }
    canonical_output = json.loads(
        json.dumps(tool_output, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    )
    output_digest = _canonical_digest(canonical_output)
    SessionVariableManager(db).set_variable(
        session_id,
        PLAN_VOTE_INTERACTION_RECEIPT_VARIABLE,
        {
            "evidence_id": evidence.evidence_id,
            "round_number": 1,
            "round_kind": round_kind,
            "content_sha256": evidence.plan_hash,
            "captured_by": session_id,
            "tool": interaction_tool,
            "tool_input": payload or _payload(*finding_ids),
            "tool_output": canonical_output,
            "tool_output_sha256": output_digest,
            "provenance": "observer-captured",
        },
    )


def _prepare_evidence(
    db: HubDatabase,
    project_id: str,
    session_id: str,
    root: Path,
) -> tuple[PlanReviewEvidenceService, PreparedReviewEvidence, Path]:
    plan_dir = root / ".gobby" / "plans"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_path = plan_dir / "demo.md"
    plan_path.write_text(
        "\n".join(
            [
                "# Vote Artifact Plan",
                "**Plan ID:** vote-artifact",
                "",
                "## P1 Phase",
                "`kind: framing`",
                "",
                "### 1.1 Work",
                "`kind: deliverable`",
                "",
                "Target: `src/example.py`",
                "",
                "**Acceptance:**",
                "- 1.1.1 — Behavior exists. test: `tests/test_example.py`",
                "",
                "## Task Mapping",
                "`kind: framing`",
                "",
                "Pending.",
                "",
                "## V1 Plan Changelog",
                "`kind: verification`",
                "",
                "No rounds yet.",
                "",
                "## M1 Task Manifest",
                "`kind: manifest`",
                "",
                "```yaml",
                "- title: Implement example",
                "  source_section: '1.1'",
                "  covers:",
                "    - 1.1.1",
                "  category: code",
                "  implementation_domain: backend",
                "  priority: 2",
                "  task_type: feature",
                "  tdd: false",
                "  labels:",
                "    - covers:vote-artifact:1.1:1.1.1",
                "  description: Implement the example.",
                "  validation_criteria: Example behavior is tested.",
                "```",
                "",
            ]
        ),
        encoding="utf-8",
    )
    SessionVariableManager(db).merge_variables(
        session_id,
        {
            REQUEST_ANCHOR_VARIABLE: build_request_anchor(
                "vote-artifact-request",
                "Review the vote artifact plan",
            )
        },
    )
    service = PlanReviewEvidenceService(db)
    prepared = service.prepare_plan_review_round(
        project_id=project_id,
        plan_path=plan_path,
        round_number=1,
        session_id=session_id,
    )
    return service, prepared, plan_path


def test_build_artifact_records_per_item_decisions_and_proposed_text() -> None:
    artifact = build_plan_vote_artifact(
        evidence_id="evidence-1",
        project_id="project-1",
        session_id="session-1",
        plan_path=".gobby/plans/demo.md",
        round_kind="enhancement",
        round_number=1,
        interaction_tool="request_user_input",
        interaction_payload=_payload("E1", "E2"),
        votes=_votes("E1", "E2"),
    )

    assert len(str(artifact["artifact_id"])) == 64
    assert artifact["votes"] == [
        {
            "vote_id": "vote-1",
            "finding_id": "E1",
            "decision": "accept",
            "target_section_id": "1.1",
            "proposed_edit_text": "edit E1",
        },
        {
            "vote_id": "vote-2",
            "finding_id": "E2",
            "decision": "accept",
            "target_section_id": "1.1",
            "proposed_edit_text": "edit E2",
        },
    ]


def test_build_artifact_rejects_free_text_and_blanket_vote() -> None:
    with pytest.raises(ReviewEvidenceError, match="free-text presentation"):
        build_plan_vote_artifact(
            evidence_id="evidence-1",
            project_id="project-1",
            session_id="session-1",
            plan_path=".gobby/plans/demo.md",
            round_kind="adversary",
            round_number=1,
            interaction_tool="free_text",
            interaction_payload=_payload("F1"),
            votes=_votes("F1"),
        )

    with pytest.raises(ReviewEvidenceError, match="blanket decisions are invalid"):
        build_plan_vote_artifact(
            evidence_id="evidence-1",
            project_id="project-1",
            session_id="session-1",
            plan_path=".gobby/plans/demo.md",
            round_kind="adversary",
            round_number=1,
            interaction_tool="AskUserQuestion",
            interaction_payload=_payload("F1", "F2"),
            votes=_votes("F1"),
        )


def test_build_artifact_requires_proposed_text_inside_payload() -> None:
    payload = _payload("F1")
    items = payload["items"]
    assert isinstance(items, list)
    item = items[0]
    assert isinstance(item, dict)
    item["full_item_text"] = "Finding without its proposed wording"

    with pytest.raises(ReviewEvidenceError, match="must contain proposed_edit_text"):
        build_plan_vote_artifact(
            evidence_id="evidence-1",
            project_id="project-1",
            session_id="session-1",
            plan_path=".gobby/plans/demo.md",
            round_kind="adversary",
            round_number=1,
            interaction_tool="AskUserQuestion",
            interaction_payload=payload,
            votes=_votes("F1"),
        )


@pytest.mark.asyncio
async def test_registry_persists_and_lists_artifact(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project = LocalProjectManager(temp_db).create(name="votes", repo_path=str(tmp_path))
    session = SessionManager(temp_db).register(
        external_id="vote-session",
        machine_id="machine",
        source="codex",
        project_id=project.id,
    )
    service, evidence, _plan_path = _prepare_evidence(
        temp_db,
        project.id,
        session.id,
        tmp_path,
    )
    registry = create_plan_registry(temp_db, default_project_id=project.id)
    _observe_interaction(
        temp_db,
        session.id,
        evidence,
        "adversary",
        "request_user_input",
        "F1",
        "F2",
    )

    with session_context_for_test(session.id):
        recorded = await registry.call(
            "record_plan_vote_artifact",
            {
                "evidence_id": evidence.evidence_id,
                "plan_path": ".gobby/plans/demo.md",
                "round_kind": "adversary",
                "round_number": 1,
                "interaction_tool": "request_user_input",
                "interaction_payload": _payload("F1", "F2"),
                "votes": _votes("F1", "F2"),
            },
        )
        listed = await registry.call(
            "list_plan_vote_artifacts",
            {"plan_path": ".gobby/plans/demo.md"},
        )

    assert recorded["ok"] is True
    stored = service.get_evidence(evidence.evidence_id)
    assert stored.vote_artifact == recorded["artifact"]
    assert stored.vote_receipt is not None
    assert (
        PLAN_VOTE_INTERACTION_RECEIPT_VARIABLE
        not in SessionVariableManager(temp_db).get_variables(session.id)
    )
    assert listed["ok"] is True
    assert listed["count"] == 1
    assert listed["artifacts"][0] == recorded["artifact"]


@pytest.mark.asyncio
async def test_registry_rejects_artifact_replay(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project = LocalProjectManager(temp_db).create(name="vote-replay", repo_path=str(tmp_path))
    session = SessionManager(temp_db).register(
        external_id="vote-replay-session",
        machine_id="machine",
        source="claude",
        project_id=project.id,
    )
    _service, evidence, _plan_path = _prepare_evidence(
        temp_db,
        project.id,
        session.id,
        tmp_path,
    )
    registry = create_plan_registry(temp_db, default_project_id=project.id)

    with session_context_for_test(session.id):
        results: list[dict[str, object]] = []
        for decision in ("decline", "accept"):
            _observe_interaction(
                temp_db,
                session.id,
                evidence,
                "enhancement",
                "AskUserQuestion",
                "E1",
                decisions={"E1": decision},
            )
            votes = _votes("E1")
            votes[0]["decision"] = decision
            result = await registry.call(
                "record_plan_vote_artifact",
                {
                    "evidence_id": evidence.evidence_id,
                    "plan_path": ".gobby/plans/demo.md",
                    "round_kind": "enhancement",
                    "round_number": 1,
                    "interaction_tool": "AskUserQuestion",
                    "interaction_payload": _payload("E1"),
                    "votes": votes,
                },
            )
            results.append(result)

    assert results[0]["ok"] is True
    assert results[1]["error"] == "plan_vote_artifact_conflict"


@pytest.mark.asyncio
async def test_registry_rejects_unobserved_or_mismatched_interaction(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project = LocalProjectManager(temp_db).create(
        name="vote-provenance",
        repo_path=str(tmp_path),
    )
    session = SessionManager(temp_db).register(
        external_id="vote-provenance-session",
        machine_id="machine",
        source="codex",
        project_id=project.id,
    )
    _service, evidence, _plan_path = _prepare_evidence(
        temp_db,
        project.id,
        session.id,
        tmp_path,
    )
    registry = create_plan_registry(temp_db, default_project_id=project.id)
    arguments = {
        "evidence_id": evidence.evidence_id,
        "plan_path": ".gobby/plans/demo.md",
        "round_kind": "adversary",
        "round_number": 1,
        "interaction_tool": "request_user_input",
        "interaction_payload": _payload("F1"),
        "votes": _votes("F1"),
    }

    with session_context_for_test(session.id):
        unobserved = await registry.call("record_plan_vote_artifact", arguments)
        _observe_interaction(
            temp_db,
            session.id,
            evidence,
            "adversary",
            "request_user_input",
            "F2",
        )
        mismatched = await registry.call("record_plan_vote_artifact", arguments)

    assert unobserved["error"] == "plan_vote_interaction_not_observed"
    assert mismatched["error"] == "plan_vote_interaction_payload_mismatch"


@pytest.mark.asyncio
async def test_vote_decisions_bind_to_observed_output(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project = LocalProjectManager(temp_db).create(name="vote-binding", repo_path=str(tmp_path))
    session = SessionManager(temp_db).register(
        external_id="vote-binding-session",
        machine_id="machine",
        source="codex",
        project_id=project.id,
    )
    _service, evidence, _plan_path = _prepare_evidence(
        temp_db,
        project.id,
        session.id,
        tmp_path,
    )
    registry = create_plan_registry(temp_db, default_project_id=project.id)
    _observe_interaction(
        temp_db,
        session.id,
        evidence,
        "adversary",
        "request_user_input",
        "F1",
        decisions={"F1": "decline"},
    )

    with session_context_for_test(session.id):
        result = await registry.call(
            "record_plan_vote_artifact",
            {
                "evidence_id": evidence.evidence_id,
                "plan_path": ".gobby/plans/demo.md",
                "round_kind": "adversary",
                "round_number": 1,
                "interaction_tool": "request_user_input",
                "interaction_payload": _payload("F1"),
                "votes": _votes("F1"),
            },
        )

    assert result["error"] == "plan_vote_decision_mismatch"


@pytest.mark.asyncio
async def test_coordinator_decision_provenance(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project = LocalProjectManager(temp_db).create(
        name="coordinator-vote",
        repo_path=str(tmp_path),
    )
    sessions = SessionManager(temp_db)
    coordinator = sessions.register(
        external_id="coordinator-session",
        machine_id="machine",
        source="codex",
        project_id=project.id,
    )
    other = sessions.register(
        external_id="other-session",
        machine_id="machine",
        source="codex",
        project_id=project.id,
    )
    service, evidence, _plan_path = _prepare_evidence(
        temp_db,
        project.id,
        coordinator.id,
        tmp_path,
    )
    registry = create_plan_registry(temp_db, default_project_id=project.id)
    arguments = {
        "evidence_id": evidence.evidence_id,
        "round_kind": "enhancement",
        "interaction_payload": _payload("E1"),
        "votes": _votes("E1"),
    }

    with session_context_for_test(other.id):
        wrong_session = await registry.call("coordinator_decision", arguments)

    token = set_current_agent_run_id("agent-run-1")
    try:
        with session_context_for_test(coordinator.id):
            agent_token = await registry.call("coordinator_decision", arguments)
    finally:
        reset_current_agent_run_id(token)

    with session_context_for_test(coordinator.id):
        recorded = await registry.call("coordinator_decision", arguments)

    stored = service.get_evidence(evidence.evidence_id)
    assert wrong_session["error"] == "plan_vote_session_mismatch"
    assert agent_token["error"] == "operator_authentication_required"
    assert recorded["ok"] is True
    assert stored.vote_artifact is not None
    assert stored.vote_artifact["provenance"] == "coordinator-authored"
    assert stored.vote_receipt is not None
    assert stored.vote_receipt["provenance"] == "coordinator-authored"
    assert stored.vote_receipt_digest is not None


@pytest.mark.asyncio
async def test_multiline_payload_verification(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project = LocalProjectManager(temp_db).create(
        name="multiline-vote",
        repo_path=str(tmp_path),
    )
    session = SessionManager(temp_db).register(
        external_id="multiline-vote-session",
        machine_id="machine",
        source="codex",
        project_id=project.id,
    )
    _service, evidence, _plan_path = _prepare_evidence(
        temp_db,
        project.id,
        session.id,
        tmp_path,
    )
    registry = create_plan_registry(temp_db, default_project_id=project.id)
    proposed = 'Replace:\n    old = "quoted"\nwith:\n    new = "also quoted"'
    payload: dict[str, object] = {
        "items": [
            {
                "finding_id": "F1",
                "target_section_id": "1.1",
                "full_item_text": f"F1 requires this edit:\n{proposed}",
                "proposed_edit_text": proposed,
            }
        ]
    }
    _observe_interaction(
        temp_db,
        session.id,
        evidence,
        "adversary",
        "request_user_input",
        "F1",
        payload=payload,
    )

    with session_context_for_test(session.id):
        result = await registry.call(
            "record_plan_vote_artifact",
            {
                "evidence_id": evidence.evidence_id,
                "plan_path": ".gobby/plans/demo.md",
                "round_kind": "adversary",
                "round_number": 1,
                "interaction_tool": "request_user_input",
                "interaction_payload": payload,
                "votes": _votes("F1"),
            },
        )

    assert result["ok"] is True


def test_observer_receipt_captures_inline_output_and_digest() -> None:
    variables: dict[str, object] = {
        "_plan_vote_interaction_context": {
            "evidence_id": "evidence-1",
            "round_number": 2,
            "round_kind": "adversary",
            "content_sha256": "a" * 64,
        }
    }
    output: dict[str, object] = {"answers": {"F1": "accept"}}
    event = HookEvent(
        event_type=HookEventType.AFTER_TOOL,
        source=SessionSource.CLAUDE,
        session_id="agent-session",
        timestamp=datetime.now(UTC),
        data={
            "tool_name": "request_user_input",
            "tool_input": _payload("F1"),
            "tool_output": output,
        },
    )

    detect_mcp_call(event, variables, "caller-session")

    receipt = variables[PLAN_VOTE_INTERACTION_RECEIPT_VARIABLE]
    assert isinstance(receipt, dict)
    assert receipt["evidence_id"] == "evidence-1"
    assert receipt["captured_by"] == "caller-session"
    assert receipt["tool_output"] == output
    assert receipt["tool_output_sha256"] == _canonical_digest(output)


def test_observer_receipt_variable_is_runtime_reserved() -> None:
    assert is_reserved_workflow_variable(PLAN_VOTE_INTERACTION_RECEIPT_VARIABLE)
    assert not is_reserved_workflow_variable(PLAN_VOTE_INTERACTION_CONTEXT_VARIABLE)


@pytest.mark.asyncio
async def test_fold_in_requires_section_match(
    temp_db: HubDatabase,
    tmp_path: Path,
) -> None:
    project = LocalProjectManager(temp_db).create(name="fold-in-vote", repo_path=str(tmp_path))
    session = SessionManager(temp_db).register(
        external_id="fold-in-vote-session",
        machine_id="machine",
        source="codex",
        project_id=project.id,
    )
    service, evidence, plan_path = _prepare_evidence(
        temp_db,
        project.id,
        session.id,
        tmp_path,
    )
    registry = create_plan_registry(temp_db, default_project_id=project.id)
    proposed = "Accepted edit belongs only in section 1.1."
    payload: dict[str, object] = {
        "items": [
            {
                "finding_id": "F1",
                "target_section_id": "1.1",
                "full_item_text": f"F1: Proposed edit: {proposed}",
                "proposed_edit_text": proposed,
            }
        ]
    }
    _observe_interaction(
        temp_db,
        session.id,
        evidence,
        "adversary",
        "request_user_input",
        "F1",
        payload=payload,
    )
    with session_context_for_test(session.id):
        recorded = await registry.call(
            "record_plan_vote_artifact",
            {
                "evidence_id": evidence.evidence_id,
                "plan_path": ".gobby/plans/demo.md",
                "round_kind": "adversary",
                "round_number": 1,
                "interaction_tool": "request_user_input",
                "interaction_payload": payload,
                "votes": _votes("F1"),
            },
        )
    assert recorded["ok"] is True
    plan_path.write_text(
        plan_path.read_text(encoding="utf-8").replace("Pending.", f"Pending.\n\n{proposed}"),
        encoding="utf-8",
    )
    result = {
        "verdict": "needs_review",
        "findings": [],
        "coverage_attestation": coverage_attestation(
            evidence_id=evidence.evidence_id,
            manifest_entries=[{"source_section": "1.1"}],
        ),
        "convergence_telemetry": enriched_telemetry(),
    }
    ensure_checkpoint(
        plan_path,
        service.render_v1_round_checkpoint(evidence.evidence_id, result),
    )

    with pytest.raises(ReviewEvidenceError, match="target section 1.1"):
        service.finalize_plan_review_evidence(evidence.evidence_id, result)
    assert service.get_evidence(evidence.evidence_id).finalized_at is None

    plan_text = plan_path.read_text(encoding="utf-8")
    plan_text = plan_text.replace(f"Pending.\n\n{proposed}", "Pending.")
    plan_text = plan_text.replace(
        "Target: `src/example.py`",
        "Target: `src/example.py`\n\nAccepted   edit belongs only in section 1.1.",
    )
    plan_path.write_text(plan_text, encoding="utf-8")

    finalized = service.finalize_plan_review_evidence(evidence.evidence_id, result)
    assert finalized.finalized_at is not None
