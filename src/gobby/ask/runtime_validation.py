"""Trusted managed-native Ask probe artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gobby.agents.sandbox import SandboxConfig
from gobby.install.version_probe import probe_native_bin_version

ASK_NATIVE_PROBE_EXPECTATIONS: Mapping[str, str] = {
    "native_shell": "denied",
    "native_edit": "denied",
    "unrestricted_read": "denied",
    "web": "denied",
    "descendant_spawn": "denied",
    "task_mutation": "denied",
    "foreign_mcp": "denied",
    "cross_run": "denied",
    "stale_attempt": "denied",
    "session_spoof": "denied",
    "evidence_query": "allowed",
    "evidence_read": "allowed",
    "submission": "allowed",
    "self_completion": "allowed",
}
ASK_RUNTIME_CONTROLS = frozenset(
    {
        "mcp_allowlist_exact",
        "native_execution_denied",
        "native_mutation_denied",
        "network_denied",
        "resume_preserves_boundary",
        "source_outside_writable_root",
        "subagents_denied",
    }
)
_NATIVE_RECEIPT_CASES = frozenset({"native_shell", "native_edit", "unrestricted_read", "web"})
_VALIDATION_VERSION = 1
_SUPPORTED_CLAUDE_AUTH_MODES = frozenset({"claude.ai", "api_key", "api_key_helper"})


@dataclass(frozen=True, slots=True)
class AskRuntimeValidationArtifact:
    path: Path
    sha256: str


@dataclass(frozen=True, slots=True)
class AskRuntimeValidation:
    provider: str
    provider_executable: str
    provider_executable_sha256: str
    provider_version: str
    auth_mode: str
    control_digest: str
    controls: frozenset[str]
    fresh_probe_passed: bool
    resume_probe_passed: bool
    evidence_sha256: str
    schema_version: int = _VALIDATION_VERSION
    verified_artifact: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.schema_version != _VALIDATION_VERSION:
            raise ValueError("unsupported Ask runtime validation version")
        for name, value in {
            "provider": self.provider,
            "provider executable": self.provider_executable,
            "provider version": self.provider_version,
            "auth mode": self.auth_mode,
        }.items():
            if not value:
                raise ValueError(f"Ask runtime validation {name} is incomplete")
        for name, value in {
            "provider executable": self.provider_executable_sha256,
            "control": self.control_digest,
            "evidence": self.evidence_sha256,
        }.items():
            _validate_sha256(value, name=name)

    @property
    def validation_digest(self) -> str:
        return _fingerprint(
            {
                "schema_version": self.schema_version,
                "provider": self.provider,
                "provider_executable": self.provider_executable,
                "provider_executable_sha256": self.provider_executable_sha256,
                "provider_version": self.provider_version,
                "auth_mode": self.auth_mode,
                "control_digest": self.control_digest,
                "controls": sorted(self.controls),
                "fresh_probe_passed": self.fresh_probe_passed,
                "resume_probe_passed": self.resume_probe_passed,
                "evidence_sha256": self.evidence_sha256,
            }
        )


def ask_provider_args(provider: str, auth_mode: str) -> tuple[str, ...]:
    if provider != "claude":
        raise ValueError(f"provider {provider!r} has no proven native Ask controls")
    if auth_mode not in _SUPPORTED_CLAUDE_AUTH_MODES:
        raise ValueError("Ask runtime auth mode is unsupported")
    arguments = (
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
    return ("--bare", *arguments) if auth_mode in {"api_key", "api_key_helper"} else arguments


def ask_sandbox_config(source_root: str, scratch_root: str) -> SandboxConfig:
    return SandboxConfig(
        enabled=True,
        backend="srt",
        mode="restrictive",
        allow_network=False,
        extra_deny_read_paths=[source_root],
        extra_deny_write_paths=[source_root, scratch_root],
        allow_git_network=False,
        allow_package_registries=False,
    )


def ask_runtime_control_digest(provider: str, auth_mode: str) -> str:
    material = {
        "provider": provider,
        "provider_args": list(ask_provider_args(provider, auth_mode)),
        "builtin_tools": ["EndConversation"],
        "auto_approve": False,
        "sandbox": ask_sandbox_config("<source>", "<scratch>").model_dump(mode="json"),
        "controls": sorted(ASK_RUNTIME_CONTROLS),
    }
    return _fingerprint(material)


def build_ask_runtime_probe_artifact(
    *,
    provider: str,
    provider_executable: Path,
    auth_mode: str,
    control_digest: str,
    observations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    executable, executable_sha256, version = _provider_identity(provider_executable)
    expected_control_digest = ask_runtime_control_digest(provider, auth_mode)
    if control_digest != expected_control_digest:
        raise ValueError("probe control digest does not match the effective Ask profile")
    normalized = _validate_observations(
        observations,
        provider_executable=executable,
        provider_executable_sha256=executable_sha256,
        provider_version=version,
        auth_mode=auth_mode,
        control_digest=control_digest,
    )
    return {
        "schema_version": _VALIDATION_VERSION,
        "provider": provider,
        "provider_executable": executable,
        "provider_executable_sha256": executable_sha256,
        "provider_version": version,
        "auth_mode": auth_mode,
        "control_digest": control_digest,
        "controls": sorted(ASK_RUNTIME_CONTROLS),
        "fresh_probe_passed": True,
        "resume_probe_passed": True,
        "observations": normalized,
    }


def write_ask_runtime_probe_artifact(path: Path, artifact: Mapping[str, Any]) -> str:
    payload = _canonical_json(artifact).encode() + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(payload).hexdigest()


def load_ask_runtime_validation_artifacts(
    manifest_path: Path,
) -> dict[str, AskRuntimeValidationArtifact]:
    """Load a trusted profile-to-artifact manifest without permitting path escape."""
    try:
        body = json.loads(manifest_path.read_bytes())
    except json.JSONDecodeError as error:
        raise ValueError("Ask runtime validation manifest is invalid JSON") from error
    if not isinstance(body, dict) or body.get("schema_version") != _VALIDATION_VERSION:
        raise ValueError("Ask runtime validation manifest schema is unsupported")
    profiles = body.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("Ask runtime validation manifest has no profiles")
    root = manifest_path.parent.resolve()
    artifacts: dict[str, AskRuntimeValidationArtifact] = {}
    for profile, raw in profiles.items():
        if not isinstance(profile, str) or not profile or not isinstance(raw, dict):
            raise ValueError("Ask runtime validation profile entry is invalid")
        relative_path = _required_string(raw.get("path"), name="artifact path")
        artifact_sha256 = _required_string(raw.get("sha256"), name="artifact hash")
        _validate_sha256(artifact_sha256, name="artifact")
        artifact_path = (root / relative_path).resolve()
        if not artifact_path.is_relative_to(root):
            raise ValueError("Ask runtime validation artifact path escapes its manifest")
        if hashlib.sha256(artifact_path.read_bytes()).hexdigest() != artifact_sha256:
            raise ValueError("Ask runtime validation artifact hash mismatch")
        artifacts[profile] = AskRuntimeValidationArtifact(
            path=artifact_path,
            sha256=artifact_sha256,
        )
    return artifacts


def load_ask_runtime_validation(
    artifact: AskRuntimeValidationArtifact,
    *,
    provider_executable: Path,
) -> AskRuntimeValidation:
    _validate_sha256(artifact.sha256, name="artifact")
    payload = artifact.path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != artifact.sha256:
        raise ValueError("Ask runtime probe artifact hash mismatch")
    try:
        body = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ValueError("Ask runtime probe artifact is invalid JSON") from error
    if not isinstance(body, dict) or body.get("schema_version") != _VALIDATION_VERSION:
        raise ValueError("Ask runtime probe artifact schema is unsupported")
    provider = _required_string(body.get("provider"), name="provider")
    executable, executable_sha256, version = _provider_identity(provider_executable)
    if body.get("provider_executable") != executable:
        raise ValueError("Ask runtime provider executable path changed after validation")
    if body.get("provider_executable_sha256") != executable_sha256:
        raise ValueError("Ask runtime provider executable changed after validation")
    if body.get("provider_version") != version:
        raise ValueError("Ask runtime provider version changed after validation")
    auth_mode = _required_string(body.get("auth_mode"), name="auth mode")
    control_digest = ask_runtime_control_digest(provider, auth_mode)
    if body.get("control_digest") != control_digest:
        raise ValueError("Ask runtime control digest changed after validation")
    observations = body.get("observations")
    if not isinstance(observations, list):
        raise ValueError("Ask runtime probe observation matrix is invalid")
    _validate_observations(
        observations,
        provider_executable=executable,
        provider_executable_sha256=executable_sha256,
        provider_version=version,
        auth_mode=auth_mode,
        control_digest=control_digest,
    )
    if body.get("fresh_probe_passed") is not True or body.get("resume_probe_passed") is not True:
        raise ValueError("Ask runtime fresh and resumed probes did not pass")
    controls = body.get("controls")
    if not isinstance(controls, list) or frozenset(controls) != ASK_RUNTIME_CONTROLS:
        raise ValueError("Ask runtime probe controls are incomplete or widened")
    return AskRuntimeValidation(
        provider=provider,
        provider_executable=executable,
        provider_executable_sha256=executable_sha256,
        provider_version=version,
        auth_mode=auth_mode,
        control_digest=control_digest,
        controls=ASK_RUNTIME_CONTROLS,
        fresh_probe_passed=True,
        resume_probe_passed=True,
        evidence_sha256=artifact.sha256,
        verified_artifact=True,
    )


def _validate_observations(
    observations: Sequence[Mapping[str, Any]],
    *,
    provider_executable: str,
    provider_executable_sha256: str,
    provider_version: str,
    auth_mode: str,
    control_digest: str,
) -> list[dict[str, Any]]:
    expected_keys = {
        (phase, case) for phase in ("fresh", "resumed") for case in ASK_NATIVE_PROBE_EXPECTATIONS
    }
    normalized: list[dict[str, Any]] = []
    observed_keys: set[tuple[str, str]] = set()
    phase_identities: dict[str, set[tuple[str, str, str, int, str]]] = {
        "fresh": set(),
        "resumed": set(),
    }
    resumed_from_run_ids: set[str] = set()
    for raw in observations:
        phase = _required_string(raw.get("phase"), name="probe phase")
        case = _required_string(raw.get("case"), name="probe case")
        observed = _required_string(raw.get("observed"), name="probe outcome")
        receipt = raw.get("receipt")
        if not isinstance(receipt, Mapping):
            raise ValueError("Ask runtime probe requires a structured raw receipt")
        source = _required_string(receipt.get("source"), name="probe receipt source")
        expected_source = "native_runtime" if case in _NATIVE_RECEIPT_CASES else "mcp_response"
        if source != expected_source:
            raise ValueError(f"Ask runtime probe receipt source is invalid: {phase}:{case}")
        record = receipt.get("record")
        if not isinstance(record, Mapping):
            raise ValueError("Ask runtime probe requires a structured raw receipt record")
        receipt_sha256 = _required_string(receipt.get("sha256"), name="probe receipt hash")
        _validate_sha256(receipt_sha256, name="probe receipt")
        normalized_record = dict(record)
        if _fingerprint(normalized_record) != receipt_sha256:
            raise ValueError("Ask runtime probe receipt hash does not match exported bytes")
        agent_run_id = _required_string(record.get("agent_run_id"), name="probe agent run")
        session_id = _required_string(record.get("session_id"), name="probe session")
        terminal_id = _required_string(record.get("terminal_id"), name="probe terminal")
        process_id = record.get("process_id")
        if not isinstance(process_id, int) or isinstance(process_id, bool) or process_id <= 0:
            raise ValueError("Ask runtime probe receipt process identity is invalid")
        if (
            record.get("phase") != phase
            or record.get("case") != case
            or record.get("observed") != observed
            or record.get("provider_executable") != provider_executable
            or record.get("provider_executable_sha256") != provider_executable_sha256
            or record.get("provider_version") != provider_version
            or record.get("auth_mode") != auth_mode
            or record.get("control_digest") != control_digest
        ):
            raise ValueError("Ask runtime probe receipt identity or outcome is inconsistent")
        policy_hash = _required_string(record.get("policy_hash"), name="probe policy hash")
        _validate_sha256(policy_hash, name="probe policy")
        key = (phase, case)
        if key in observed_keys or key not in expected_keys:
            raise ValueError("Ask runtime probe observation matrix is invalid")
        if observed != ASK_NATIVE_PROBE_EXPECTATIONS[case]:
            raise ValueError(f"Ask runtime probe failed: {phase}:{case}")
        observed_keys.add(key)
        phase_identities[phase].add(
            (agent_run_id, session_id, terminal_id, process_id, policy_hash)
        )
        if phase == "resumed":
            resumed_from_run_id = _required_string(
                record.get("resumed_from_agent_run_id"),
                name="interrupted Ask agent run",
            )
            if resumed_from_run_id == agent_run_id:
                raise ValueError("Ask runtime resumed receipt points to its own agent run")
            resumed_from_run_ids.add(resumed_from_run_id)
        normalized.append(
            {
                "phase": phase,
                "case": case,
                "observed": observed,
                "receipt": {
                    "source": source,
                    "record": normalized_record,
                    "sha256": receipt_sha256,
                },
            }
        )
    if observed_keys != expected_keys:
        raise ValueError("Ask runtime probe observation matrix is incomplete")
    if any(len(identities) != 1 for identities in phase_identities.values()):
        raise ValueError("Ask runtime probe phase receipts do not share one process identity")
    if len(resumed_from_run_ids) != 1:
        raise ValueError("Ask runtime resumed receipts do not bind one interrupted agent run")
    normalized.sort(key=lambda item: (item["phase"], item["case"]))
    return normalized


def _provider_identity(executable: Path) -> tuple[str, str, str]:
    try:
        resolved = executable.resolve(strict=True)
    except OSError as error:
        raise ValueError("Ask runtime provider executable is unavailable") from error
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError("Ask runtime provider executable is not executable")
    version = probe_native_bin_version(resolved)
    if version is None:
        raise ValueError("Ask runtime provider version could not be probed")
    return str(resolved), hashlib.sha256(resolved.read_bytes()).hexdigest(), version


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _required_string(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"Ask runtime {name} is missing")
    return value


def _validate_sha256(value: str, *, name: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"Ask runtime {name} hash must be lowercase SHA-256")
