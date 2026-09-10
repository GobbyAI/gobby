from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gobby.ask.contracts import AskRequest, ProfileSnapshot
from gobby.ask.storage import AskRunStorage
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager

pytestmark = pytest.mark.unit


def _profile(identifier: str, _timeout: float) -> ProfileSnapshot:
    return ProfileSnapshot(
        identifier=identifier,
        definition_id=f"definition-{identifier}",
        definition_updated_at="2026-09-09T12:00:00+00:00",
        effective={"name": identifier, "provider": "claude", "model": "claude-test"},
    )


@pytest.mark.asyncio
async def test_deadline_and_stage_boundary_recovery(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
) -> None:
    try:
        from gobby.ask.recovery import AskRecoveryController
        from gobby.ask.stages import AskErrorCode, AskStage, AskStageStore
    except ModuleNotFoundError:
        raise NotImplementedError("native Ask recovery is not implemented") from None

    project_id = str(sample_project["id"])
    admitted_at = datetime.now(UTC)
    manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
    storage = AskRunStorage(
        manager,
        profile_resolver=_profile,
        commit_resolver=lambda _root, _ref, _timeout: ("a" * 40, "b" * 40),
        now=lambda: admitted_at,
    )
    record = storage.start(
        AskRequest(
            question="Can recovery duplicate an Ask attempt?",
            project_id=project_id,
            investigator_profile="ask-investigator",
            reviewer_profile="ask-reviewer",
            timeout_seconds=120,
        ),
        tmp_path,
    )
    stages = AskStageStore(manager)
    state = stages.initialize(record)
    original_deadline = state.deadline_at
    attempt = stages.reserve_attempt(
        record.run_id,
        stage=AskStage.INVESTIGATOR,
        attempt=0,
        boundary_id="investigator:0:reserved",
    )
    bound = stages.bind_agent(
        record.run_id,
        stage=AskStage.INVESTIGATOR,
        attempt=0,
        agent_run_id="agent-original",
        boundary_id="investigator:0:launched",
    )

    recovered = AskStageStore(manager).get(record.run_id)
    assert recovered is not None
    assert recovered.deadline_at == original_deadline == admitted_at + timedelta(seconds=120)
    assert recovered.attempts == (bound,)
    assert bound.status.value == "running"
    assert bound.agent_run_id == "agent-original"
    assert bound.attempt == attempt.attempt
    duplicate = stages.reserve_attempt(
        record.run_id,
        stage=AskStage.INVESTIGATOR,
        attempt=0,
        boundary_id="investigator:0:reserved",
    )
    assert duplicate.agent_run_id == "agent-original"
    durable = stages.get(record.run_id)
    assert durable is not None
    assert len(durable.attempts) == 1

    events: list[str] = []

    class Permissions:
        def revoke_for_run(self, ask_run_id: str, *, reason: str) -> int:
            events.append(f"revoke:{ask_run_id}:{reason}")
            return 1

    class Agents:
        def status(self, agent_run_id: str) -> str | None:
            assert agent_run_id == "agent-original"
            return "running"

        async def cancel(self, agent_run_id: str) -> None:
            events.append(f"cleanup:{agent_run_id}")

    controller = AskRecoveryController(
        manager=manager,
        stages=stages,
        permissions=Permissions(),
        agents=Agents(),
    )
    decision = controller.inspect(record.run_id, project_id=project_id)
    assert decision.reattach_agent_run_id == "agent-original"
    assert decision.restart_attempt is None

    waiter = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert controller.inspect(record.run_id, project_id=project_id).reattach_agent_run_id == (
        "agent-original"
    )

    cancelled = await controller.cancel(
        record.run_id,
        project_id=project_id,
        caller_session_id="caller-session",
    )
    assert events == [
        f"revoke:{record.run_id}:cancelled",
        "cleanup:agent-original",
    ]
    assert cancelled.status == "cancelled"

    expired_record = storage.start(
        AskRequest(
            question="Is the deadline reset?",
            project_id=project_id,
            investigator_profile="ask-investigator",
            reviewer_profile="ask-reviewer",
            timeout_seconds=1,
        ),
        tmp_path,
    )
    stages.initialize(expired_record)
    with pytest.raises(TimeoutError, match=AskErrorCode.DEADLINE_EXCEEDED.value):
        controller.inspect(
            expired_record.run_id,
            project_id=project_id,
            now=expired_record.binding.deadline_at,
        )


def test_identical_stage_submission_replay_is_idempotent(
    temp_db: HubDatabase,
    sample_project: dict[str, object],
    tmp_path: Path,
) -> None:
    from gobby.ask.permissions import AskAgentStage
    from gobby.ask.stages import AskStageStore

    project_id = str(sample_project["id"])
    manager = LocalPipelineExecutionManager(temp_db, project_id=project_id)
    storage = AskRunStorage(
        manager,
        profile_resolver=_profile,
        commit_resolver=lambda _root, _ref, _timeout: ("a" * 40, "b" * 40),
    )
    record = storage.start(
        AskRequest(
            question="Can a completed submission be replayed safely?",
            project_id=project_id,
            investigator_profile="ask-investigator",
            reviewer_profile="ask-reviewer",
        ),
        tmp_path,
    )
    stages = AskStageStore(manager)
    stages.initialize(record)
    stages.reserve_attempt(
        record.run_id,
        stage=AskAgentStage.INVESTIGATOR,
        attempt=0,
        boundary_id="investigator:0:reserved",
    )
    stages.bind_agent(
        record.run_id,
        stage=AskAgentStage.INVESTIGATOR,
        attempt=0,
        agent_run_id="agent-original",
        boundary_id="investigator:0:launched",
    )
    arguments = {
        "stage": AskAgentStage.INVESTIGATOR,
        "attempt": 0,
        "agent_run_id": "agent-original",
        "artifact": {"kind": "answer-draft", "sha256": "a" * 64},
        "submission_hash": "b" * 64,
        "evidence_manifest_hash": "c" * 64,
        "boundary_id": "investigator:0:answer:" + "b" * 64,
    }

    first = stages.record_submission(record.run_id, **arguments)
    replay = stages.record_submission(record.run_id, **arguments)

    assert replay == first
    state = stages.get(record.run_id)
    assert state is not None
    assert state.attempts == (first,)
