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

from gobby.agents.sandbox_policy import (
    SRT_SETTINGS_RELATIVE_PATH,
)
from gobby.agents.srt_runtime import SRT_POLICY_SCHEMA_VERSION as _SRT_POLICY_SCHEMA_VERSION
from gobby.ask.runtime_controls import (
    ASK_RUNTIME_CONTROLS,
    ask_runtime_control_digest,
    normalized_ask_srt_policy_digest,
)
from gobby.ask.runtime_probe_cleanup import validate_probe_group_cleanup
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
_NATIVE_RECEIPT_CASES = frozenset({"native_shell", "native_edit", "unrestricted_read", "web"})
ASK_SRT_POLICY_SCHEMA_VERSION = _SRT_POLICY_SCHEMA_VERSION
_VALIDATION_VERSION = 2


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
    srt_runtime_version: str
    srt_policy_schema_version: int
    srt_policy_digest: str | None
    controls: frozenset[str]
    fresh_probe_passed: bool
    resume_probe_passed: bool
    evidence_sha256: str | None
    schema_version: int = _VALIDATION_VERSION
    verified_artifact: bool = field(default=False, repr=False, compare=False)
    derived: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.schema_version != _VALIDATION_VERSION:
            raise ValueError("unsupported Ask runtime validation version")
        if self.srt_policy_schema_version != ASK_SRT_POLICY_SCHEMA_VERSION:
            raise ValueError("unsupported Ask SRT policy schema version")
        for name, value in {
            "provider": self.provider,
            "provider executable": self.provider_executable,
            "provider version": self.provider_version,
            "auth mode": self.auth_mode,
            "SRT runtime version": self.srt_runtime_version,
        }.items():
            if not value:
                raise ValueError(f"Ask runtime validation {name} is incomplete")
        if self.derived and self.verified_artifact:
            raise ValueError("Ask runtime validation cannot be derived and attested at once")
        digests: dict[str, str | None] = {
            "provider executable": self.provider_executable_sha256,
            "control": self.control_digest,
            "SRT policy": self.srt_policy_digest,
            "evidence": self.evidence_sha256,
        }
        for name, digest in digests.items():
            if digest is None:
                # A derived validation observed no earlier launch, so it pins no SRT
                # policy digest and holds no probe evidence to hash. The launch binds
                # its policy semantically instead. Every other digest stays mandatory.
                if not self.derived or name not in {"SRT policy", "evidence"}:
                    raise ValueError(f"Ask runtime validation {name} digest is missing")
                continue
            _validate_sha256(digest, name=name)

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
                "srt_runtime_version": self.srt_runtime_version,
                "srt_policy_schema_version": self.srt_policy_schema_version,
                "srt_policy_digest": self.srt_policy_digest,
                "derived": self.derived,
                "controls": sorted(self.controls),
                "fresh_probe_passed": self.fresh_probe_passed,
                "resume_probe_passed": self.resume_probe_passed,
                "evidence_sha256": self.evidence_sha256,
            }
        )


