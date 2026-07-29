"""Tests for structured interactive plan vote artifacts."""

from __future__ import annotations

from pathlib import Path

import pytest

from gobby.mcp_proxy.tools.plans import create_plan_registry
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.plans.vote_artifacts import (
    PLAN_VOTE_INTERACTION_RECEIPT_VARIABLE,
    build_plan_vote_artifact,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager

pytestmark = pytest.mark.unit


def _payload(*finding_ids: str) -> dict[str, object]:
    return {
        "items": [
            {
                "finding_id": finding_id,
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


def _observe_interaction(
    db: HubDatabase,
    session_id: str,
    interaction_tool: str,
    *finding_ids: str,
) -> None:
    SessionVariableManager(db).set_variable(
        session_id,
        PLAN_VOTE_INTERACTION_RECEIPT_VARIABLE,
        {
            "tool": interaction_tool,
            "payload": {
                "questions": [
                    {
                        "id": finding_id,
                        "question": (f"{finding_id}: detail. Proposed edit: edit {finding_id}"),
                    }
                    for finding_id in finding_ids
                ]
            },
            "response_observed": True,
        },
    )


def test_build_artifact_records_per_item_decisions_and_proposed_text() -> None:
    artifact = build_plan_vote_artifact(
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
            "proposed_edit_text": "edit E1",
        },
        {
            "vote_id": "vote-2",
            "finding_id": "E2",
            "decision": "accept",
            "proposed_edit_text": "edit E2",
        },
    ]


def test_build_artifact_rejects_free_text_and_blanket_vote() -> None:
    with pytest.raises(ReviewEvidenceError, match="free-text presentation"):
        build_plan_vote_artifact(
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
    registry = create_plan_registry(temp_db, default_project_id=project.id)
    _observe_interaction(
        temp_db,
        session.id,
        "request_user_input",
        "F1",
        "F2",
    )

    with session_context_for_test(session.id):
        recorded = await registry.call(
            "record_plan_vote_artifact",
            {
                "plan_path": ".gobby/plans/demo.md",
                "round_kind": "adversary",
                "round_number": 2,
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
    assert listed["ok"] is True
    assert listed["count"] == 1
    assert listed["artifacts"][0] == recorded["artifact"]


@pytest.mark.asyncio
async def test_registry_replaces_same_round_artifact(
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
    registry = create_plan_registry(temp_db, default_project_id=project.id)

    with session_context_for_test(session.id):
        for decision in ("decline", "accept"):
            _observe_interaction(
                temp_db,
                session.id,
                "AskUserQuestion",
                "E1",
            )
            votes = _votes("E1")
            votes[0]["decision"] = decision
            result = await registry.call(
                "record_plan_vote_artifact",
                {
                    "plan_path": ".gobby/plans/demo.md",
                    "round_kind": "enhancement",
                    "round_number": 1,
                    "interaction_tool": "AskUserQuestion",
                    "interaction_payload": _payload("E1"),
                    "votes": votes,
                },
            )
            assert result["ok"] is True
        listed = await registry.call("list_plan_vote_artifacts", {})

    assert listed["count"] == 1
    assert listed["artifacts"][0]["votes"][0]["decision"] == "accept"


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
    registry = create_plan_registry(temp_db, default_project_id=project.id)
    arguments = {
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
            "request_user_input",
            "F2",
        )
        mismatched = await registry.call("record_plan_vote_artifact", arguments)

    assert unobserved["error"] == "plan_vote_interaction_not_observed"
    assert mismatched["error"] == "plan_vote_interaction_payload_mismatch"
