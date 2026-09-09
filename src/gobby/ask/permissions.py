"""Fail-closed authorization boundary for managed native Ask agents.

Ask authority is stored in the existing pipeline and step execution rows. The
pipeline input owns the immutable source/profile/deadline snapshot; a step input
owns one logical stage/attempt; and the step output fences the one current native
agent run. Caller-supplied session or run arguments never create authority.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

import psycopg

from gobby.agents.sandbox import SandboxConfig
from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.session_context import get_current_agent_run_id
from gobby.workflows.pipeline_state import StepStatus

ASK_PIPELINE_NAME = "native-ask"
_AUTHORITY_KIND = "ask-agent-authority"
_AUTHORITY_VERSION = 1


class AskPermissionDenied(PermissionError):
    """An authenticated Ask agent exceeded its immutable stage authority."""


class UnsupportedAskRuntime(RuntimeError):
    """A native provider cannot enforce Ask's MCP-only action surface."""


class AskAgentStage(StrEnum):
    INVESTIGATOR = "investigator"
    REVIEWER = "reviewer"
    REPAIR = "repair"


_INVESTIGATOR_TOOLS = frozenset(
    {
        ("gobby-ask", "search_evidence"),
        ("gobby-ask", "read_evidence"),
        ("gobby-ask", "submit_draft"),
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
class AskRuntimeProfile:
    """Effective native restrictions compiled independently of mutable agent metadata."""

    provider: str
    provider_args: tuple[str, ...]
    builtin_tools: tuple[str, ...]
    auto_approve: bool
    sandbox_config: SandboxConfig
    source_root: str
    scratch_root: str
    profile_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_args": list(self.provider_args),
            "builtin_tools": list(self.builtin_tools),
            "auto_approve": self.auto_approve,
            "sandbox_config": self.sandbox_config.model_dump(mode="json"),
            "source_root": self.source_root,
            "scratch_root": self.scratch_root,
            "profile_hash": self.profile_hash,
        }

    @classmethod
    def from_dict(cls, raw: object) -> AskRuntimeProfile:
        value = _json_object(raw, name="Ask runtime profile")
        provider_args = value.get("provider_args")
        builtin_tools = value.get("builtin_tools")
        if not isinstance(provider_args, Sequence) or isinstance(provider_args, str | bytes):
            raise AskPermissionDenied("invalid persisted Ask provider arguments")
        if not isinstance(builtin_tools, Sequence) or isinstance(builtin_tools, str | bytes):
            raise AskPermissionDenied("invalid persisted Ask builtin tools")
        profile = cls(
            provider=_required_string(value.get("provider"), name="Ask provider"),
            provider_args=tuple(str(item) for item in provider_args),
            builtin_tools=tuple(str(item) for item in builtin_tools),
            auto_approve=value.get("auto_approve") is True,
            sandbox_config=SandboxConfig.model_validate(value.get("sandbox_config")),
            source_root=_required_string(value.get("source_root"), name="Ask source root"),
            scratch_root=_required_string(value.get("scratch_root"), name="Ask scratch root"),
            profile_hash=_required_string(value.get("profile_hash"), name="Ask profile hash"),
        )
        expected_hash = _runtime_profile_hash(profile.to_dict())
        if profile.profile_hash != expected_hash:
            raise AskPermissionDenied("Ask runtime profile hash mismatch")
        return profile


def _runtime_profile_hash(value: Mapping[str, Any]) -> str:
    payload = dict(value)
    payload.pop("profile_hash", None)
    return _fingerprint(payload)


def compile_ask_runtime_profile(
    *,
    provider: str,
    source_root: Path,
    scratch_root: Path,
) -> AskRuntimeProfile:
    """Compile a provider profile with an exact MCP-only native action surface."""
    source = _resolved_root(source_root, name="Ask source root")
    scratch = _resolved_root(scratch_root, name="Ask scratch root")
    if source == scratch or source in scratch.parents or scratch in source.parents:
        raise UnsupportedAskRuntime("Ask source and scratch roots must be disjoint")
    if provider != "claude":
        if provider == "codex":
            raise UnsupportedAskRuntime(
                "Codex cannot yet prove that native writes are disabled; refusing Ask runtime"
            )
        raise UnsupportedAskRuntime(
            f"Provider {provider!r} has no proven MCP-only native Ask profile"
        )

    # Claude's restricted+bare modes ignore repository/user customizations.
    # --tools "" removes action-capable built-ins including Bash/Edit/Write/Web/Task;
    # the provider retains EndConversation as a non-mutating termination tool.
    # The strict MCP file contains only the managed Gobby proxy, whose three
    # exposed wrapper tools remain subject to the canonical policy below.
    provider_args = (
        "--bare",
        "--safe-mode",
        "--restricted",
        "--disable-slash-commands",
        "--no-chrome",
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--tools",
        "",
        "--allowedTools",
        "mcp__gobby__call_tool,mcp__gobby__get_tool_schema,mcp__gobby__list_tools",
        "--strict-mcp-config",
    )
    sandbox = SandboxConfig(
        enabled=True,
        backend="srt",
        mode="restrictive",
        allow_network=False,
        extra_deny_read_paths=[str(source)],
        extra_deny_write_paths=[str(source), str(scratch)],
        allow_git_network=False,
        allow_package_registries=False,
    )
    unhashed = {
        "provider": provider,
        "provider_args": list(provider_args),
        "builtin_tools": ["EndConversation"],
        "auto_approve": False,
        "sandbox_config": sandbox.model_dump(mode="json"),
        "source_root": str(source),
        "scratch_root": str(scratch),
    }
    return AskRuntimeProfile(
        provider=provider,
        provider_args=provider_args,
        builtin_tools=("EndConversation",),
        auto_approve=False,
        sandbox_config=sandbox,
        source_root=str(source),
        scratch_root=str(scratch),
        profile_hash=_runtime_profile_hash(unhashed),
    )


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
            ask_inputs = _ask_inputs(pipeline_row["inputs_json"])
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
                "deadline_at": _deadline(binding.get("deadline_at")).isoformat(),
                "binding_hash": _fingerprint(binding),
                "profile_snapshot_hash": profile_hash,
                "runtime_profile": runtime_profile.to_dict(),
                "runtime_profile_hash": runtime_profile.profile_hash,
                "scratch_root": str(scratch),
            }
            lifecycle = {
                "ask_authority": {
                    "active": True,
                    "current_agent_run_id": agent_run_id,
                    "generation": 1,
                    "superseded_agent_run_ids": [],
                    "revocation_reason": None,
                }
            }
            step_id = _authority_step_id(stage, attempt)
            inserted = connection.execute(
                """
                INSERT INTO step_executions (
                    execution_id, step_id, status, started_at, input_json, output_json
                )
                VALUES (%s, %s, %s, NOW(), %s, %s)
                ON CONFLICT (execution_id, step_id) DO NOTHING
                RETURNING *
                """,
                (
                    ask_run_id,
                    step_id,
                    StepStatus.RUNNING.value,
                    _canonical_json(immutable),
                    _canonical_json(lifecycle),
                ),
            ).fetchone()
            row = inserted
            if row is None:
                row = connection.execute(
                    """
                    SELECT * FROM step_executions
                    WHERE execution_id = %s AND step_id = %s
                    FOR UPDATE
                    """,
                    (ask_run_id, step_id),
                ).fetchone()
                if row is None:
                    raise AskPermissionDenied("Ask authority disappeared during activation")
                if _json_object(row["input_json"], name="Ask authority") != immutable:
                    raise AskPermissionDenied("Ask stage attempt binding is immutable")
                state = _authority_state(row["output_json"])
                if (
                    row["status"] != StepStatus.RUNNING.value
                    or state.get("active") is not True
                    or state.get("current_agent_run_id") != agent_run_id
                ):
                    raise AskPermissionDenied("Ask stage attempt already has another principal")
            return _principal_from_rows(pipeline_row, row)

    def find(self, agent_run_id: str) -> AskPrincipal | None:
        row = self.db.fetchone(
            """
            SELECT
                pe.id AS pipeline_execution_id,
                pe.project_id,
                pe.status AS pipeline_status,
                pe.inputs_json AS pipeline_inputs_json,
                se.*
            FROM step_executions se
            JOIN pipeline_executions pe ON pe.id = se.execution_id
            WHERE pe.pipeline_name = %s
              AND (
                se.output_json::jsonb #>> '{ask_authority,current_agent_run_id}' = %s
                OR EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements_text(
                        COALESCE(
                            se.output_json::jsonb #> '{ask_authority,superseded_agent_run_ids}',
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
        state = _authority_state(row["output_json"])
        if state.get("current_agent_run_id") != agent_run_id:
            raise AskPermissionDenied("Ask agent run was superseded")
        return _principal_from_rows(row, row)

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
                    pe.inputs_json AS pipeline_inputs_json,
                    se.*
                FROM step_executions se
                JOIN pipeline_executions pe ON pe.id = se.execution_id
                WHERE pe.pipeline_name = %s
                  AND se.output_json::jsonb #>>
                      '{ask_authority,current_agent_run_id}' = %s
                FOR UPDATE OF se
                """,
                (ASK_PIPELINE_NAME, original_agent_run_id),
            ).fetchone()
            if row is None:
                raise AskPermissionDenied("original Ask principal is not current")
            principal = _principal_from_rows(row, row)
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
            output = _json_object(row["output_json"], name="Ask authority lifecycle")
            state = _authority_state(output)
            superseded = state.get("superseded_agent_run_ids")
            if not isinstance(superseded, list):
                raise AskPermissionDenied("invalid persisted Ask successor history")
            state["superseded_agent_run_ids"] = [*superseded, original_agent_run_id]
            state["current_agent_run_id"] = successor_agent_run_id
            state["generation"] = principal.generation + 1
            output["ask_authority"] = state
            updated = connection.execute(
                """
                UPDATE step_executions
                SET output_json = %s
                WHERE id = %s
                  AND output_json::jsonb #>>
                      '{ask_authority,current_agent_run_id}' = %s
                RETURNING *
                """,
                (_canonical_json(output), row["id"], original_agent_run_id),
            ).fetchone()
            if updated is None:
                raise AskPermissionDenied("Ask successor authority CAS failed")
            combined = dict(row)
            combined.update(dict(updated))
            return _principal_from_rows(combined, combined)

    def revoke_for_run(self, ask_run_id: str, *, reason: str) -> int:
        if not reason:
            raise ValueError("Ask revocation reason is required")
        revoked = 0
        with self.db.transaction() as connection:
            rows = connection.execute(
                """
                SELECT id, output_json
                FROM step_executions
                WHERE execution_id = %s
                  AND input_json::jsonb ->> 'kind' = %s
                  AND status = %s
                FOR UPDATE
                """,
                (ask_run_id, _AUTHORITY_KIND, StepStatus.RUNNING.value),
            ).fetchall()
            for row in rows:
                output = _json_object(row["output_json"], name="Ask authority lifecycle")
                state = _authority_state(output)
                if state.get("active") is not True:
                    continue
                state["active"] = False
                state["revocation_reason"] = reason
                output["ask_authority"] = state
                connection.execute(
                    """
                    UPDATE step_executions
                    SET status = %s, completed_at = NOW(), output_json = %s, error = %s
                    WHERE id = %s AND status = %s
                    """,
                    (
                        StepStatus.CANCELLED.value,
                        _canonical_json(output),
                        reason,
                        row["id"],
                        StepStatus.RUNNING.value,
                    ),
                )
                revoked += 1
        return revoked


