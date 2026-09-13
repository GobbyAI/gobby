"""Frozen cohort contract and append-only evidence record primitives."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

BASE_COMMIT = "0216f1e33f05962d49467d95fe84609041c6dba8"


CHANGE_COMMIT = "8b24ac26699aac8b24254a647aa70b208287b492"


TIMEOUT_SECONDS = 600


CLIENT_TIMEOUT_SECONDS = 630


PROFILES = ("ask-investigator", "ask-reviewer")


EXPECTED_TOOL_IDENTITIES = (
    "gobby-ask:query_evidence",
    "gobby-ask:read_evidence",
    "gobby-ask:submit_answer",
    "gobby-ask:submit_review",
    "gobby-agents:end_agent_run",
)


REQUIRED_EXECUTION_GATES = (
    "#22018",
    "#22019",
    "normal_loader_runtime_admission",
    "installed_cli_acceptance",
)


REQUIRED_ASK_FLAGS = set(
    "--timeout-seconds --retrieval --background --status --resume --cancel "
    "--export --output --format".split()
)


REQUIRED_ASK_RESULT_KEYS = set(
    "run_id status current_stage answer_outcome typed_error deadline_at profile_identities "
    "tool_identities artifact_manifest attempt_count repair_count binding evidence "
    "result_artifact usage output".split()
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


_GIT_OID = re.compile(r"^[0-9a-f]{40,64}$")


_DATABASE_SCHEMA = re.compile(r"^gobby_test_[A-Za-z0-9_]+$")


_PASSTHROUGH_ENVIRONMENT_KEYS = (
    "COMSPEC",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "PATHEXT",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "TZ",
    "WINDIR",
)


QUESTIONS = (
    ("Q01", "What is the shared platform, and which systems remain standalone?", BASE_COMMIT),
    ("Q02", "Which data source is authoritative and what is PostgreSQL's role?", BASE_COMMIT),
    ("Q03", "How does a paged synchronization protect progress?", BASE_COMMIT),
    ("Q04", "What exactly does the daily replenishment cadence do?", BASE_COMMIT),
    ("Q05", "What is the weekly lane order and why does order matter?", BASE_COMMIT),
    ("Q06", "What prevents an unreviewed run from mutating Lightspeed?", BASE_COMMIT),
    ("Q07", "What is in the vendor workbook and when is it uploaded?", BASE_COMMIT),
    ("Q08", "What recovery behavior is implemented after interruption?", BASE_COMMIT),
    ("Q09", "What does Restocks read, calculate, and publish?", BASE_COMMIT),
    ("Q10", "What does Buylist produce and where does it publish?", BASE_COMMIT),
    ("Q11", "How does Buylist catalog refresh and failure retention work?", BASE_COMMIT),
    ("Q12", "Is Buylist already part of the shared platform?", BASE_COMMIT),
    ("Q13", "Which artifacts express intent rather than implemented truth?", BASE_COMMIT),
    ("Q14", "What changed at the C3 commit?", CHANGE_COMMIT),
)


class CohortError(RuntimeError):
    """Base error for runner contract failures."""


class PreparationError(CohortError):
    """Runtime identity or native CLI preflight was not accepted."""


class AttemptError(CohortError):
    """An append-only attempt cannot be created safely."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured process result, including typed interruption state."""

    exit_code: int | None
    stdout: bytes
    stderr: bytes
    wall_seconds: float
    termination_signal: str | None = None
    interruption: str | None = None


class CommandRunner(Protocol):
    def __call__(
        self, argv: tuple[str, ...], timeout: float, environment: Mapping[str, str], /
    ) -> CommandResult: ...


class RuntimeServiceProbe(Protocol):
    def __call__(
        self, manifest: Mapping[str, Any], environment: Mapping[str, str], /
    ) -> dict[str, Any]: ...


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_new(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_json_new(path: Path, value: object) -> None:
    _write_new(path, _canonical_json(value))


def _mapping(value: object, *, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise PreparationError(f"{name} must be a JSON object")
    return cast(dict[str, Any], value)


def _string(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise PreparationError(f"{name} must be a non-empty string")
    return value


def _normalize_daemon_url(value: str) -> str:
    return value.strip().rstrip("/")


def _digest(value: object, *, name: str) -> str:
    digest = _string(value, name=name)
    if not _SHA256.fullmatch(digest):
        raise PreparationError(f"{name} must be a lowercase SHA-256")
    return digest


def cohort_contract() -> dict[str, Any]:
    """Return the answer-key-free execution contract."""
    return {
        "schema_version": 1,
        "timeout_seconds": TIMEOUT_SECONDS,
        "retrieval_mode": "deterministic",
        "profiles": list(PROFILES),
        "questions": [
            {"id": identifier, "question": question, "source_commit": commit}
            for identifier, question, commit in QUESTIONS
        ],
    }


def _require_success(result: CommandResult, *, operation: str) -> bytes:
    if result.interruption or result.exit_code != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise PreparationError(
            f"{operation} failed: {detail or result.interruption or result.exit_code}"
        )
    return result.stdout
