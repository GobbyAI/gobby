"""Immutable provider and sandbox identity for managed native Ask agents."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gobby.agents.sandbox import SandboxConfig
from gobby.ask.errors import AskPermissionDenied, UnsupportedAskRuntime
from gobby.ask.runtime_validation import (
    ASK_RUNTIME_CONTROLS,
    AskRuntimeValidation,
    ask_provider_args,
    ask_runtime_control_digest,
    ask_sandbox_config,
    normalized_ask_srt_policy_digest,
)
from gobby.install.version_probe import probe_native_bin_version


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


def _normalized_model(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if normalized and normalized != "inherit" else None


def _normalized_api_base(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().rstrip("/")
    return normalized or None


def _runtime_api_base(provider: str, environment: Mapping[str, str]) -> str | None:
    key = {
        "claude": "ANTHROPIC_BASE_URL",
        "codex": "OPENAI_BASE_URL",
        "qwen": "QWEN_API_BASE",
        "grok": "GROK_API_BASE",
    }.get(provider)
    return _normalized_api_base(environment.get(key)) if key is not None else None


def _resolved_root(path: Path, *, name: str) -> Path:
    if not path.is_absolute():
        raise UnsupportedAskRuntime(f"{name} must be absolute")
    resolved = path.resolve()
    if not resolved.is_dir():
        raise UnsupportedAskRuntime(f"{name} must be an existing directory")
    return resolved


def _runtime_profile_hash(value: Mapping[str, Any]) -> str:
    payload = dict(value)
    payload.pop("profile_hash", None)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


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
    agent_profile_digest: str
    model: str
    reasoning_effort: str | None
    endpoint_api_base: str | None
    auth_mode: str
    provider_executable: str
    provider_executable_sha256: str
    provider_version: str
    srt_runtime_version: str
    srt_policy_schema_version: int
    srt_policy_digest: str
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
            "agent_profile_digest": self.agent_profile_digest,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "endpoint_api_base": self.endpoint_api_base,
            "auth_mode": self.auth_mode,
            "provider_executable": self.provider_executable,
            "provider_executable_sha256": self.provider_executable_sha256,
            "provider_version": self.provider_version,
            "srt_runtime_version": self.srt_runtime_version,
            "srt_policy_schema_version": self.srt_policy_schema_version,
            "srt_policy_digest": self.srt_policy_digest,
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
        runtime_version: str | None,
        policy_schema_version: int | None,
        policy_hash: str | None,
        policy_path: str | None,
        environment: Mapping[str, str],
    ) -> None:
        """Bind an actual SRT launch to the provider artifact pinned by this profile."""
        if backend != "srt" or not enforced or not policy_hash or not policy_path:
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
        if runtime_version != self.srt_runtime_version:
            raise UnsupportedAskRuntime("Ask SRT runtime version changed after validation")
        if policy_schema_version != self.srt_policy_schema_version:
            raise UnsupportedAskRuntime("Ask SRT policy schema changed after validation")
        try:
            policy_bytes = Path(policy_path).read_bytes()
            if hashlib.sha256(policy_bytes).hexdigest() != policy_hash:
                raise UnsupportedAskRuntime("Ask SRT policy bytes changed before launch")
            policy = json.loads(policy_bytes)
            if not isinstance(policy, Mapping):
                raise ValueError("Ask SRT policy is not an object")
            run_root = Path(policy_path).resolve(strict=False).parents[1]
            run_tmp_key = "CLAUDE_CODE_TMPDIR" if self.provider == "claude" else "TMPDIR"
            raw_run_tmp = environment.get(run_tmp_key)
            run_tmp_root = None
            if raw_run_tmp is not None and not Path(raw_run_tmp).resolve(
                strict=False
            ).is_relative_to(run_root):
                run_tmp_root = raw_run_tmp
            policy_digest = normalized_ask_srt_policy_digest(
                policy,
                source_root=self.source_root,
                scratch_root=self.scratch_root,
                policy_path=policy_path,
                run_tmp_root=run_tmp_root,
                require_registered_run_tmp=run_tmp_root is not None,
                managed_bootstrap_path=environment.get("GOBBY_MANAGED_EXECUTION_BOOTSTRAP"),
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise UnsupportedAskRuntime("Ask SRT policy could not be verified") from error
        if policy_digest != self.srt_policy_digest:
            raise UnsupportedAskRuntime("Ask SRT policy semantics changed after validation")
        if _runtime_api_base(self.provider, environment) != self.endpoint_api_base:
            raise UnsupportedAskRuntime("Ask endpoint environment changed after validation")
        if self.auth_mode == "claude.ai" and any(
            environment.get(name)
            for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
        ):
            raise UnsupportedAskRuntime("Ask auth environment changed after validation")
        expected_control = ask_runtime_control_digest(self.provider, self.auth_mode)
        if self.runtime_control_digest != expected_control:
            raise UnsupportedAskRuntime("Ask runtime controls changed after validation")

    def validate_selection(
        self,
        *,
        provider: str,
        model: str | None,
        reasoning_effort: str | None,
        api_base: str | None,
    ) -> None:
        """Reject mutable provider/model/endpoint inputs before endpoint resolution."""
        if provider != self.provider:
            raise UnsupportedAskRuntime("Ask provider changed after validation")
        if _normalized_model(model) != self.model:
            raise UnsupportedAskRuntime("Ask model changed after validation")
        if reasoning_effort != self.reasoning_effort:
            raise UnsupportedAskRuntime("Ask reasoning effort changed after validation")
        if _normalized_api_base(api_base) != self.endpoint_api_base:
            raise UnsupportedAskRuntime("Ask endpoint changed after validation")

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
            agent_profile_digest=_required_string(
                value.get("agent_profile_digest"), name="Ask agent profile digest"
            ),
            model=_required_string(value.get("model"), name="Ask model"),
            reasoning_effort=(
                str(value["reasoning_effort"])
                if value.get("reasoning_effort") is not None
                else None
            ),
            endpoint_api_base=_normalized_api_base(value.get("endpoint_api_base")),
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
            srt_runtime_version=_required_string(
                value.get("srt_runtime_version"), name="Ask SRT runtime version"
            ),
            srt_policy_schema_version=_required_int(
                value.get("srt_policy_schema_version"), name="Ask SRT policy schema version"
            ),
            srt_policy_digest=_required_string(
                value.get("srt_policy_digest"), name="Ask SRT policy digest"
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


def compile_ask_runtime_profile(
    *,
    provider: str,
    source_root: Path,
    scratch_root: Path,
    agent_profile_digest: str,
    model: str,
    reasoning_effort: str | None,
    endpoint_api_base: str | None,
    validation: AskRuntimeValidation | None = None,
) -> AskRuntimeProfile:
    """Compile a provider profile with an exact MCP-only native action surface."""
    source = _resolved_root(source_root, name="Ask source root")
    scratch = _resolved_root(scratch_root, name="Ask scratch root")
    if source == scratch or source in scratch.parents or scratch in source.parents:
        raise UnsupportedAskRuntime("Ask source and scratch roots must be disjoint")
    if len(agent_profile_digest) != 64 or any(
        character not in "0123456789abcdef" for character in agent_profile_digest
    ):
        raise UnsupportedAskRuntime("Ask agent profile digest must be lowercase SHA-256")
    normalized_model = _normalized_model(model)
    if normalized_model is None or normalized_model.startswith("endpoint:"):
        raise UnsupportedAskRuntime("Ask runtime requires an explicit provider-native model")
    normalized_api_base = _normalized_api_base(endpoint_api_base)
    if normalized_api_base is not None:
        raise UnsupportedAskRuntime("Ask runtime does not permit a custom model endpoint")
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
        "agent_profile_digest": agent_profile_digest,
        "model": normalized_model,
        "reasoning_effort": reasoning_effort,
        "endpoint_api_base": normalized_api_base,
        "auth_mode": validation.auth_mode,
        "provider_executable": validation.provider_executable,
        "provider_executable_sha256": validation.provider_executable_sha256,
        "provider_version": validation.provider_version,
        "srt_runtime_version": validation.srt_runtime_version,
        "srt_policy_schema_version": validation.srt_policy_schema_version,
        "srt_policy_digest": validation.srt_policy_digest,
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
        agent_profile_digest=agent_profile_digest,
        model=normalized_model,
        reasoning_effort=reasoning_effort,
        endpoint_api_base=normalized_api_base,
        auth_mode=validation.auth_mode,
        provider_executable=validation.provider_executable,
        provider_executable_sha256=validation.provider_executable_sha256,
        provider_version=validation.provider_version,
        srt_runtime_version=validation.srt_runtime_version,
        srt_policy_schema_version=validation.srt_policy_schema_version,
        srt_policy_digest=validation.srt_policy_digest,
        runtime_control_digest=validation.control_digest,
        runtime_validation_digest=validation.validation_digest,
        profile_hash=_runtime_profile_hash(unhashed),
    )
