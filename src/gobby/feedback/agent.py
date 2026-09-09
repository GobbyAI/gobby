"""Named-agent boundary for the nightly session-feedback review pass."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import jsonschema

from gobby.agents.launcher_session import get_or_create_launcher_session
from gobby.events.completion_registry import (
    CompletionEventRegistry,
    CompletionResultEvictedError,
)
from gobby.mcp_proxy.tools.spawn_agent._implementation import spawn_agent_impl
from gobby.sessions.handoff_records import get_agent_end_handoff
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.project_checkouts import require_root
from gobby.utils.machine_id import require_machine_id
from gobby.workflows.agent_resolver import resolve_agent

if TYPE_CHECKING:
    from gobby.agents.runner import AgentRunner
    from gobby.config.app import DaemonConfig
    from gobby.storage.sessions import SessionManager
    from gobby.worktrees.git import WorktreeGitManager

FEEDBACK_REVIEWER_AGENT_NAME = "feedback-reviewer"
FEEDBACK_REVIEWER_LAUNCHER_SOURCE = "feedback-review"
COMPLETION_GRACE_SECONDS = 30.0

_CLASSIFICATIONS = ("defect", "guidance-gap", "noise", "praise")

FEEDBACK_FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "clusters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "observation_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "uniqueItems": True,
                    },
                    "cited_paths": {"type": "array", "items": {"type": "string"}},
                    "theme": {"type": "string"},
                    "classification": {"type": "string", "enum": list(_CLASSIFICATIONS)},
                    "proposed_task": {
                        "type": ["object", "null"],
                        "properties": {
                            "title": {"type": "string"},
                            "description": {"type": "string"},
                            "labels": {"type": "array", "items": {"type": "string"}},
                            "priority": {"type": "integer", "minimum": 1, "maximum": 4},
                        },
                        "required": ["title", "description"],
                        "additionalProperties": False,
                    },
                    "digest_note": {"type": "string"},
                },
                "required": [
                    "observation_ids",
                    "cited_paths",
                    "theme",
                    "classification",
                    "digest_note",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clusters"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class FeedbackReviewerResult:
    """Validated proposal returned by one named reviewer agent run."""

    agent_run_id: str
    findings: dict[str, Any]


class FeedbackReviewerProtocol(Protocol):
    """The agent-review slice consumed by :class:`FeedbackReviewService`."""

    async def review(self, prompt: str, *, timeout_seconds: float) -> FeedbackReviewerResult: ...


class FeedbackReviewerError(RuntimeError):
    """Base error carrying the agent run when launch progressed that far."""

    def __init__(self, message: str, *, agent_run_id: str | None = None) -> None:
        super().__init__(message)
        self.agent_run_id = agent_run_id


class FeedbackReviewerLaunchError(FeedbackReviewerError):
    """The named reviewer could not be launched."""


class FeedbackReviewerRunError(FeedbackReviewerError):
    """The named reviewer reached a non-success terminal state."""


class FeedbackReviewerTimeoutError(FeedbackReviewerError):
    """The named reviewer exceeded its execution or completion deadline."""


class FeedbackReviewerResultError(FeedbackReviewerError):
    """The named reviewer did not return the required result contract."""


def validate_feedback_findings(
    value: object,
    *,
    agent_run_id: str | None = None,
    observation_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Validate untrusted reviewer output before deterministic actions consume it."""
    if not isinstance(value, dict):
        raise FeedbackReviewerResultError(
            f"feedback reviewer result must be an object, got {type(value).__name__}",
            agent_run_id=agent_run_id,
        )
    try:
        jsonschema.validate(value, FEEDBACK_FINDINGS_SCHEMA)
    except jsonschema.ValidationError as exc:
        location = ".".join(str(part) for part in exc.absolute_path)
        at = f" at {location}" if location else ""
        raise FeedbackReviewerResultError(
            f"feedback reviewer result failed schema validation{at}: {exc.message}",
            agent_run_id=agent_run_id,
        ) from exc
    if observation_ids is not None:
        actual = Counter(
            observation_id
            for cluster in value["clusters"]
            for observation_id in cluster["observation_ids"]
        )
        expected = set(observation_ids)
        missing = sorted(expected - actual.keys())
        unknown = sorted(actual.keys() - expected)
        repeated = sorted(key for key, count in actual.items() if count != 1)
        if missing or unknown or repeated:
            raise FeedbackReviewerResultError(
                f"feedback coverage must include each frozen observation exactly once: "
                f"missing={missing}, unknown={unknown}, repeated={repeated}",
                agent_run_id=agent_run_id,
            )
    return value


