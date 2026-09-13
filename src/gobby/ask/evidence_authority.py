"""Validate persisted Ask binding generations and managed runtime grants."""

from __future__ import annotations

import hmac
import json
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gobby.ask.artifacts import AskArtifactStore
from gobby.ask.claims import canonical_json
from gobby.ask.contracts import RetrievalMode
from gobby.ask.errors import EvidenceAdmissionError
from gobby.ask.snapshots import SnapshotIndexRuntime
from gobby.ask.storage import AskRunStorage
from gobby.code_index.eligibility import overlay_project_id_for_root
from gobby.runtime_grants.schema import GrantBundle, PostgresDirect
from gobby.runtime_grants.signing import payload_checksum
from gobby.storage.managed_credentials import MANAGED_EXECUTION_BOOTSTRAP_ENV


def load_evidence_authority(
    run_id: str,
    runtime: SnapshotIndexRuntime,
    artifacts: AskArtifactStore,
    storage: AskRunStorage,
) -> tuple[
    Path,
    dict[str, Any],
    RetrievalMode,
    datetime,
    Path,
    tuple[str, ...],
    dict[str, str],
]:
    if artifacts.run_id != run_id:
        raise EvidenceAdmissionError("artifact store does not belong to the Ask run")
    record = storage.get(run_id)
    if record is None:
        raise EvidenceAdmissionError(f"Ask run does not exist: {run_id}")
    if artifacts.project_id != record.binding.project_id:
        raise EvidenceAdmissionError("artifact store does not belong to the Ask project")
    current = record.generation
    if current is None:
        raise EvidenceAdmissionError("Ask run has no current snapshot generation")
    try:
        artifacts.verify_manifest()
        lifecycle = artifacts.read_body(current.lifecycle_artifact)
    except (OSError, ValueError, RuntimeError) as error:
        raise EvidenceAdmissionError("current snapshot lifecycle artifact is invalid") from error

    deadline_at = record.binding.deadline_at
    if deadline_at.tzinfo is None:
        raise EvidenceAdmissionError("persisted evidence deadline is not timezone-aware")
    deadline_at = deadline_at.astimezone(UTC)
    remaining = (deadline_at - datetime.now(UTC)).total_seconds()
    if not math.isfinite(remaining):
        raise EvidenceAdmissionError("persisted evidence deadline is not finite")
    source_root = Path(record.binding.repository_root).resolve(strict=True)
    if not source_root.is_dir():
        raise EvidenceAdmissionError("current repository root is unavailable")
    scratch_root = artifacts.run_root.resolve()
    if source_root.is_relative_to(scratch_root) or scratch_root.is_relative_to(source_root):
        raise EvidenceAdmissionError("repository and scratch roots must be disjoint")
    binding = {
        "project_id": overlay_project_id_for_root(source_root) or record.binding.project_id,
        "commit_oid": record.binding.commit_oid,
        "tree_oid": record.binding.tree_oid,
    }
    expected = {
        "generation": current.generation,
        "run_id": record.run_id,
        "project_id": record.binding.project_id,
        "binding": binding,
        "repository_root": str(source_root),
        "deadline_at": deadline_at.isoformat().replace("+00:00", "Z"),
        "retrieval_mode": record.binding.retrieval_mode.value,
    }
    for key, value in expected.items():
        if lifecycle.get(key) != value:
            raise EvidenceAdmissionError(
                f"current binding lifecycle {key} differs from the persisted Ask run"
            )
    runtime_value = lifecycle
    executable_value = runtime_value.get("executable")
    argv_prefix_value = runtime_value.get("argv_prefix")
    if (
        not isinstance(executable_value, str)
        or not isinstance(argv_prefix_value, list)
        or not all(isinstance(value, str) for value in argv_prefix_value)
    ):
        raise EvidenceAdmissionError("current snapshot runtime command is invalid")
    executable = Path(executable_value).resolve()
    argv_prefix = tuple(argv_prefix_value)
    if (
        runtime.executable.resolve() != executable
        or runtime.argv_prefix != argv_prefix
        or runtime.managed_execution_id != runtime_value.get("managed_execution_id")
        or runtime.credential_generation != runtime_value.get("credential_generation")
    ):
        raise EvidenceAdmissionError("current snapshot runtime identity does not match")
    env = dict(runtime.env)
    if any(
        not isinstance(key, str)
        or not isinstance(value, str)
        or key.startswith("GIT_")
        or key in {"LD_PRELOAD", "PYTHONHOME", "PYTHONPATH"}
        or key.startswith("DYLD_")
        for key, value in env.items()
    ):
        raise EvidenceAdmissionError("current snapshot runtime environment is not admissible")
    _validate_runtime_environment(env, runtime, record.binding.project_id, deadline_at)
    return (
        source_root,
        json.loads(canonical_json(binding)),
        record.binding.retrieval_mode,
        deadline_at,
        executable,
        argv_prefix,
        env,
    )


def _validate_runtime_environment(
    env: Mapping[str, str],
    runtime: SnapshotIndexRuntime,
    project_id: str,
    deadline_at: datetime,
) -> None:
    if "DATABASE_URL" in env:
        raise EvidenceAdmissionError("direct database credentials are not admissible")
    expected_identity = {
        "GOBBY_AGENT_RUN_ID": runtime.managed_execution_id,
        "GOBBY_PROJECT_ID": project_id,
    }
    for key, expected in expected_identity.items():
        if env.get(key) != expected:
            raise EvidenceAdmissionError(f"managed runtime {key} identity does not match")
    if not env.get("GOBBY_MACHINE_ID") or not env.get("GOBBY_SESSION_ID"):
        raise EvidenceAdmissionError("managed runtime machine or session identity is missing")
    grant_value = env.get(MANAGED_EXECUTION_BOOTSTRAP_ENV)
    if not grant_value:
        raise EvidenceAdmissionError("managed runtime grant is missing")
    try:
        grant = GrantBundle.model_validate_json(Path(grant_value).read_bytes())
    except (OSError, ValueError) as error:
        raise EvidenceAdmissionError("managed runtime grant is invalid") from error
    if not hmac.compare_digest(grant.payload_checksum, payload_checksum(grant)):
        raise EvidenceAdmissionError("managed runtime grant checksum does not match")
    if len(grant.signature) != 64:
        raise EvidenceAdmissionError("managed runtime grant signature is invalid")
    principal = grant.principal
    if (
        principal.execution_id != runtime.managed_execution_id
        or principal.project_id != project_id
        or principal.machine_id != env["GOBBY_MACHINE_ID"]
        or principal.session_id != env["GOBBY_SESSION_ID"]
    ):
        raise EvidenceAdmissionError("managed runtime grant principal does not match")
    postgres = grant.capabilities.postgres
    if (
        not isinstance(postgres, PostgresDirect)
        or postgres.credential_generation != runtime.credential_generation
    ):
        raise EvidenceAdmissionError("managed runtime credential generation does not match")
    if grant.expires_at < int(deadline_at.timestamp()):
        raise EvidenceAdmissionError("managed runtime grant expires before the Ask deadline")