def build_ask_runtime_probe_artifact(
    *,
    provider: str,
    provider_executable: Path,
    auth_mode: str,
    control_digest: str,
    observations: Sequence[Mapping[str, Any]],
    runtime_identity: Mapping[str, Any],
) -> dict[str, Any]:
    executable, executable_sha256, version = _provider_identity(provider_executable)
    expected_control_digest = ask_runtime_control_digest(provider, auth_mode)
    if control_digest != expected_control_digest:
        raise ValueError("probe control digest does not match the effective Ask profile")
    normalized, policy_identity, raw_probe_sha256 = _validate_observations(
        observations,
        provider_executable=executable,
        provider_executable_sha256=executable_sha256,
        provider_version=version,
        auth_mode=auth_mode,
        control_digest=control_digest,
        require_registered_run_tmp=True,
    )
    return {
        "schema_version": _VALIDATION_VERSION,
        "raw_probe_sha256": raw_probe_sha256,
        "runtime_identity": _validate_probe_runtime_identity(runtime_identity),
        "provider": provider,
        "provider_executable": executable,
        "provider_executable_sha256": executable_sha256,
        "provider_version": version,
        "auth_mode": auth_mode,
        "control_digest": control_digest,
        "srt_runtime_version": policy_identity[0],
        "srt_policy_schema_version": policy_identity[1],
        "srt_policy_digest": policy_identity[2],
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
    executable, executable_sha256 = _provider_binary_identity(provider_executable)
    if body.get("provider_executable") != executable:
        raise ValueError("Ask runtime provider executable path changed after validation")
    if body.get("provider_executable_sha256") != executable_sha256:
        raise ValueError("Ask runtime provider executable changed after validation")
    version = probe_native_bin_version(Path(executable))
    if version is None:
        raise ValueError("Ask runtime provider version could not be probed")
    if body.get("provider_version") != version:
        raise ValueError("Ask runtime provider version changed after validation")
    auth_mode = _required_string(body.get("auth_mode"), name="auth mode")
    control_digest = ask_runtime_control_digest(provider, auth_mode)
    if body.get("control_digest") != control_digest:
        raise ValueError("Ask runtime control digest changed after validation")
    observations = body.get("observations")
    if not isinstance(observations, list):
        raise ValueError("Ask runtime probe observation matrix is invalid")
    _, policy_identity, raw_probe_sha256 = _validate_observations(
        observations,
        provider_executable=executable,
        provider_executable_sha256=executable_sha256,
        provider_version=version,
        auth_mode=auth_mode,
        control_digest=control_digest,
        require_registered_run_tmp=False,
    )
    if body.get("raw_probe_sha256") != raw_probe_sha256:
        raise ValueError("Ask runtime artifact raw probe identity mismatch")
    _validate_probe_runtime_identity(body.get("runtime_identity"))
    if (
        body.get("srt_runtime_version"),
        body.get("srt_policy_schema_version"),
        body.get("srt_policy_digest"),
    ) != policy_identity:
        raise ValueError("Ask runtime SRT policy identity changed after validation")
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
        srt_runtime_version=policy_identity[0],
        srt_policy_schema_version=policy_identity[1],
        srt_policy_digest=policy_identity[2],
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
    require_registered_run_tmp: bool,
) -> tuple[list[dict[str, Any]], tuple[str, int, str], str]:
    expected_keys = {
        (phase, case) for phase in ("fresh", "resumed") for case in ASK_NATIVE_PROBE_EXPECTATIONS
    }
    normalized: list[dict[str, Any]] = []
    observed_keys: set[tuple[str, str]] = set()
    phase_identities: dict[str, set[tuple[str, str, str, int, str, str]]] = {
        "fresh": set(),
        "resumed": set(),
    }
    resumed_from_run_ids: set[str] = set()
    policy_identities: set[tuple[str, int, str]] = set()
    raw_probe_hashes: set[str] = set()
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
        raw_probe_hashes.add(_validate_raw_evidence(record.get("raw_evidence")))
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
        policy = record.get("policy")
        if not isinstance(policy, Mapping) or _fingerprint(policy) != policy_hash:
            raise ValueError("Ask runtime probe policy hash does not match rendered policy")
        srt_runtime_version = _required_string(
            record.get("srt_runtime_version"),
            name="probe SRT runtime version",
        )
        srt_policy_schema_version = record.get("srt_policy_schema_version")
        if (
            not isinstance(srt_policy_schema_version, int)
            or isinstance(srt_policy_schema_version, bool)
            or srt_policy_schema_version != ASK_SRT_POLICY_SCHEMA_VERSION
        ):
            raise ValueError("Ask runtime probe SRT policy schema is unsupported")
        raw_run_tmp_root = record.get("run_tmp_root")
        if raw_run_tmp_root is not None and not isinstance(raw_run_tmp_root, str):
            raise ValueError("Ask runtime probe run temp root is invalid")
        policy_digest = normalized_ask_srt_policy_digest(
            policy,
            source_root=_required_string(record.get("source_root"), name="probe source root"),
            scratch_root=_required_string(record.get("scratch_root"), name="probe scratch root"),
            policy_path=_required_string(record.get("policy_path"), name="probe policy path"),
            run_tmp_root=raw_run_tmp_root,
            require_registered_run_tmp=require_registered_run_tmp,
            managed_bootstrap_path=_required_string(
                record.get("managed_bootstrap_path"),
                name="managed grant path",
            ),
        )
        policy_identities.add((srt_runtime_version, srt_policy_schema_version, policy_digest))
        key = (phase, case)
        if key in observed_keys or key not in expected_keys:
            raise ValueError("Ask runtime probe observation matrix is invalid")
        if observed != ASK_NATIVE_PROBE_EXPECTATIONS[case]:
            raise ValueError(f"Ask runtime probe failed: {phase}:{case}")
        observed_keys.add(key)
        phase_identities[phase].add(
            (
                agent_run_id,
                session_id,
                terminal_id,
                process_id,
                policy_hash,
                record["raw_evidence"]["process_start_identity"],
            )
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
    if len(policy_identities) != 1:
        raise ValueError("Ask runtime fresh and resumed SRT policy semantics differ")
    if len(raw_probe_hashes) != 1:
        raise ValueError("Ask runtime observations do not share one raw probe")
    if next(iter(phase_identities["fresh"]))[0] == next(iter(phase_identities["resumed"]))[0]:
        raise ValueError("Ask runtime fresh and resumed agent identities must be distinct")
    normalized.sort(key=lambda item: (item["phase"], item["case"]))
    return normalized, policy_identities.pop(), raw_probe_hashes.pop()


def _provider_identity(executable: Path) -> tuple[str, str, str]:
    resolved, executable_sha256 = _provider_binary_identity(executable)
    version = probe_native_bin_version(Path(resolved))
    if version is None:
        raise ValueError("Ask runtime provider version could not be probed")
    return resolved, executable_sha256, version


def bind_ask_runtime_observations(
    raw_probe_path: Path, observations: Sequence[Mapping[str, Any]]
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Bind reviewed outcomes to captured identities and exact response bytes.

    The operator interprets the response. This check proves which captured bytes
    support that interpretation; it does not infer a denial from arbitrary text.
    """
    try:
        payload = raw_probe_path.read_bytes()
        expected_hash = raw_probe_path.with_suffix(".sha256").read_text().strip()
        raw = json.loads(payload)
    except (OSError, ValueError) as error:
        raise ValueError("Ask runtime raw probe is missing or invalid") from error
    raw_hash = hashlib.sha256(payload).hexdigest()
    if raw_hash != expected_hash or not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("Ask runtime raw probe hash or schema mismatch")
    if (
        raw.get("complete") is not True
        or raw.get("capture_errors") != []
        or raw.get("missing_agent_run_ids") != []
    ):
        raise ValueError("Ask runtime raw probe export is incomplete")
    root = raw_probe_path.parent.resolve(strict=True)
    runtime_identity = _validate_probe_runtime_identity(raw.get("runtime_identity"))
    snapshots = _probe_rows(raw.get("ask_runs"), "Ask runs")
    if len(snapshots) != 2:
        raise ValueError("Ask runtime raw probe requires distinct fresh and resumed runs")
    executions = [_probe_mapping(snapshot.get("execution"), "execution") for snapshot in snapshots]
    if any(row.get("status") != "completed" for row in executions):
        raise ValueError("Ask runtime raw probe executions must be completed")
    if not executions[0].get("id") or executions[0].get("id") == executions[1].get("id"):
        raise ValueError("Ask runtime raw probe execution identities are invalid")
    if (
        any(row.get("pipeline_name") != "native-ask" for row in executions)
        or not executions[0].get("project_id")
        or executions[0].get("project_id") != executions[1].get("project_id")
        or set(snapshots[0].get("agent_run_ids", [])) & set(snapshots[1].get("agent_run_ids", []))
    ):
        raise ValueError(
            "Ask runtime raw probe phases must be distinct native Ask runs in one project"
        )
    process_sets = _probe_mapping(raw.get("process_sets"), "process sets")
    validate_probe_group_cleanup(process_sets)
    cleanup = _probe_mapping(process_sets.get("after_cleanup"), "final cleanup")
    for kind in ("agents", "workers"):
        final_rows = _probe_rows(cleanup.get(kind), kind)
        if any(row.get("live") is not False for row in final_rows):
            raise ValueError("Ask runtime raw probe still has live owned processes")
        final_identities = {_probe_process_identity(row, kind) for row in final_rows}
        identities: dict[object, tuple[object, ...]] = {}
        for snapshot in process_sets.values():
            for row in _probe_rows(_probe_mapping(snapshot, "process snapshot").get(kind), kind):
                if row.get("pid") is None and row.get("live") is False:
                    continue  # A durable agent can exist before its native process launches.
                identity = _probe_process_identity(row, kind)
                key = row.get("id") if kind == "agents" else row.get("pid")
                if key in identities and identities[key] != identity:
                    raise ValueError("Ask runtime captured process identity changed")
                identities[key] = identity
                if row.get("live") is True and identity not in final_identities:
                    raise ValueError(
                        "Ask runtime raw probe lacks complete process cleanup evidence"
                    )
    process_rows = [
        row
        for snapshot in process_sets.values()
        for row in _probe_rows(_probe_mapping(snapshot, "process snapshot").get("agents"), "agents")
    ]
    agents: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for row in _probe_rows(raw.get("agent_runs"), "agent runs"):
        agent = _probe_mapping(row.get("agent"), "agent")
        session = _probe_mapping(row.get("session"), "session")
        run_id = _required_string(agent.get("id"), name="captured agent run")
        if run_id in agents or agent.get("child_session_id") != session.get("id"):
            raise ValueError("Ask runtime raw probe agent/session binding is invalid")
        agents[run_id] = (agent, session)
    receipts: dict[str, tuple[dict[str, Any], bytes]] = {}
    for receipt in _probe_rows(raw.get("receipts"), "receipts"):
        path = Path(_required_string(receipt.get("output_path"), name="captured receipt path"))
        path = path if path.is_absolute() else root / path
        try:
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root) or path.is_symlink():
                raise ValueError("Ask runtime raw probe receipt escapes its evidence directory")
            content = resolved.read_bytes()
        except OSError as error:
            raise ValueError("Ask runtime raw probe receipt is unavailable") from error
        if (
            hashlib.sha256(content).hexdigest() != receipt.get("sha256")
            or len(content) != receipt.get("size_bytes")
            or str(resolved) in receipts
        ):
            raise ValueError("Ask runtime raw probe receipt hash, size, or identity mismatch")
        receipts[str(resolved)] = (receipt, content)
    required_kinds = {"srt-policy", "provider-transcript-and-mcp-responses"}
    if any(
        row.get("kind") in required_kinds
        for row in _probe_rows(raw.get("excluded_receipts"), "excluded receipts")
    ):
        raise ValueError("Ask runtime raw probe is missing required receipts")
    bound: list[dict[str, Any]] = []
    for observation in observations:
        receipt = _probe_mapping(observation.get("receipt"), "reviewed receipt")
        record = _probe_mapping(receipt.get("record"), "reviewed record")
        if _fingerprint(record) != receipt.get("sha256"):
            raise ValueError("Ask runtime reviewed record hash mismatch")
        run_id = _required_string(record.get("agent_run_id"), name="reviewed agent run")
        if run_id not in agents:
            raise ValueError("Ask runtime reviewed agent was not captured")
        agent, session = agents[run_id]
        phase = observation.get("phase")
        if phase not in ("fresh", "resumed"):
            raise ValueError("Ask runtime reviewed phase is invalid")
        snapshot = snapshots[0 if phase == "fresh" else 1]
        execution = executions[0 if phase == "fresh" else 1]
        metadata = _probe_mapping(agent.get("resume_metadata_json"), "resume metadata")
        sandbox = _probe_mapping(metadata.get("sandbox"), "launch sandbox")
        managed_bootstrap_path = _required_string(
            sandbox.get("managed_bootstrap_path"),
            name="captured managed grant path",
        )
        raw_captured_policy_path = Path(
            _required_string(sandbox.get("policy_path"), name="captured policy path")
        ).expanduser()
        if not raw_captured_policy_path.is_absolute() or tuple(
            raw_captured_policy_path.parts[-len(SRT_SETTINGS_RELATIVE_PATH.parts) :]
        ) != tuple(SRT_SETTINGS_RELATIVE_PATH.parts):
            raise ValueError("Ask runtime captured policy path is not a managed SRT settings path")
        captured_policy_path = raw_captured_policy_path.resolve(strict=False)
        captured_run_root = captured_policy_path.parents[len(SRT_SETTINGS_RELATIVE_PATH.parts) - 1]
        raw_captured_bootstrap = Path(managed_bootstrap_path).expanduser()
        if not raw_captured_bootstrap.is_absolute():
            raise ValueError("Ask runtime captured managed grant path is not absolute")
        captured_bootstrap = raw_captured_bootstrap.resolve(strict=False)
        if (
            captured_bootstrap.name != "grant.json"
            or captured_bootstrap.parent != captured_run_root
        ):
            raise ValueError("Ask runtime captured managed grant is outside its launch root")
        if record.get("managed_bootstrap_path") is not None:
            raise ValueError("Ask runtime reviewed receipt cannot self-attest a managed grant")
        if (
            sandbox.get("backend") != "srt"
            or sandbox.get("enforced") is not True
            or sandbox.get("policy_path") != record.get("policy_path")
            or sandbox.get("policy_hash") != record.get("policy_hash")
        ):
            raise ValueError("Ask runtime reviewed policy lacks captured launch metadata")
        if run_id not in snapshot.get("agent_run_ids", []):
            raise ValueError("Ask runtime reviewed agent belongs to a different phase")
        if (
            record.get("session_id") != session.get("id")
            or record.get("terminal_id") != agent.get("terminal_id")
            or record.get("process_id") != agent.get("pid")
            or agent.get("provider") != "claude"
            or agent.get("machine_id") != session.get("machine_id")
            or not session.get("machine_id")
            or not session.get("project_id")
            or metadata.get("project_id") != session.get("project_id")
            or execution.get("project_id") != session.get("project_id")
        ):
            raise ValueError("Ask runtime reviewed process/session identity mismatch")
        if phase == "resumed":
            predecessor = record.get("resumed_from_agent_run_id")
            if (
                predecessor != metadata.get("resumed_from_run_id")
                or predecessor not in agents
                or predecessor == run_id
                or predecessor not in snapshot.get("agent_run_ids", [])
            ):
                raise ValueError("Ask runtime reviewed successor lacks its captured predecessor")
            previous, previous_session = agents[predecessor]
            previous_metadata = _probe_mapping(
                previous.get("resume_metadata_json"), "predecessor metadata"
            )
            if (
                previous.get("provider") != agent.get("provider")
                or previous.get("machine_id") != session.get("machine_id")
                or previous_session.get("machine_id") != session.get("machine_id")
                or previous_session.get("project_id") != session.get("project_id")
                or previous_metadata.get("project_id") != session.get("project_id")
            ):
                raise ValueError("Ask runtime predecessor belongs to another runtime or project")
            authorities = execution
            for key in ("inputs_json", "ask", "runtime", "authorities"):
                authorities = _probe_mapping(authorities.get(key), f"recovered {key}")
            lifecycles = [
                _probe_mapping(_probe_mapping(authority, "authority").get("lifecycle"), "lifecycle")
                for authority in authorities.values()
            ]
            if not any(
                lifecycle.get("current_agent_run_id") == run_id
                and predecessor in lifecycle.get("superseded_agent_run_ids", [])
                for lifecycle in lifecycles
            ) or not any(
                row.get("id") == predecessor
                and row.get("pid") == previous.get("pid")
                and row.get("terminal_id") == previous.get("terminal_id")
                and row.get("live") is True
                for row in process_rows
            ):
                raise ValueError(
                    "Ask runtime predecessor lacks captured interrupted/recovered lifecycle"
                )
        live = [
            row
            for row in process_rows
            if row.get("id") == run_id
            and row.get("pid") == agent.get("pid")
            and row.get("terminal_id") == agent.get("terminal_id")
            and row.get("live") is True
        ]
        if not live or any(not row.get("start_identity") for row in live):
            raise ValueError("Ask runtime reviewed process lacks captured live/start identity")
        if not any(
            row.get("id") == run_id and row.get("pid") == agent.get("pid")
            for row in _probe_rows(cleanup.get("agents"), "final agents")
        ):
            raise ValueError("Ask runtime reviewed process lacks final cleanup evidence")
        starts = {str(row["start_identity"]) for row in live}
        if len(starts) != 1:
            raise ValueError("Ask runtime captured process identity changed")
        owned = [item for item in receipts.values() if item[0].get("agent_run_id") == run_id]
        if not required_kinds.issubset({item[0].get("kind") for item in owned}):
            raise ValueError("Ask runtime reviewed agent lacks required captured receipts")
        policies = [
            json.loads(content) for meta, content in owned if meta.get("kind") == "srt-policy"
        ]
        if not policies or any(policy != record.get("policy") for policy in policies):
            raise ValueError("Ask runtime reviewed policy differs from captured policy bytes")
        evidence = _probe_mapping(receipt.get("evidence"), "response evidence")
        path = Path(_required_string(evidence.get("path"), name="response evidence path"))
        selected = receipts.get(str((path if path.is_absolute() else root / path).resolve()))
        if selected is None:
            raise ValueError("Ask runtime response evidence was not captured")
        metadata, content = selected
        if (
            metadata.get("agent_run_id") != run_id
            or metadata.get("sha256") != evidence.get("sha256")
            or metadata.get("kind")
            not in {"provider-transcript-and-mcp-responses", "srt-violations"}
        ):
            raise ValueError("Ask runtime response evidence ownership mismatch")
        if (
            receipt.get("source") == "mcp_response"
            and metadata.get("kind") != "provider-transcript-and-mcp-responses"
        ):
            raise ValueError("Ask runtime MCP response evidence is not a captured transcript")
        start, end = evidence.get("start_byte"), evidence.get("end_byte")
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(content):
            raise ValueError("Ask runtime response evidence byte range is invalid")
        try:
            response = content[start:end].decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("Ask runtime response evidence is not UTF-8") from error
        captured = {
            "raw_probe_sha256": raw_hash,
            "receipt_sha256": metadata["sha256"],
            "start_byte": start,
            "end_byte": end,
            "response": response,
            "process_start_identity": starts.pop(),
        }
        record = {
            **record,
            "managed_bootstrap_path": managed_bootstrap_path,
            "raw_evidence": captured,
        }
        bound.append(
            {
                **observation,
                "receipt": {**receipt, "record": record, "sha256": _fingerprint(record)},
            }
        )
    return raw_hash, bound, runtime_identity


def _probe_process_identity(row: Mapping[str, Any], kind: str) -> tuple[object, ...]:
    pid = row.get("pid")
    if type(pid) is not int or pid <= 0:
        raise ValueError("Ask runtime captured process PID is invalid")
    start = _required_string(row.get("start_identity"), name="captured process start identity")
    if kind == "agents":
        return (
            _required_string(row.get("id"), name="captured process agent"),
            pid,
            _required_string(row.get("terminal_id"), name="captured process terminal"),
            start,
        )
    return pid, start


def _validate_probe_runtime_identity(value: object) -> dict[str, Any]:
    identity = _probe_mapping(value, "runtime identity")
    head = _required_string(identity.get("source_head"), name="probe source HEAD")
    if len(head) not in {40, 64} or any(c not in "0123456789abcdef" for c in head):
        raise ValueError("Ask runtime source HEAD is invalid")
    for name in ("gcode", "gterm"):
        binary = _probe_mapping(identity.get(name), f"{name} runtime identity")
        if not Path(_required_string(binary.get("path"), name=f"{name} executable")).is_absolute():
            raise ValueError("Ask runtime binary path must be absolute")
        _validate_sha256(_required_string(binary.get("sha256"), name=f"{name} hash"), name=name)
        if name == "gterm" and "version" in binary and binary["version"] is None:
            continue  # gterm currently has no version-reporting CLI.
        _required_string(binary.get("version"), name=f"{name} version")
    return identity


def _validate_raw_evidence(value: object) -> str:
    evidence = _probe_mapping(value, "raw evidence")
    for name in ("raw_probe_sha256", "receipt_sha256"):
        _validate_sha256(_required_string(evidence.get(name), name=name), name=name)
    _required_string(evidence.get("process_start_identity"), name="raw process start identity")
    response = _required_string(evidence.get("response"), name="raw response bytes")
    start, end = evidence.get("start_byte"), evidence.get("end_byte")
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 <= start < end
        or len(response.encode("utf-8")) != end - start
    ):
        raise ValueError("Ask runtime raw response byte range is inconsistent")
    return str(evidence["raw_probe_sha256"])


def _probe_mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Ask runtime raw probe {name} is invalid")
    return value


def _probe_rows(value: object, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"Ask runtime raw probe {name} is invalid")
    return [_probe_mapping(row, name) for row in value]


def _provider_binary_identity(executable: Path) -> tuple[str, str]:
    try:
        resolved = executable.resolve(strict=True)
    except OSError as error:
        raise ValueError("Ask runtime provider executable is unavailable") from error
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ValueError("Ask runtime provider executable is not executable")
    return str(resolved), hashlib.sha256(resolved.read_bytes()).hexdigest()


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
