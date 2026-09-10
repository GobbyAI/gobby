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

from gobby.agents.sandbox import SandboxConfig
from gobby.ask.runtime_validation import (
    ASK_RUNTIME_CONTROLS,
    AskRuntimeValidation,
    ask_provider_args,
    ask_runtime_control_digest,
    ask_sandbox_config,
)
from gobby.install.version_probe import probe_native_bin_version
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
    auth_mode: str
    provider_executable: str
    provider_executable_sha256: str
    provider_version: str
    runtime_control_digest: str
    runtime_validation_digest: str
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
            "auth_mode": self.auth_mode,
            "provider_executable": self.provider_executable,
            "provider_executable_sha256": self.provider_executable_sha256,
            "provider_version": self.provider_version,
            "runtime_control_digest": self.runtime_control_digest,
            "runtime_validation_digest": self.runtime_validation_digest,
            "profile_hash": self.profile_hash,
        }

    def validate_launch(
        self,
        *,
        backend: str,
        enforced: bool,
        provider_executable: str | None,
        policy_hash: str | None,
    ) -> None:
        """Bind an actual SRT launch to the provider artifact pinned by this profile."""
        if backend != "srt" or not enforced or not policy_hash:
            raise UnsupportedAskRuntime("Ask launch did not enforce its pinned SRT policy")
        if provider_executable is None or (
            Path(provider_executable).resolve() != Path(self.provider_executable).resolve()
        ):
            raise UnsupportedAskRuntime("Ask provider executable path changed after validation")
        executable = Path(provider_executable)
        if hashlib.sha256(executable.read_bytes()).hexdigest() != self.provider_executable_sha256:
            raise UnsupportedAskRuntime("Ask provider executable changed after validation")
        if probe_native_bin_version(executable) != self.provider_version:
            raise UnsupportedAskRuntime("Ask provider version changed after validation")
        expected_control = ask_runtime_control_digest(self.provider, self.auth_mode)
        if self.runtime_control_digest != expected_control:
            raise UnsupportedAskRuntime("Ask runtime controls changed after validation")

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
            auth_mode=_required_string(value.get("auth_mode"), name="Ask runtime auth mode"),
            provider_executable=_required_string(
                value.get("provider_executable"), name="Ask provider executable"
            ),
            provider_executable_sha256=_required_string(
                value.get("provider_executable_sha256"),
                name="Ask provider executable hash",
            ),
            provider_version=_required_string(
                value.get("provider_version"), name="Ask provider version"
            ),
            runtime_control_digest=_required_string(
                value.get("runtime_control_digest"), name="Ask runtime control digest"
            ),
            runtime_validation_digest=_required_string(
                value.get("runtime_validation_digest"),
                name="Ask runtime validation digest",
            ),
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
    validation: AskRuntimeValidation | None = None,
) -> AskRuntimeProfile:
    """Compile a provider profile with an exact MCP-only native action surface."""
    source = _resolved_root(source_root, name="Ask source root")
    scratch = _resolved_root(scratch_root, name="Ask scratch root")
    if source == scratch or source in scratch.parents or scratch in source.parents:
        raise UnsupportedAskRuntime("Ask source and scratch roots must be disjoint")
    if validation is None:
        raise UnsupportedAskRuntime("Ask runtime requires trusted fresh and resume validation")
    if not validation.verified_artifact:
        raise UnsupportedAskRuntime("Ask runtime validation must come from a pinned artifact")
    if validation.provider != provider:
        raise UnsupportedAskRuntime("Ask runtime validation provider mismatch")
    if not validation.fresh_probe_passed or not validation.resume_probe_passed:
        raise UnsupportedAskRuntime("Ask runtime validation did not pass fresh and resume probes")
    if validation.controls != ASK_RUNTIME_CONTROLS:
        raise UnsupportedAskRuntime("Ask runtime validation controls are incomplete or widened")
    if provider != "claude":
        if provider == "codex":
            raise UnsupportedAskRuntime(
                "Codex cannot yet prove that native writes are disabled; refusing Ask runtime"
            )
        raise UnsupportedAskRuntime(
            f"Provider {provider!r} has no proven MCP-only native Ask profile"
        )
    try:
        provider_args = ask_provider_args(provider, validation.auth_mode)
        sandbox = ask_sandbox_config(str(source), str(scratch))
        expected_control_digest = ask_runtime_control_digest(provider, validation.auth_mode)
    except ValueError as error:
        raise UnsupportedAskRuntime(str(error)) from error
    if validation.control_digest != expected_control_digest:
        raise UnsupportedAskRuntime("Ask runtime validation control digest mismatch")
    unhashed = {
        "provider": provider,
        "provider_args": list(provider_args),
        "builtin_tools": ["EndConversation"],
        "auto_approve": False,
        "sandbox_config": sandbox.model_dump(mode="json"),
        "source_root": str(source),
        "scratch_root": str(scratch),
        "auth_mode": validation.auth_mode,
        "provider_executable": validation.provider_executable,
        "provider_executable_sha256": validation.provider_executable_sha256,
        "provider_version": validation.provider_version,
        "runtime_control_digest": validation.control_digest,
        "runtime_validation_digest": validation.validation_digest,
    }
    return AskRuntimeProfile(
        provider=provider,
        provider_args=provider_args,
        builtin_tools=("EndConversation",),
        auto_approve=False,
        sandbox_config=sandbox,
        source_root=str(source),
        scratch_root=str(scratch),
        auth_mode=validation.auth_mode,
        provider_executable=validation.provider_executable,
        provider_executable_sha256=validation.provider_executable_sha256,
        provider_version=validation.provider_version,
        runtime_control_digest=validation.control_digest,
        runtime_validation_digest=validation.validation_digest,
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
