"""Shared doubles for tests that drive a real ``AskService``.

An Ask acceptance test has to exercise the real storage, stage store and
lifecycle; only the native agent boundary is stubbed. These helpers supply that
boundary so each test file can build the service without restating it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml

from gobby.ask.contracts import ProfileSnapshot
from gobby.ask.pipeline import parse_ask_pipeline
from gobby.ask.service import AskService
from gobby.ask.stages import AskStageStore
from gobby.ask.storage import AskRunStorage
from gobby.events.completion_registry import CompletionEventRegistry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.workflows.pipeline_models import PipelineDefinition
from gobby.workflows.pipeline_state import ExecutionStatus, PipelineExecution

ASK_PIPELINE_PATH = (
    Path(__file__).parents[2] / "src/gobby/install/shared/workflows/pipelines/ask.yaml"
)


def profile_snapshot(identifier: str, _timeout: float) -> ProfileSnapshot:
    """Resolve an agent profile without reading the profile registry."""
    return ProfileSnapshot(
        identifier=identifier,
        definition_id=f"definition-{identifier}",
        definition_updated_at="2026-09-09T12:00:00+00:00",
        effective={"name": identifier, "provider": "claude", "model": "claude-test"},
    )


def ask_pipeline_snapshot() -> dict[str, Any]:
    """Return the bundled Ask pipeline as storage records it."""
    return parse_ask_pipeline(yaml.safe_load(ASK_PIPELINE_PATH.read_text())).model_dump(mode="json")


def fixed_commit_resolver(_root: Path, _ref: str, _timeout: float) -> tuple[str, str]:
    """Bind every run to one commit so identity assertions stay stable."""
    return ("a" * 40, "b" * 40)


class NoopAskPermissions:
    """Permission runtime that grants nothing and revokes nothing."""

    def revoke_for_run(self, ask_run_id: str, *, reason: str) -> int:
        del ask_run_id, reason
        return 0


class NoopAskAgents:
    """Native agent boundary that never spawns a terminal."""

    def preflight(self, _profiles: object) -> None:
        return None

    def status(self, agent_run_id: str) -> str | None:
        del agent_run_id
        return None

    async def cancel(self, agent_run_id: str) -> None:
        del agent_run_id


class CompletingPipelineExecutor:
    """Pipeline executor that completes its execution instead of spawning agents.

    Completion is gated so a test can hold a run in flight, observe a waiter, and
    then release it — which is what proves the wait is event-driven rather than
    polled.
    """

    def __init__(self, manager: LocalPipelineExecutionManager) -> None:
        self.manager = manager
        self.calls: list[tuple[str | None, str | None, dict[str, Any]]] = []
        self.running = asyncio.Event()
        self._release: asyncio.Event | None = None
        self.fail_next = False

    def gate(self, release: asyncio.Event) -> None:
        """Hold execution until ``release`` is set."""
        self._release = release

    async def execute(
        self,
        pipeline: PipelineDefinition,
        inputs: dict[str, Any],
        project_id: str,
        execution_id: str | None = None,
        session_id: str | None = None,
    ) -> PipelineExecution:
        del pipeline
        self.calls.append((execution_id, session_id, inputs))
        assert execution_id is not None
        # PipelineExecutor._execute marks the execution RUNNING before it does any
        # step work, so a caller that reads the run between start and completion
        # sees the same status it would in production.
        self.manager.update_execution_status(execution_id, ExecutionStatus.RUNNING)
        self.running.set()
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("controlled executor interruption")
        if self._release is not None:
            await self._release.wait()
        return complete_publication(self.manager, execution_id, project_id)


def complete_publication(
    manager: LocalPipelineExecutionManager, execution_id: str, project_id: str
) -> PipelineExecution:
    from gobby.ask.artifacts import AskArtifactStore
    from gobby.ask.publication import publish_answer
    from gobby.ask.stages import AskStage
    from gobby.ask.validation import validate_claims, validate_review
    from tests.ask.test_validation import _valid_case

    draft, evidence, blobs, review = _valid_case(run_id=execution_id, project_id=project_id)
    deterministic = validate_claims(draft, evidence, pinned_blobs=blobs)
    reviewed = validate_review(draft, evidence, deterministic, review)
    publication = publish_answer(
        AskArtifactStore(None, project_id, execution_id, db=manager.db),
        draft,
        evidence,
        deterministic,
        reviewed,
        request={"question": draft.question},
        binding=evidence.repository_binding.model_dump(mode="json"),
        profiles={"investigator": "test-investigator", "reviewer": "test-reviewer"},
        tool_identities=("gcode@test",),
        attempt_history=({"attempt": 0, "status": "reviewed"},),
    )
    AskStageStore(manager).checkpoint(
        execution_id,
        stage=AskStage.PUBLISH,
        boundary_id=f"publish:{publication.manifest_sha256}",
        status=ExecutionStatus.COMPLETED.value,
        answer_outcome=publication.outcome,
        publication={
            "artifact": publication.artifact,
            "manifest_sha256": publication.manifest_sha256,
        },
    )
    execution = manager.update_execution_status(execution_id, ExecutionStatus.COMPLETED)
    assert execution is not None
    return execution


class RecordingCompletionRegistry(CompletionEventRegistry):
    """Completion registry that records which ids a caller parked on.

    An event-driven wait blocks here; a polling one never arrives, so ``awaited``
    is what separates the two.
    """

    def __init__(self) -> None:
        super().__init__()
        self.awaited: list[str] = []

    async def wait(self, completion_id: str, timeout: float | None = None) -> dict[str, Any]:
        self.awaited.append(completion_id)
        return await super().wait(completion_id, timeout)


@dataclass
class AskHarness:
    """A real ``AskService`` with the seams a test needs to drive it."""

    service: AskService
    executor: CompletingPipelineExecutor
    storage: AskRunStorage
    stages: AskStageStore
    completions: RecordingCompletionRegistry


def build_ask_service(db: HubDatabase, *, project_id: str, state_root: Path) -> AskHarness:
    """Build an ``AskService`` on real storage, stubbed only at the native boundary.

    The snapshot manager is never reached: the pipeline executor completes the
    execution instead of running the stages that would use it.
    """
    manager = LocalPipelineExecutionManager(db, project_id=project_id)
    storage = AskRunStorage(
        manager,
        profile_resolver=profile_snapshot,
        commit_resolver=fixed_commit_resolver,
        pipeline_snapshot=ask_pipeline_snapshot(),
    )
    stages = AskStageStore(manager)
    executor = CompletingPipelineExecutor(manager)
    completions = RecordingCompletionRegistry()
    service = AskService(
        storage=storage,
        stages=stages,
        snapshot_manager=cast(Any, object()),
        agents=cast(Any, NoopAskAgents()),
        permissions=cast(Any, NoopAskPermissions()),
        pipeline_executor=cast(Any, executor),
        state_root=state_root,
        completion_registry=completions,
    )
    return AskHarness(
        service=service,
        executor=executor,
        storage=storage,
        stages=stages,
        completions=completions,
    )
