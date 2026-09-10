"""Fail-closed authorization boundary for managed native Ask agents.

Ask authority is stored under ``inputs_json.ask.runtime`` on the existing pipeline
execution. Declared pipeline step status fences the current logical stage; no
undeclared pseudo-step is created. Caller-supplied session or run arguments never
create authority.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, cast

import psycopg

from gobby.ask.errors import AskPermissionDenied, UnsupportedAskRuntime
from gobby.ask.runtime_profile import AskRuntimeProfile, compile_ask_runtime_profile
from gobby.ask.runtime_validation import AskRuntimeValidation
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.session_context import get_current_agent_run_id
from gobby.workflows.pipeline_state import StepStatus

ASK_PIPELINE_NAME = "native-ask"
_AUTHORITY_KIND = "ask-agent-authority"
_AUTHORITY_VERSION = 1


class AskAgentStage(StrEnum):
    INVESTIGATOR = "investigator"
    REVIEWER = "reviewer"
    REPAIR = "repair"


_INVESTIGATOR_TOOLS = frozenset(
    {
        ("gobby-ask", "query_evidence"),
        ("gobby-ask", "read_evidence"),
        ("gobby-ask", "submit_answer"),
        ("gobby-agents", "end_agent_run"),
    }
)
_REVIEWER_TOOLS = frozenset(
    {
        ("gobby-ask", "read_evidence"),
        ("gobby-ask", "submit_review"),
        ("gobby-agents", "end_agent_run"),
    }
)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _json_object(value: object, *, name: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise AskPermissionDenied(f"invalid persisted {name}") from exc
    if not isinstance(value, Mapping):
        raise AskPermissionDenied(f"invalid persisted {name}")
    return {str(key): item for key, item in value.items()}


def _required_string(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise AskPermissionDenied(f"invalid persisted {name}")
    return value


def _required_int(value: object, *, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise AskPermissionDenied(f"{name} is missing or invalid")
    return value


def _deadline(value: object) -> datetime:
    raw = _required_string(value, name="Ask deadline")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AskPermissionDenied("invalid persisted Ask deadline") from exc
    if parsed.tzinfo is None:
        raise AskPermissionDenied("invalid persisted Ask deadline")
    return parsed.astimezone(UTC)


def _resolved_root(path: Path, *, name: str) -> Path:
    if not path.is_absolute():
        raise UnsupportedAskRuntime(f"{name} must be absolute")
    resolved = path.resolve()
    if not resolved.is_dir():
        raise UnsupportedAskRuntime(f"{name} must be an existing directory")
    return resolved


@dataclass(frozen=True, slots=True)
class AskPrincipal:
    ask_run_id: str
    project_id: str
    agent_run_id: str
    stage: AskAgentStage
    attempt: int
    generation: int
    deadline_at: datetime
    profile_snapshot_hash: str
    runtime_profile: AskRuntimeProfile
    runtime_profile_hash: str
    pipeline_status: str
    step_status: str
    active: bool
    revocation_reason: str | None

    @property
    def allowed_tools(self) -> frozenset[tuple[str, str]]:
        if self.stage is AskAgentStage.REVIEWER:
            return _REVIEWER_TOOLS
        return _INVESTIGATOR_TOOLS


class AskPermissionRuntime(Protocol):
    """Narrow authority operations required by Ask orchestration."""

    def activate(
        self,
        *,
        ask_run_id: str,
        stage: AskAgentStage,
        attempt: int,
        agent_run_id: str,
        runtime_profile: AskRuntimeProfile,
        scratch_root: Path,
    ) -> object: ...

    def replace_for_resume(
        self,
        *,
        original_agent_run_id: str,
        successor_agent_run_id: str,
    ) -> object: ...

    def authorize(
        self,
        agent_run_id: str,
        server_name: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> AskPrincipal: ...

    def revoke_for_run(self, ask_run_id: str, *, reason: str) -> int: ...


class AskPermissionStore:
    """Persist and resolve the active native principal for each Ask stage attempt."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db

    def resolve_authenticated(self, agent_run_id: str) -> AskPrincipal | None:
        """Resolve Ask authority without allowing a managed run to downgrade."""
        run = self.db.fetchone(
            "SELECT workflow_name, status FROM agent_runs WHERE id = %s",
            (agent_run_id,),
        )
        principal = self.find(agent_run_id)
        if run is None:
            if principal is None:
                return None
            raise AskPermissionDenied("Ask authority is bound to an invalid managed agent")
        is_managed_ask = run["workflow_name"] == ASK_PIPELINE_NAME
        if principal is None:
            if is_managed_ask:
                raise AskPermissionDenied("Ask managed agent authority is missing")
            return None
        if not is_managed_ask:
            raise AskPermissionDenied("Ask authority is bound to an invalid managed agent")
        if run["status"] not in {"pending", "running"}:
            raise AskPermissionDenied("Ask managed agent run is not live")
        return principal

    def activate(
        self,
        *,
        ask_run_id: str,
        stage: AskAgentStage,
        attempt: int,
        agent_run_id: str,
        runtime_profile: AskRuntimeProfile,
        scratch_root: Path,
    ) -> AskPrincipal:
        if attempt < 0:
            raise ValueError("Ask attempt must be non-negative")
        scratch = _resolved_root(scratch_root, name="Ask scratch root")
        if str(scratch) != runtime_profile.scratch_root:
            raise AskPermissionDenied("Ask scratch root does not match runtime profile")
        step_id = _authority_step_id(stage, attempt)
        with self.db.transaction() as connection:
            pipeline_row = connection.execute(
                """
                SELECT id, project_id, status, inputs_json
                FROM pipeline_executions
                WHERE id = %s AND pipeline_name = %s
                FOR UPDATE
                """,
                (ask_run_id, ASK_PIPELINE_NAME),
            ).fetchone()
            if pipeline_row is None:
                raise AskPermissionDenied("Ask run not found")
            if pipeline_row["status"] not in {"pending", "running", "interrupted"}:
                raise AskPermissionDenied("Ask run is not active")
            document = _json_object(pipeline_row["inputs_json"], name="Ask pipeline inputs")
            ask_inputs = _json_object(document.get("ask"), name="Ask pipeline input")
            profile_snapshot = _profile_snapshot(ask_inputs, stage)
            profile_hash = _required_string(
                profile_snapshot.get("content_hash"), name="Ask profile snapshot hash"
            )
            effective = _json_object(
                profile_snapshot.get("effective"), name="Ask effective profile"
            )
            if effective.get("provider") != runtime_profile.provider:
                raise AskPermissionDenied("Ask runtime provider does not match profile snapshot")
            agent_row = connection.execute(
                """
                SELECT id, provider, status, child_session_id, workflow_name
                FROM agent_runs WHERE id = %s
                """,
                (agent_run_id,),
            ).fetchone()
            if (
                agent_row is None
                or agent_row["provider"] != runtime_profile.provider
                or agent_row["workflow_name"] != ASK_PIPELINE_NAME
                or agent_row["status"] not in {"pending", "running"}
                or not agent_row["child_session_id"]
            ):
                raise AskPermissionDenied("agent run is not eligible for Ask authority")
            binding = _json_object(ask_inputs.get("binding"), name="Ask binding")
            immutable = {
                "kind": _AUTHORITY_KIND,
                "version": _AUTHORITY_VERSION,
                "ask_run_id": ask_run_id,
                "project_id": str(pipeline_row["project_id"]),
                "stage": stage.value,
                "attempt": attempt,
                "step_id": step_id,
                "deadline_at": _deadline(binding.get("deadline_at")).isoformat(),
                "binding_hash": _fingerprint(binding),
                "profile_snapshot_hash": profile_hash,
                "runtime_profile": runtime_profile.to_dict(),
                "runtime_profile_hash": runtime_profile.profile_hash,
                "scratch_root": str(scratch),
            }
            lifecycle = {
                "active": True,
                "current_agent_run_id": agent_run_id,
                "generation": 1,
                "superseded_agent_run_ids": [],
                "revocation_reason": None,
            }
            step_row = connection.execute(
                """
                SELECT id, step_id, status FROM step_executions
                WHERE execution_id = %s AND step_id = %s
                FOR UPDATE
                """,
                (ask_run_id, step_id),
            ).fetchone()
            if step_row is None or step_row["status"] != StepStatus.RUNNING.value:
                raise AskPermissionDenied("Ask declared stage is not accepting a principal")
            runtime = _json_object(ask_inputs.get("runtime", {}), name="Ask runtime metadata")
            authorities = _json_object(runtime.get("authorities", {}), name="Ask authorities")
            authority = {"immutable": immutable, "lifecycle": lifecycle}
            existing = authorities.get(step_id)
            if existing is not None:
                existing_immutable, existing_lifecycle = _authority_entry(existing)
                if existing_immutable != immutable:
                    raise AskPermissionDenied("Ask stage attempt binding is immutable")
                if (
                    existing_lifecycle.get("active") is not True
                    or existing_lifecycle.get("current_agent_run_id") != agent_run_id
                ):
                    raise AskPermissionDenied("Ask stage attempt already has another principal")
                authority = _json_object(existing, name="Ask authority entry")
            else:
                authorities[step_id] = authority
                runtime["authorities"] = authorities
                ask_inputs["runtime"] = runtime
                document["ask"] = ask_inputs
                connection.execute(
                    "UPDATE pipeline_executions SET inputs_json = %s, updated_at = NOW() WHERE id = %s",
                    (_canonical_json(document), ask_run_id),
                )
            return _principal_from_authority(pipeline_row, step_row, authority)

    def find(self, agent_run_id: str) -> AskPrincipal | None:
        row = self.db.fetchone(
            """
            SELECT
                pe.id AS pipeline_execution_id,
                pe.project_id,
                pe.status AS pipeline_status,
                pe.inputs_json AS pipeline_inputs_json
            FROM pipeline_executions pe
            WHERE pe.pipeline_name = %s
              AND EXISTS (
                SELECT 1
                FROM jsonb_each(
                    COALESCE(
                        pe.inputs_json::jsonb #> '{ask,runtime,authorities}',
                        '{}'::jsonb
                    )
                ) AS authority(step_id, body)
                WHERE authority.body #>> '{lifecycle,current_agent_run_id}' = %s
                OR EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements_text(
                        COALESCE(
                            authority.body #> '{lifecycle,superseded_agent_run_ids}',
                            '[]'::jsonb
                        )
                    ) AS superseded(value)
                    WHERE superseded.value = %s
                )
              )
            LIMIT 1
            """,
            (ASK_PIPELINE_NAME, agent_run_id, agent_run_id),
        )
        if row is None:
            return None
        ask_inputs = _ask_inputs(row["pipeline_inputs_json"])
        found = _find_authority_entry(ask_inputs, agent_run_id)
        if found is None:
            raise AskPermissionDenied("Ask authority disappeared during resolution")
        step_id, authority, superseded = found
        if superseded:
            raise AskPermissionDenied("Ask agent run was superseded")
        step_row = self.db.fetchone(
            """
            SELECT step_id, status FROM step_executions
            WHERE execution_id = %s AND step_id = %s
            """,
            (row["pipeline_execution_id"], step_id),
        )
        if step_row is None:
            raise AskPermissionDenied("Ask declared authority stage is missing")
        return _principal_from_authority(row, step_row, authority)

    def authorize(
        self,
        agent_run_id: str,
        server_name: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> AskPrincipal:
        principal = self.authorize_if_ask(
            agent_run_id,
            server_name,
            tool_name,
            arguments,
            now=now,
        )
        if principal is None:
            raise AskPermissionDenied("agent run has no Ask authority")
        return principal

    def authorize_if_ask(
        self,
        agent_run_id: str,
        server_name: str,
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        now: datetime | None = None,
    ) -> AskPrincipal | None:
        """Authorize a managed Ask caller, or return ``None`` for an ordinary run."""
        principal = self.resolve_authenticated(agent_run_id)
        if principal is None:
            return None
        _assert_live(principal, now=now)
        if (server_name, tool_name) not in principal.allowed_tools:
            raise AskPermissionDenied(
                f"Ask {principal.stage.value} cannot call {server_name}.{tool_name}"
            )
        if server_name == "gobby-ask":
            if arguments.get("run_id") != principal.ask_run_id:
                raise AskPermissionDenied("cross-run Ask access denied")
            raw_attempt = arguments.get("attempt")
            if raw_attempt is not None and raw_attempt != principal.attempt:
                raise AskPermissionDenied("stale Ask attempt denied")
        elif server_name == "gobby-agents" and arguments.get("agent_run_id") not in {
            None,
            principal.agent_run_id,
        }:
            raise AskPermissionDenied("cross-run agent completion denied")
        return principal

    def replace_for_resume(
        self,
        *,
        original_agent_run_id: str,
        successor_agent_run_id: str,
    ) -> AskPrincipal:
        with self.db.transaction() as connection:
            row = connection.execute(
                """
                SELECT
                    pe.id AS pipeline_execution_id,
                    pe.project_id,
                    pe.status AS pipeline_status,
                    pe.inputs_json AS pipeline_inputs_json
                FROM pipeline_executions pe
                WHERE pe.pipeline_name = %s
                  AND EXISTS (
                    SELECT 1
                    FROM jsonb_each(
                        COALESCE(
                            pe.inputs_json::jsonb #> '{ask,runtime,authorities}',
                            '{}'::jsonb
                        )
                    ) AS authority(step_id, body)
                    WHERE authority.body #>> '{lifecycle,current_agent_run_id}' = %s
                  )
                FOR UPDATE OF pe
                """,
                (ASK_PIPELINE_NAME, original_agent_run_id),
            ).fetchone()
            if row is None:
                raise AskPermissionDenied("original Ask principal is not current")
            document = _json_object(row["pipeline_inputs_json"], name="Ask pipeline inputs")
            ask_inputs = _json_object(document.get("ask"), name="Ask pipeline input")
            found = _find_authority_entry(ask_inputs, original_agent_run_id)
            if found is None or found[2]:
                raise AskPermissionDenied("original Ask principal is not current")
            step_id, authority, _superseded = found
            step_row = connection.execute(
                """
                SELECT step_id, status FROM step_executions
                WHERE execution_id = %s AND step_id = %s
                FOR UPDATE
                """,
                (row["pipeline_execution_id"], step_id),
            ).fetchone()
            if step_row is None:
                raise AskPermissionDenied("Ask declared authority stage is missing")
            principal = _principal_from_authority(row, step_row, authority)
            _assert_live(principal, allow_interrupted=True)
            successor = connection.execute(
                """
                SELECT provider, status, child_session_id, workflow_name
                FROM agent_runs WHERE id = %s
                """,
                (successor_agent_run_id,),
            ).fetchone()
            if (
                successor is None
                or successor["provider"] != principal.runtime_profile.provider
                or successor["workflow_name"] != ASK_PIPELINE_NAME
                or successor["status"] not in {"pending", "running"}
                or not successor["child_session_id"]
            ):
                raise AskPermissionDenied("successor agent run is not eligible for Ask authority")
            immutable, state = _authority_entry(authority)
            superseded = state.get("superseded_agent_run_ids")
            if not isinstance(superseded, list):
                raise AskPermissionDenied("invalid persisted Ask successor history")
            state["superseded_agent_run_ids"] = [*superseded, original_agent_run_id]
            state["current_agent_run_id"] = successor_agent_run_id
            state["generation"] = principal.generation + 1
            authorities = _authority_entries(ask_inputs)
            authorities[step_id] = {"immutable": immutable, "lifecycle": state}
            runtime = _json_object(ask_inputs.get("runtime", {}), name="Ask runtime metadata")
            runtime["authorities"] = authorities
            ask_inputs["runtime"] = runtime
            document["ask"] = ask_inputs
            connection.execute(
                "UPDATE pipeline_executions SET inputs_json = %s, updated_at = NOW() WHERE id = %s",
                (_canonical_json(document), row["pipeline_execution_id"]),
            )
            updated_row = dict(row)
            updated_row["pipeline_inputs_json"] = _canonical_json(document)
            return _principal_from_authority(
                updated_row,
                step_row,
                authorities[step_id],
            )

    def revoke_for_run(self, ask_run_id: str, *, reason: str) -> int:
        if not reason:
            raise ValueError("Ask revocation reason is required")
        revoked = 0
        with self.db.transaction() as connection:
            row = connection.execute(
                """
                SELECT id, inputs_json FROM pipeline_executions
                WHERE id = %s AND pipeline_name = %s
                FOR UPDATE
                """,
                (ask_run_id, ASK_PIPELINE_NAME),
            ).fetchone()
            if row is None:
                return 0
            document = _json_object(row["inputs_json"], name="Ask pipeline inputs")
            ask_inputs = _json_object(document.get("ask"), name="Ask pipeline input")
            authorities = _authority_entries(ask_inputs)
            for step_id, raw in list(authorities.items()):
                immutable, lifecycle = _authority_entry(raw)
                if lifecycle.get("active") is not True:
                    continue
                lifecycle["active"] = False
                lifecycle["revocation_reason"] = reason
                authorities[step_id] = {
                    "immutable": immutable,
                    "lifecycle": lifecycle,
                }
                revoked += 1
            if revoked:
                runtime = _json_object(ask_inputs.get("runtime", {}), name="Ask runtime metadata")
                runtime["authorities"] = authorities
                ask_inputs["runtime"] = runtime
                document["ask"] = ask_inputs
                connection.execute(
                    "UPDATE pipeline_executions SET inputs_json = %s, updated_at = NOW() WHERE id = %s",
                    (_canonical_json(document), ask_run_id),
                )
        return revoked


def _authority_step_id(stage: AskAgentStage, attempt: int) -> str:
    try:
        return {
            (AskAgentStage.INVESTIGATOR, 0): "investigate",
            (AskAgentStage.REVIEWER, 0): "review_initial",
            (AskAgentStage.REPAIR, 1): "repair",
            (AskAgentStage.REVIEWER, 1): "review_repair",
        }[(stage, attempt)]
    except KeyError as error:
        raise AskPermissionDenied("Ask stage attempt is not declared by the pipeline") from error


def _ask_inputs(raw: object) -> dict[str, Any]:
    document = _json_object(raw, name="Ask pipeline inputs")
    return _json_object(document.get("ask"), name="Ask pipeline input")


def _authority_entries(ask_inputs: Mapping[str, Any]) -> dict[str, Any]:
    runtime = _json_object(ask_inputs.get("runtime", {}), name="Ask runtime metadata")
    return _json_object(runtime.get("authorities", {}), name="Ask authorities")


def _authority_entry(raw: object) -> tuple[dict[str, Any], dict[str, Any]]:
    entry = _json_object(raw, name="Ask authority entry")
    immutable = _json_object(entry.get("immutable"), name="Ask authority")
    lifecycle = _json_object(entry.get("lifecycle"), name="Ask authority lifecycle")
    return immutable, lifecycle


def _find_authority_entry(
    ask_inputs: Mapping[str, Any], agent_run_id: str
) -> tuple[str, dict[str, Any], bool] | None:
    for step_id, raw in _authority_entries(ask_inputs).items():
        _immutable, lifecycle = _authority_entry(raw)
        if lifecycle.get("current_agent_run_id") == agent_run_id:
            return step_id, _json_object(raw, name="Ask authority entry"), False
        superseded = lifecycle.get("superseded_agent_run_ids")
        if isinstance(superseded, list) and agent_run_id in superseded:
            return step_id, _json_object(raw, name="Ask authority entry"), True
    return None


def _profile_snapshot(ask_inputs: Mapping[str, Any], stage: AskAgentStage) -> dict[str, Any]:
    key = "reviewer" if stage is AskAgentStage.REVIEWER else "investigator"
    return _json_object(ask_inputs.get(key), name=f"Ask {key} profile snapshot")


def _principal_from_authority(
    pipeline_row: Mapping[str, Any],
    step_row: Mapping[str, Any],
    authority: object,
) -> AskPrincipal:
    immutable, state = _authority_entry(authority)
    if immutable.get("kind") != _AUTHORITY_KIND or immutable.get("version") != _AUTHORITY_VERSION:
        raise AskPermissionDenied("invalid persisted Ask authority version")
    ask_inputs = _ask_inputs(
        pipeline_row.get("pipeline_inputs_json", pipeline_row.get("inputs_json"))
    )
    binding = _json_object(ask_inputs.get("binding"), name="Ask binding")
    if immutable.get("binding_hash") != _fingerprint(binding):
        raise AskPermissionDenied("Ask binding hash mismatch")
    pipeline_run_id = _required_string(
        pipeline_row.get("pipeline_execution_id", pipeline_row.get("id")),
        name="Ask pipeline run ID",
    )
    if immutable.get("ask_run_id") != pipeline_run_id:
        raise AskPermissionDenied("Ask run ID does not match owning pipeline")
    project_id = _required_string(pipeline_row.get("project_id"), name="Ask pipeline project ID")
    if immutable.get("project_id") != project_id:
        raise AskPermissionDenied("Ask project does not match owning pipeline")
    deadline_at = _deadline(binding.get("deadline_at"))
    if _deadline(immutable.get("deadline_at")) != deadline_at:
        raise AskPermissionDenied("Ask deadline does not match immutable binding")
    stage = AskAgentStage(_required_string(immutable.get("stage"), name="Ask stage"))
    attempt = immutable.get("attempt")
    if not isinstance(attempt, int) or attempt < 0:
        raise AskPermissionDenied("invalid persisted Ask attempt")
    expected_step_id = _authority_step_id(stage, attempt)
    if immutable.get("step_id") != expected_step_id or step_row.get("step_id") != expected_step_id:
        raise AskPermissionDenied("Ask authority does not match its declared stage")
    snapshot = _profile_snapshot(ask_inputs, stage)
    snapshot_hash = _required_string(snapshot.get("content_hash"), name="Ask profile snapshot hash")
    if immutable.get("profile_snapshot_hash") != snapshot_hash:
        raise AskPermissionDenied("Ask profile snapshot hash mismatch")
    runtime_profile = AskRuntimeProfile.from_dict(immutable.get("runtime_profile"))
    if immutable.get("runtime_profile_hash") != runtime_profile.profile_hash:
        raise AskPermissionDenied("Ask runtime profile binding mismatch")
    generation = state.get("generation")
    if not isinstance(generation, int) or generation < 1:
        raise AskPermissionDenied("invalid persisted Ask authority generation")
    return AskPrincipal(
        ask_run_id=pipeline_run_id,
        project_id=project_id,
        agent_run_id=_required_string(state.get("current_agent_run_id"), name="Ask agent run ID"),
        stage=stage,
        attempt=attempt,
        generation=generation,
        deadline_at=deadline_at,
        profile_snapshot_hash=snapshot_hash,
        runtime_profile=runtime_profile,
        runtime_profile_hash=runtime_profile.profile_hash,
        pipeline_status=_required_string(
            pipeline_row.get("pipeline_status", pipeline_row.get("status")),
            name="Ask pipeline status",
        ),
        step_status=_required_string(step_row.get("status"), name="Ask step status"),
        active=state.get("active") is True,
        revocation_reason=(
            str(state["revocation_reason"]) if state.get("revocation_reason") else None
        ),
    )


def _assert_live(
    principal: AskPrincipal,
    *,
    now: datetime | None = None,
    allow_interrupted: bool = False,
) -> None:
    if not principal.active:
        reason = principal.revocation_reason or "revoked"
        raise AskPermissionDenied(f"Ask authority revoked: {reason}")
    accepted_pipeline_statuses = {"pending", "running"}
    if allow_interrupted:
        accepted_pipeline_statuses.add("interrupted")
    if principal.pipeline_status not in accepted_pipeline_statuses:
        raise AskPermissionDenied("Ask pipeline is not accepting agent actions")
    if principal.step_status != StepStatus.RUNNING.value:
        raise AskPermissionDenied("Ask stage attempt is not accepting agent actions")
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("Ask authorization clock must be timezone-aware")
    if current.astimezone(UTC) >= principal.deadline_at:
        raise AskPermissionDenied("Ask deadline exceeded")


def _policy_database(service: object) -> HubDatabase | None:
    resolve_hook_manager = getattr(service, "_resolve_hook_manager", None)
    hook_manager = resolve_hook_manager() if callable(resolve_hook_manager) else None
    db = getattr(hook_manager, "_database", None)
    if db is not None:
        return cast(HubDatabase, db)
    session_manager = getattr(service, "session_manager", None)
    if session_manager is None:
        session_manager = getattr(hook_manager, "_session_manager", None)
    database = getattr(session_manager, "db", None)
    if database is None:
        database = getattr(getattr(service, "services", None), "database", None)
    return cast(HubDatabase | None, database)


def current_ask_allowed_tools(service: object) -> frozenset[tuple[str, str]] | None:
    """Return the authenticated Ask allowlist, or ``None`` for an ordinary caller."""
    agent_run_id = get_current_agent_run_id()
    if agent_run_id is None:
        return None
    db = _policy_database(service)
    if db is None:
        raise AskPermissionDenied("Ask authorization database is unavailable")
    principal = AskPermissionStore(db).resolve_authenticated(agent_run_id)
    if principal is None:
        return None
    _assert_live(principal)
    return principal.allowed_tools


def ask_tool_denial_reason(
    service: object,
    server_name: str,
    tool_name: str,
    arguments: Mapping[str, Any],
) -> str | None:
    """Return a denial reason for the authenticated Ask run, or ``None``."""
    from gobby.ask.stage_authority import ask_stage_tool_denial_reason, is_ask_stage_tool

    if is_ask_stage_tool(server_name, tool_name):
        return ask_stage_tool_denial_reason(server_name, tool_name, arguments)
    agent_run_id = get_current_agent_run_id()
    if agent_run_id is None:
        return None
    db = _policy_database(service)
    if db is None:
        return "Ask authorization database is unavailable"
    store = AskPermissionStore(db)
    try:
        store.authorize_if_ask(agent_run_id, server_name, tool_name, arguments)
    except (AskPermissionDenied, psycopg.Error) as exc:
        return str(exc)
    return None


def filter_tools_for_current_ask_principal(
    db: HubDatabase,
    tools_by_server: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    now: datetime | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Hide every tool outside the authenticated Ask principal's exact stage scope."""
    agent_run_id = get_current_agent_run_id()
    if agent_run_id is None:
        return {server: [dict(tool) for tool in tools] for server, tools in tools_by_server.items()}
    principal = AskPermissionStore(db).resolve_authenticated(agent_run_id)
    if principal is None:
        return {server: [dict(tool) for tool in tools] for server, tools in tools_by_server.items()}
    _assert_live(principal, now=now)
    filtered: dict[str, list[dict[str, Any]]] = {}
    for server_name, tools in tools_by_server.items():
        visible = [
            dict(tool)
            for tool in tools
            if isinstance(tool.get("name"), str)
            and (server_name, str(tool["name"])) in principal.allowed_tools
        ]
        if visible:
            filtered[server_name] = visible
    return filtered


__all__ = [
    "ASK_PIPELINE_NAME",
    "AskAgentStage",
    "AskPermissionDenied",
    "AskPermissionStore",
    "AskPrincipal",
    "AskRuntimeProfile",
    "AskRuntimeValidation",
    "UnsupportedAskRuntime",
    "compile_ask_runtime_profile",
    "current_ask_allowed_tools",
    "filter_tools_for_current_ask_principal",
]