class FeedbackReviewerAgent:
    """Resolve, launch, await, and decode the configured feedback-reviewer agent."""

    def __init__(
        self,
        *,
        db: HubDatabase,
        runner: AgentRunner | None,
        session_manager: SessionManager,
        completion_registry: CompletionEventRegistry,
        project_id: str | None,
        project_path: str | None,
        git_manager: WorktreeGitManager | None,
        daemon_config: DaemonConfig,
    ) -> None:
        self.db = db
        self.runner = runner
        self.session_manager = session_manager
        self.completion_registry = completion_registry
        self.project_id = project_id
        self.project_path = project_path
        self.git_manager = git_manager
        self.daemon_config = daemon_config

    async def review(self, prompt: str, *, timeout_seconds: float) -> FeedbackReviewerResult:
        """Run the named reviewer and return its validated agent-end payload."""
        runner = self.runner
        project_id = self.project_id
        if runner is None:
            raise FeedbackReviewerLaunchError(
                "feedback reviewer launch failed: agent runner unavailable"
            )
        if project_id is None:
            raise FeedbackReviewerLaunchError(
                "feedback reviewer launch failed: project unavailable"
            )

        try:
            agent_body = await asyncio.to_thread(
                resolve_agent,
                FEEDBACK_REVIEWER_AGENT_NAME,
                self.db,
                project_id=project_id,
            )
        except Exception as exc:
            raise FeedbackReviewerLaunchError(
                f"feedback reviewer launch failed: agent resolution failed: {exc}"
            ) from exc
        if agent_body is None:
            raise FeedbackReviewerLaunchError(
                f"feedback reviewer launch failed: agent {FEEDBACK_REVIEWER_AGENT_NAME!r} not found"
            )

        project_path = self.project_path
        if project_path is None:
            try:
                machine_id = await asyncio.to_thread(require_machine_id)
                project_path = await asyncio.to_thread(
                    require_root,
                    self.db,
                    project_id,
                    machine_id,
                )
            except Exception as exc:
                raise FeedbackReviewerLaunchError(
                    f"feedback reviewer launch failed: project checkout unavailable: {exc}"
                ) from exc

        try:
            launcher_session_id = await asyncio.to_thread(
                get_or_create_launcher_session,
                self.session_manager,
                project_id,
                FEEDBACK_REVIEWER_LAUNCHER_SOURCE,
            )
        except Exception as exc:
            raise FeedbackReviewerLaunchError(
                f"feedback reviewer launch failed: launcher session unavailable: {exc}"
            ) from exc
        try:
            spawn_result = await spawn_agent_impl(
                prompt,
                runner,
                agent_body=agent_body,
                agent_lookup_name=FEEDBACK_REVIEWER_AGENT_NAME,
                isolation="none",
                git_manager=self.git_manager,
                timeout=timeout_seconds,
                parent_session_id=launcher_session_id,
                caller_session_id=launcher_session_id,
                project_path=project_path,
                target_project_id=project_id,
                session_manager=self.session_manager,
                db=self.db,
                completion_registry=self.completion_registry,
                notify_parent_on_completion=True,
                daemon_config=self.daemon_config,
            )
        except Exception as exc:
            raise FeedbackReviewerLaunchError(f"feedback reviewer launch failed: {exc}") from exc

        run_id_value = spawn_result.get("run_id")
        run_id = str(run_id_value) if run_id_value else None
        if not spawn_result.get("success") or run_id is None:
            detail = str(spawn_result.get("error") or "spawn returned no agent run")
            # The spawn boundary serializes exceptions. Recover only known
            # transport failures; invalid definitions stay explicit failures.
            cause = (
                OSError(detail)
                if any(
                    message in detail.lower()
                    for message in (
                        "temporarily unavailable",
                        "connection reset",
                        "input/output error",
                        "message too long",
                        "socket unavailable",
                    )
                )
                else None
            )
            raise FeedbackReviewerLaunchError(
                f"feedback reviewer launch failed: {detail}",
                agent_run_id=run_id,
            ) from cause

        completion_error: Exception | None = None
        try:
            await self.completion_registry.wait(
                run_id,
                timeout=timeout_seconds + COMPLETION_GRACE_SECONDS,
            )
        except TimeoutError as exc:
            completion_error = exc
        except (KeyError, CompletionResultEvictedError) as exc:
            completion_error = exc

        run = await asyncio.to_thread(runner.get_run, run_id)
        if run is None:
            detail = f": {completion_error}" if completion_error is not None else ""
            raise FeedbackReviewerRunError(
                f"feedback reviewer agent {run_id} has no durable run record{detail}",
                agent_run_id=run_id,
            )
        if run.status == "timeout" or (
            isinstance(completion_error, TimeoutError) and run.status in {"pending", "running"}
        ):
            raise FeedbackReviewerTimeoutError(
                f"feedback reviewer agent {run_id} timed out",
                agent_run_id=run_id,
            )
        if run.status != "success":
            detail = run.error or run.result or f"terminal status {run.status}"
            raise FeedbackReviewerRunError(
                f"feedback reviewer agent {run_id} failed: {detail}",
                agent_run_id=run_id,
            )

        handoff = await asyncio.to_thread(get_agent_end_handoff, self.db, run_id)
        if handoff is None:
            raise FeedbackReviewerResultError(
                f"feedback reviewer agent {run_id} returned no agent-end handoff",
                agent_run_id=run_id,
            )
        try:
            decoded: object = json.loads(handoff.payload.current_state)
        except (TypeError, json.JSONDecodeError) as exc:
            raise FeedbackReviewerResultError(
                f"feedback reviewer agent {run_id} returned invalid JSON: {exc}",
                agent_run_id=run_id,
            ) from exc
        findings = validate_feedback_findings(decoded, agent_run_id=run_id)
        return FeedbackReviewerResult(agent_run_id=run_id, findings=findings)