def _authority_step_id(stage: AskAgentStage, attempt: int) -> str:
    return f"ask-agent:{stage.value}:{attempt}"


def _ask_inputs(raw: object) -> dict[str, Any]:
    document = _json_object(raw, name="Ask pipeline inputs")
    return _json_object(document.get("ask"), name="Ask pipeline input")


def _profile_snapshot(ask_inputs: Mapping[str, Any], stage: AskAgentStage) -> dict[str, Any]:
    key = "reviewer" if stage is AskAgentStage.REVIEWER else "investigator"
    return _json_object(ask_inputs.get(key), name=f"Ask {key} profile snapshot")


def _authority_state(raw: object) -> dict[str, Any]:
    output = _json_object(raw, name="Ask authority lifecycle")
    return _json_object(output.get("ask_authority"), name="Ask authority state")


def _principal_from_rows(
    pipeline_row: Mapping[str, Any], step_row: Mapping[str, Any]
) -> AskPrincipal:
    immutable = _json_object(step_row["input_json"], name="Ask authority")
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
    snapshot = _profile_snapshot(ask_inputs, stage)
    snapshot_hash = _required_string(snapshot.get("content_hash"), name="Ask profile snapshot hash")
    if immutable.get("profile_snapshot_hash") != snapshot_hash:
        raise AskPermissionDenied("Ask profile snapshot hash mismatch")
    runtime_profile = AskRuntimeProfile.from_dict(immutable.get("runtime_profile"))
    if immutable.get("runtime_profile_hash") != runtime_profile.profile_hash:
        raise AskPermissionDenied("Ask runtime profile binding mismatch")
    state = _authority_state(step_row["output_json"])
    generation = state.get("generation")
    attempt = immutable.get("attempt")
    if not isinstance(generation, int) or generation < 1:
        raise AskPermissionDenied("invalid persisted Ask authority generation")
    if not isinstance(attempt, int) or attempt < 0:
        raise AskPermissionDenied("invalid persisted Ask attempt")
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
    "UnsupportedAskRuntime",
    "compile_ask_runtime_profile",
    "current_ask_allowed_tools",
    "filter_tools_for_current_ask_principal",
]
