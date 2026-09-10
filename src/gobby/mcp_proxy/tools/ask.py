"""MCP adapters for the native Ask lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from gobby.ask.contracts import AskRequest, RetrievalMode
from gobby.ask.publication import replay_publication
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.utils.session_context import get_current_session_id

_INVESTIGATOR_PROFILE = "ask-investigator"
_REVIEWER_PROFILE = "ask-reviewer"


def _payload(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        return dict(result)
    return dict(result.model_dump(mode="json"))


def _caller_session_id() -> str:
    session_id = get_current_session_id()
    if session_id is None:
        raise PermissionError("Ask lifecycle mutation requires an authenticated session")
    return session_id


def _retrieval_mode(value: str) -> RetrievalMode:
    if value == "hybrid":
        return RetrievalMode.AUDITED_HYBRID
    return RetrievalMode(value)


def create_ask_registry(
    service_resolver: Callable[[], Any | None],
    *,
    project_id: str,
    project_root_resolver: Callable[[str], Path],
) -> InternalToolRegistry:
    """Create public, pipeline-stage, and agent-facing Ask tools."""
    registry = InternalToolRegistry(
        name="gobby-ask",
        description="Native, source-bound Ask runs and immutable answer artifacts",
    )

    def service() -> Any:
        resolved = service_resolver()
        if resolved is None:
            raise RuntimeError("Ask service is unavailable")
        return resolved

    @registry.tool(description="Start a durable Ask run for the current project.")
    async def start_ask_run(
        question: str,
        commit_ref: str = "HEAD",
        timeout_seconds: float = 600.0,
        retrieval_mode: Literal["deterministic", "hybrid"] = "deterministic",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        request = AskRequest(
            question=question,
            project_id=project_id,
            commit_ref=commit_ref,
            timeout_seconds=timeout_seconds,
            retrieval_mode=_retrieval_mode(retrieval_mode),
            investigator_profile=_INVESTIGATOR_PROFILE,
            reviewer_profile=_REVIEWER_PROFILE,
            idempotency_key=idempotency_key,
        )
        project_root = await asyncio.to_thread(project_root_resolver, project_id)
        result = await service().start(
            request,
            project_root=project_root,
            caller_session_id=_caller_session_id(),
        )
        return _payload(result)

    @registry.tool(description="Read one durable Ask run from the current project.")
    async def get_ask_run(run_id: str) -> dict[str, Any]:
        return _payload(service().get(run_id, project_id=project_id))

    @registry.tool(description="Wait on Ask completion events, then return durable state.")
    async def wait_for_ask_run(run_id: str, timeout_seconds: float | None = None) -> dict[str, Any]:
        result = await service().wait(
            run_id,
            project_id=project_id,
            timeout=timeout_seconds,
        )
        return _payload(result)

    @registry.tool(description="Resume an interrupted Ask run without resetting its deadline.")
    async def resume_ask_run(run_id: str) -> dict[str, Any]:
        result = await service().resume(
            run_id,
            project_id=project_id,
            caller_session_id=_caller_session_id(),
        )
        return _payload(result)

    @registry.tool(description="Cancel an Ask run and its active native child.")
    async def cancel_ask_run(run_id: str) -> dict[str, Any]:
        result = await service().cancel(
            run_id,
            project_id=project_id,
            caller_session_id=_caller_session_id(),
        )
        return _payload(result)

    @registry.tool(
        description="Resolve the verified immutable publication download for an Ask run."
    )
    async def export_ask_run(run_id: str) -> dict[str, Any]:
        run = _payload(service().get(run_id, project_id=project_id))
        root = await asyncio.to_thread(
            service().publication_root,
            run_id,
            project_id=project_id,
        )
        replay = await asyncio.to_thread(replay_publication, root)
        return {
            **run,
            "publication_manifest_sha256": replay.manifest_sha256,
            "download_url": f"/api/ask/runs/{run_id}/export?project_id={project_id}",
        }

    @registry.tool(description="Prepare the immutable source snapshot for an owning Ask pipeline.")
    async def prepare(run_id: str, project_id: str) -> dict[str, Any]:
        root = await asyncio.to_thread(project_root_resolver, project_id)
        return _payload(
            await service().prepare(run_id=run_id, project_id=project_id, project_root=root)
        )

    @registry.tool(description="Seed deterministic evidence for an owning Ask pipeline.")
    async def seed(run_id: str, project_id: str) -> dict[str, Any]:
        return _payload(await service().seed(run_id=run_id, project_id=project_id))

    @registry.tool(description="Spawn the authorized native agent for one Ask stage attempt.")
    async def spawn(run_id: str, project_id: str, stage: str, attempt: int) -> dict[str, Any]:
        from gobby.ask.permissions import AskAgentStage

        return _payload(
            await service().spawn(
                run_id=run_id,
                project_id=project_id,
                stage=AskAgentStage(stage),
                attempt=attempt,
                caller_session_id=_caller_session_id(),
            )
        )

    @registry.tool(description="Validate one Ask agent submission at its durable attempt boundary.")
    async def validate(run_id: str, project_id: str, stage: str, attempt: int) -> dict[str, Any]:
        from gobby.ask.permissions import AskAgentStage

        return _payload(
            await service().validate(
                run_id=run_id,
                project_id=project_id,
                stage=AskAgentStage(stage),
                attempt=attempt,
            )
        )

    @registry.tool(description="Admit the single bounded Ask repair attempt when eligible.")
    async def admit_repair(run_id: str, project_id: str) -> dict[str, Any]:
        return _payload(await service().admit_repair(run_id=run_id, project_id=project_id))

    @registry.tool(description="Publish the reviewed immutable Ask answer bundle.")
    async def publish(run_id: str, project_id: str) -> dict[str, Any]:
        return _payload(await service().publish(run_id=run_id, project_id=project_id))

    @registry.tool(description="Query admitted source evidence for the active Ask investigator.")
    async def query_evidence(
        run_id: str,
        operation: str,
        selector: dict[str, Any],
        continuation: str | None = None,
    ) -> dict[str, Any]:
        return _payload(
            await service().query_evidence(
                run_id=run_id,
                operation=operation,
                selector=selector,
                continuation=continuation,
            )
        )

    @registry.tool(description="Read one admitted evidence record for the active Ask child.")
    async def read_evidence(run_id: str, evidence_id: str) -> dict[str, Any]:
        return _payload(await service().read_evidence(run_id=run_id, evidence_id=evidence_id))

    @registry.tool(description="Submit one immutable investigator answer for validation.")
    async def submit_answer(
        run_id: str,
        attempt: int,
        draft: dict[str, Any],
        draft_hash: str,
        evidence_manifest_hash: str,
    ) -> dict[str, Any]:
        return _payload(
            await service().submit_answer(
                run_id=run_id,
                attempt=attempt,
                draft=draft,
                draft_hash=draft_hash,
                evidence_manifest_hash=evidence_manifest_hash,
            )
        )

    @registry.tool(description="Submit one immutable reviewer verdict for validation.")
    async def submit_review(
        run_id: str,
        attempt: int,
        review: dict[str, Any],
        review_hash: str,
        draft_hash: str,
        evidence_manifest_hash: str,
    ) -> dict[str, Any]:
        return _payload(
            await service().submit_review(
                run_id=run_id,
                attempt=attempt,
                review=review,
                review_hash=review_hash,
                draft_hash=draft_hash,
                evidence_manifest_hash=evidence_manifest_hash,
            )
        )

    return registry
