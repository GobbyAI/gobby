"""Standalone runner for the frozen native Ask cohort.

Preparation seals external runtime identity before any Ask invocation. Execution
then records one append-only primary attempt per question, in cohort order.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import stat
import statistics
import subprocess
import sys
import tarfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, cast
from urllib.parse import urlsplit

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
    "--commit --timeout-seconds --retrieval --background --status --resume --cancel "
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


def _digest(value: object, *, name: str) -> str:
    digest = _string(value, name=name)
    if not _SHA256.fullmatch(digest):
        raise PreparationError(f"{name} must be a lowercase SHA-256")
    return digest


def _private_owned_path(value: object, *, name: str, directory: bool) -> Path:
    raw = Path(_string(value, name=name))
    if not raw.is_absolute():
        raise PreparationError(f"{name} must be absolute")
    try:
        metadata = raw.lstat()
        resolved = raw.resolve(strict=True)
    except OSError as error:
        raise PreparationError(f"{name} is unavailable: {error}") from error
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if raw != resolved or stat.S_ISLNK(metadata.st_mode) or not expected_type(metadata.st_mode):
        raise PreparationError(
            f"{name} must be an owned non-symlink {'directory' if directory else 'file'}"
        )
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise PreparationError(f"{name} must be owner-only")
    return resolved


def _receipt_payload(value: object, *, name: str) -> tuple[dict[str, str], dict[str, Any]]:
    receipt = _mapping(value, name=f"{name} receipt")
    path = _private_owned_path(receipt.get("path"), name=f"{name} receipt path", directory=False)
    expected = _digest(receipt.get("sha256"), name=f"{name} receipt")
    try:
        payload = path.read_bytes()
    except OSError as error:
        raise PreparationError(f"{name} receipt is unavailable: {error}") from error
    if _sha256_bytes(payload) != expected:
        raise PreparationError(f"{name} receipt hash changed")
    try:
        body = _mapping(json.loads(payload), name=f"{name} receipt body")
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PreparationError(f"{name} receipt must be JSON") from error
    return {"path": str(path), "sha256": expected}, body


def _runtime_isolation(value: object) -> dict[str, Any]:
    isolation = _mapping(value, name="runtime isolation")
    if isolation.get("mode") != "contained":
        raise PreparationError("runtime isolation mode must be contained")
    daemon_url = _string(isolation.get("daemon_url"), name="isolated daemon_url")
    try:
        endpoint = urlsplit(daemon_url)
        port = endpoint.port
    except ValueError as error:
        raise PreparationError("isolated daemon_url is invalid") from error
    if (
        endpoint.scheme != "http"
        or endpoint.hostname not in {"127.0.0.1", "::1"}
        or port is None
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.path not in {"", "/"}
        or endpoint.query
        or endpoint.fragment
    ):
        raise PreparationError(
            "isolated daemon_url must be an uncredentialed loopback HTTP endpoint"
        )

    gobby_home = _private_owned_path(
        isolation.get("gobby_home"), name="isolated GOBBY_HOME", directory=True
    )
    if gobby_home == (Path.home() / ".gobby").resolve():
        raise PreparationError("isolated GOBBY_HOME must not be the user's default Gobby home")
    bootstrap = _mapping(isolation.get("bootstrap"), name="bootstrap identity")
    bootstrap_path = _private_owned_path(
        bootstrap.get("path"), name="bootstrap path", directory=False
    )
    if bootstrap_path != gobby_home / "bootstrap.yaml":
        raise PreparationError("bootstrap path must be the contained GOBBY_HOME bootstrap")
    bootstrap_hash = _digest(bootstrap.get("sha256"), name="bootstrap")
    try:
        observed_bootstrap_hash = _sha256_file(bootstrap_path)
    except OSError as error:
        raise PreparationError(f"bootstrap is unavailable: {error}") from error
    if observed_bootstrap_hash != bootstrap_hash:
        raise PreparationError("bootstrap hash changed")

    database = _mapping(isolation.get("database"), name="database identity")
    database_public = {
        "host": _string(database.get("host"), name="database host"),
        "port": database.get("port"),
        "name": _string(database.get("name"), name="database name"),
        "schema": _string(database.get("schema"), name="database schema"),
    }
    if (
        database_public["host"] not in {"127.0.0.1", "::1"}
        or not isinstance(database_public["port"], int)
        or isinstance(database_public["port"], bool)
        or not 1 <= database_public["port"] <= 65535
        or not _DATABASE_SCHEMA.fullmatch(cast(str, database_public["schema"]))
    ):
        raise PreparationError("database identity must name a private loopback test schema")
    database_receipt, database_body = _receipt_payload(database.get("receipt"), name="database")
    if database_body != database_public:
        raise PreparationError("database receipt does not match its public identity")

    service = _mapping(isolation.get("service"), name="service identity")
    service_public = {
        "identity": _string(service.get("identity"), name="service identity"),
        "daemon_url": _string(service.get("daemon_url"), name="service daemon_url"),
    }
    if service_public["daemon_url"] != daemon_url:
        raise PreparationError("service receipt endpoint does not match isolated daemon_url")
    service_receipt, service_body = _receipt_payload(service.get("receipt"), name="service")
    if service_body != service_public:
        raise PreparationError("service receipt does not match its public identity")
    return {
        "mode": "contained",
        "daemon_url": daemon_url,
        "gobby_home": str(gobby_home),
        "bootstrap": {"path": str(bootstrap_path), "sha256": bootstrap_hash},
        "database": {**database_public, "receipt": database_receipt},
        "service": {**service_public, "receipt": service_receipt},
    }


def _sealed_environment(isolation: Mapping[str, Any]) -> dict[str, str]:
    return {
        "GOBBY_DAEMON_URL": cast(str, isolation["daemon_url"]),
        "GOBBY_HOME": cast(str, isolation["gobby_home"]),
        "GOBBY_TEST_PROTECT": "1",
    }


def _command_environment(isolation: Mapping[str, Any]) -> dict[str, str]:
    environment = {
        key: os.environ[key] for key in _PASSTHROUGH_ENVIRONMENT_KEYS if key in os.environ
    }
    environment.update(_sealed_environment(isolation))
    return environment


def _profile(value: object, *, role: str, identifier: str) -> dict[str, Any]:
    profile = _mapping(value, name=f"{role} profile")
    if profile.get("identifier") != identifier:
        raise PreparationError(f"{role} profile must identify {identifier}")
    effective = _mapping(profile.get("effective"), name=f"{role} effective profile")
    for key in ("provider", "model", "reasoning_effort"):
        _string(effective.get(key), name=f"{role} {key}")
    return {
        "identifier": identifier,
        "definition_id": _string(profile.get("definition_id"), name=f"{role} definition_id"),
        "definition_updated_at": _string(
            profile.get("definition_updated_at"), name=f"{role} definition_updated_at"
        ),
        "effective": effective,
        "content_hash": _digest(profile.get("content_hash"), name=f"{role} content_hash"),
    }


def validate_runtime_identity(value: object) -> dict[str, Any]:
    """Validate and normalize the database-derived pre-execution identity receipt."""
    identity = _mapping(value, name="runtime identity")
    if identity.get("schema_version") != 1:
        raise PreparationError("runtime identity schema_version must be 1")
    gcode = _mapping(identity.get("gcode"), name="gcode identity")
    if gcode.get("contract_version") != 10:
        raise PreparationError("installed gcode contract_version must be 10")
    profiles = _mapping(identity.get("profiles"), name="profile identities")
    if identity.get("tool_identities") != list(EXPECTED_TOOL_IDENTITIES):
        raise PreparationError("runtime Ask tool identities do not match the accepted contract")
    isolation = _runtime_isolation(identity.get("isolation"))
    gates = _mapping(identity.get("execution_gates"), name="execution gates")
    normalized_gates: dict[str, dict[str, str]] = {}
    for gate_name in REQUIRED_EXECUTION_GATES:
        gate = _mapping(gates.get(gate_name), name=f"execution gate {gate_name}")
        if gate.get("status") != "accepted":
            raise PreparationError(f"execution gate {gate_name} must be accepted")
        normalized_gates[gate_name] = {
            "status": "accepted",
            "evidence_sha256": _digest(
                gate.get("evidence_sha256"), name=f"execution gate {gate_name} evidence"
            ),
        }
    return {
        "schema_version": 1,
        "project_id": _string(identity.get("project_id"), name="project_id"),
        "gcode": {
            "version": _string(gcode.get("version"), name="gcode version"),
            "contract_version": 10,
            "executable_sha256": _digest(gcode.get("executable_sha256"), name="gcode executable"),
        },
        "profiles": {
            "investigator": _profile(
                profiles.get("investigator"),
                role="investigator",
                identifier="ask-investigator",
            ),
            "reviewer": _profile(
                profiles.get("reviewer"), role="reviewer", identifier="ask-reviewer"
            ),
        },
        "tool_identities": list(EXPECTED_TOOL_IDENTITIES),
        "isolation": isolation,
        "execution_gates": normalized_gates,
    }


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


def _validate_cli_contract(value: object) -> dict[str, Any]:
    contract = _mapping(value, name="gcode contract")
    if contract.get("tool") != "gcode" or contract.get("contract_version") != 10:
        raise PreparationError("installed gcode must expose CLI contract version 10")
    commands = contract.get("commands")
    if not isinstance(commands, list):
        raise PreparationError("gcode contract commands must be a list")
    ask = next(
        (item for item in commands if isinstance(item, dict) and item.get("name") == "ask"),
        None,
    )
    if ask is None:
        raise PreparationError("installed gcode contract does not expose ask")
    flags = ask.get("flags")
    if not isinstance(flags, list):
        raise PreparationError("installed gcode Ask flags are malformed")
    flag_map = {
        item.get("name"): item
        for item in flags
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    if not REQUIRED_ASK_FLAGS.issubset(flag_map):
        raise PreparationError("installed gcode Ask flags are incomplete")
    if flag_map["--retrieval"].get("allowed_values") != ["deterministic", "hybrid"]:
        raise PreparationError("installed gcode Ask retrieval modes do not match the contract")
    keys = ask.get("json_output_keys")
    if not isinstance(keys, list) or not REQUIRED_ASK_RESULT_KEYS.issubset(keys):
        raise PreparationError("installed gcode Ask JSON result contract is incomplete")
    return contract


def _parse_name_status(payload: bytes) -> list[dict[str, str]]:
    try:
        lines = payload.decode(errors="strict").splitlines()
    except UnicodeDecodeError as error:
        raise PreparationError("Q14 first-parent diff must be UTF-8") from error
    changes: list[dict[str, str]] = []
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 2 or not re.fullmatch(r"[A-Z]", fields[0]) or not fields[1]:
            raise PreparationError("Q14 first-parent diff contains unsupported name-status output")
        changes.append({"status": fields[0], "path": fields[1]})
    if not changes:
        raise PreparationError("Q14 first-parent diff must not be empty")
    return changes


def prepare_cohort(
    *,
    runtime_identity_path: Path,
    gcode_binary: Path,
    project_root: Path,
    output_root: Path,
    command_runner: CommandRunner | None = None,
    now: Callable[[], str] = _utc_now,
) -> Path:
    """Seal accepted runtime, tool, source, and cohort identities before execution."""
    runner = command_runner or run_command
    source = project_root.resolve()
    binary = gcode_binary.resolve()
    destination = output_root.resolve()
    if not source.is_dir():
        raise PreparationError(f"source project does not exist: {source}")
    if not binary.is_file():
        raise PreparationError(f"gcode binary does not exist: {binary}")
    if destination == source or destination.is_relative_to(source):
        raise PreparationError("cohort output must be outside the source project")
    identity_bytes = runtime_identity_path.read_bytes()
    identity = validate_runtime_identity(json.loads(identity_bytes))
    environment = _command_environment(identity["isolation"])
    binary_hash = _sha256_file(binary)
    if binary_hash != identity["gcode"]["executable_sha256"]:
        raise PreparationError("gcode executable hash differs from installed CLI acceptance")

    observed_version = (
        _require_success(
            runner((str(binary), "--version"), 30, environment),
            operation="gcode version preflight",
        )
        .decode(errors="strict")
        .strip()
    )
    if observed_version != identity["gcode"]["version"]:
        raise PreparationError("gcode version differs from installed CLI acceptance")
    contract_bytes = _require_success(
        runner((str(binary), "contract", "--format", "json"), 30, environment),
        operation="gcode contract preflight",
    )
    contract = _validate_cli_contract(json.loads(contract_bytes))

    commits: dict[str, dict[str, Any]] = {}
    for commit in (BASE_COMMIT, CHANGE_COMMIT):
        observed_commit = (
            _require_success(
                runner(
                    ("git", "-C", str(source), "rev-parse", "--verify", f"{commit}^{{commit}}"),
                    30,
                    environment,
                ),
                operation=f"source commit preflight {commit}",
            )
            .decode(errors="strict")
            .strip()
        )
        tree_oid = (
            _require_success(
                runner(
                    ("git", "-C", str(source), "rev-parse", "--verify", f"{commit}^{{tree}}"),
                    30,
                    environment,
                ),
                operation=f"source tree preflight {commit}",
            )
            .decode(errors="strict")
            .strip()
        )
        if observed_commit != commit or not _GIT_OID.fullmatch(tree_oid):
            raise PreparationError(f"source identity mismatch for {commit}")
        commit_identity: dict[str, Any] = {"tree_oid": tree_oid}
        if commit == CHANGE_COMMIT:
            first_parent = (
                _require_success(
                    runner(
                        ("git", "-C", str(source), "rev-parse", "--verify", f"{commit}^1"),
                        30,
                        environment,
                    ),
                    operation="Q14 first-parent preflight",
                )
                .decode(errors="strict")
                .strip()
            )
            if first_parent != BASE_COMMIT:
                raise PreparationError("Q14 first parent does not match the frozen baseline")
            changes = _parse_name_status(
                _require_success(
                    runner(
                        (
                            "git",
                            "-C",
                            str(source),
                            "diff-tree",
                            "--no-commit-id",
                            "--name-status",
                            "-r",
                            f"{commit}^1",
                            commit,
                        ),
                        30,
                        environment,
                    ),
                    operation="Q14 first-parent diff preflight",
                )
            )
            commit_identity.update(
                {"first_parent_oid": first_parent, "first_parent_changes": changes}
            )
        commits[commit] = commit_identity

    destination.mkdir(parents=True, exist_ok=True)
    os.chmod(destination, 0o700)
    manifest_path = destination / "cohort-manifest.json"
    manifest = {
        "schema_version": 1,
        "prepared_at": now(),
        "cohort": cohort_contract(),
        "runtime_identity_receipt": {
            "path": str(runtime_identity_path.resolve()),
            "sha256": _sha256_bytes(identity_bytes),
        },
        "runtime_identity": identity,
        "gcode": {
            "path": str(binary),
            "version": observed_version,
            "contract_version": contract["contract_version"],
            "executable_sha256": binary_hash,
            "contract_sha256": _sha256_bytes(contract_bytes),
        },
        "source": {"project_root": str(source), "commits": commits},
        "execution": {
            "serial": True,
            "primary_attempts_per_question": 1,
            "timeout_seconds": TIMEOUT_SECONDS,
            "client_timeout_seconds": CLIENT_TIMEOUT_SECONDS,
            "retrieval_mode": "deterministic",
            "query_cap": None,
            "turn_cap": None,
            "environment": _sealed_environment(identity["isolation"]),
            "environment_sha256": _sha256_bytes(
                _canonical_json(_sealed_environment(identity["isolation"]))
            ),
        },
    }
    manifest_bytes = _canonical_json(manifest)
    _write_new(destination / "runtime-identity.receipt.json", identity_bytes)
    _write_new(destination / "gcode-contract.json", contract_bytes)
    _write_new(manifest_path, manifest_bytes)
    _write_new(manifest_path.with_suffix(".sha256"), f"{_sha256_bytes(manifest_bytes)}\n".encode())
    return manifest_path


def _verified_execution_environment(manifest: Mapping[str, Any]) -> dict[str, str]:
    runtime = _mapping(manifest.get("runtime_identity"), name="runtime identity")
    isolation = _runtime_isolation(runtime.get("isolation"))
    execution = _mapping(manifest.get("execution"), name="execution contract")
    sealed = _sealed_environment(isolation)
    if execution.get("environment") != sealed:
        raise PreparationError("sealed execution environment changed")
    expected = _digest(execution.get("environment_sha256"), name="sealed execution environment")
    if _sha256_bytes(_canonical_json(sealed)) != expected:
        raise PreparationError("sealed execution environment hash changed")
    return _command_environment(isolation)


def load_prepared_manifest(path: Path) -> dict[str, Any]:
    """Load a prepared manifest only when its detached hash and contract match."""
    payload = path.read_bytes()
    expected = path.with_suffix(".sha256").read_text(encoding="ascii").strip()
    if not _SHA256.fullmatch(expected) or _sha256_bytes(payload) != expected:
        raise PreparationError("cohort manifest hash mismatch")
    manifest = _mapping(json.loads(payload), name="cohort manifest")
    if manifest.get("schema_version") != 1 or manifest.get("cohort") != cohort_contract():
        raise PreparationError("cohort manifest does not match the frozen 14-question contract")
    identity = validate_runtime_identity(manifest.get("runtime_identity"))
    manifest["runtime_identity"] = identity
    gcode = _mapping(manifest.get("gcode"), name="gcode")
    binary = Path(_string(gcode.get("path"), name="gcode path"))
    if _sha256_file(binary) != identity["gcode"]["executable_sha256"]:
        raise PreparationError("gcode executable changed after cohort preparation")
    _verified_execution_environment(manifest)
    return manifest


def build_ask_argv(
    manifest: Mapping[str, Any], question: Mapping[str, str], *, retrieval_mode: str
) -> tuple[str, ...]:
    """Build the accepted native CLI start form without query or turn caps."""
    if retrieval_mode not in {"deterministic", "hybrid"}:
        raise AttemptError(f"unsupported retrieval mode: {retrieval_mode}")
    return (
        str(manifest["gcode"]["path"]),
        "--project",
        str(manifest["source"]["project_root"]),
        "--format",
        "json",
        "ask",
        question["question"],
        "--commit",
        question["source_commit"],
        "--timeout-seconds",
        str(TIMEOUT_SECONDS),
        "--retrieval",
        retrieval_mode,
    )


def _result_object(stdout: bytes) -> dict[str, Any]:
    try:
        return _mapping(json.loads(stdout), name="Ask CLI result")
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise AttemptError(f"Ask CLI stdout is not one JSON result: {error}") from error


def _validate_result(
    result: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any],
    question: Mapping[str, str],
    retrieval_mode: str,
) -> None:
    missing = REQUIRED_ASK_RESULT_KEYS - result.keys()
    if missing:
        raise AttemptError(f"Ask result is missing contract keys: {sorted(missing)}")
    binding = _mapping(result.get("binding"), name="Ask result binding")
    expected_tree = manifest["source"]["commits"][question["source_commit"]]["tree_oid"]
    expected_binding = {
        "project_id": manifest["runtime_identity"]["project_id"],
        "commit_oid": question["source_commit"],
        "tree_oid": expected_tree,
        "retrieval_mode": "audited_hybrid" if retrieval_mode == "hybrid" else "deterministic",
    }
    for key, expected in expected_binding.items():
        if binding.get(key) != expected:
            raise AttemptError(f"Ask result binding mismatch for {key}")
    if result.get("profile_identities") != {
        "investigator": PROFILES[0],
        "reviewer": PROFILES[1],
    }:
        raise AttemptError("Ask result profile identifiers changed")
    if result.get("tool_identities") != list(EXPECTED_TOOL_IDENTITIES):
        raise AttemptError("Ask result tool identities changed")


def _safe_tar_members(path: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with tarfile.open(path, "r:") as archive:
        for member in archive.getmembers():
            pure = PurePosixPath(member.name)
            if (
                member.name in files
                or pure.is_absolute()
                or ".." in pure.parts
                or not member.isfile()
            ):
                raise AttemptError(f"unsafe Ask export member: {member.name}")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise AttemptError(f"unreadable Ask export member: {member.name}")
            files[member.name] = extracted.read()
    for required in ("manifest.json", "answer.json", "evidence-manifest.json"):
        if required not in files:
            raise AttemptError(f"Ask export is missing {required}")
    return files


def read_verified_publication(
    path: Path,
    *,
    expected_tar_sha256: object = None,
    expected_files: object = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, bytes]]:
    """Read a publication only after verifying its recorded and internal hashes."""
    if expected_tar_sha256 is not None and _sha256_file(path) != expected_tar_sha256:
        raise AttemptError("publication tar hash changed after execution")
    files = _safe_tar_members(path)
    publication = _mapping(json.loads(files["manifest.json"]), name="publication manifest")
    manifests = [publication.get("files")]
    if expected_files is not None:
        manifests.append(expected_files)
    for file_manifest in manifests:
        if not isinstance(file_manifest, list):
            raise AttemptError("publication file manifest is malformed")
        for raw in file_manifest:
            item = _mapping(raw, name="publication file entry")
            relative = _string(item.get("path"), name="publication file path")
            payload = files.get(relative)
            if payload is None or len(payload) != item.get("size_bytes"):
                raise AttemptError(f"publication file size mismatch: {relative}")
            if _sha256_bytes(payload) != item.get("sha256"):
                raise AttemptError(f"publication file hash mismatch: {relative}")
    answer = _mapping(json.loads(files["answer.json"]), name="published answer")
    evidence = _mapping(json.loads(files["evidence-manifest.json"]), name="evidence manifest")
    return publication, answer, evidence, files


def _verify_export(
    path: Path,
    *,
    result: Mapping[str, Any],
    manifest: Mapping[str, Any],
    question: Mapping[str, str],
) -> dict[str, Any]:
    publication, answer, evidence, files = read_verified_publication(path)
    run_id = _string(result.get("run_id"), name="Ask run_id")
    if publication.get("run_id") != run_id:
        raise AttemptError("publication run_id differs from the Ask result")
    provenance = _mapping(publication.get("provenance"), name="publication provenance")
    request = _mapping(provenance.get("request"), name="publication request")
    binding = _mapping(provenance.get("binding"), name="publication binding")
    if (
        request.get("question") != question["question"]
        or request.get("commit_ref") != question["source_commit"]
    ):
        raise AttemptError("publication request differs from the frozen question")
    if binding.get("commit_oid") != question["source_commit"]:
        raise AttemptError("publication source commit differs from the frozen question")
    if provenance.get("profiles") != manifest["runtime_identity"]["profiles"]:
        raise AttemptError("publication profile snapshots differ from pre-execution identities")
    if provenance.get("tool_identities") != list(EXPECTED_TOOL_IDENTITIES):
        raise AttemptError("publication tool identities differ from pre-execution identities")
    if answer.get("run_id") != run_id or answer.get("question") != question["question"]:
        raise AttemptError("published answer differs from the frozen run")
    snapshot = _mapping(evidence.get("snapshot_binding"), name="evidence snapshot binding")
    for key in ("project_id", "commit_oid", "tree_oid"):
        if snapshot.get(key) != binding.get(key):
            raise AttemptError(f"evidence snapshot binding mismatch for {key}")
    manifest_hash = _sha256_bytes(files["manifest.json"])
    pointer = result.get("artifact_manifest")
    if isinstance(pointer, dict) and pointer.get("sha256") != manifest_hash:
        raise AttemptError("result publication manifest hash differs from exported bytes")
    return {
        "tar_path": str(path),
        "tar_sha256": _sha256_file(path),
        "file_count": len(files),
        "bytes": sum(len(payload) for payload in files.values()),
        "manifest_sha256": manifest_hash,
        "files": [
            {"path": name, "sha256": _sha256_bytes(payload), "size_bytes": len(payload)}
            for name, payload in sorted(files.items())
        ],
    }


def _question(manifest: Mapping[str, Any], question_id: str) -> dict[str, str]:
    questions = manifest["cohort"]["questions"]
    found = next((item for item in questions if item["id"] == question_id), None)
    if found is None:
        raise AttemptError(f"unknown frozen question: {question_id}")
    return cast(dict[str, str], found)


def _attempt_directory(root: Path, question_id: str, kind: str, index: int) -> Path:
    label = "primary" if kind == "primary" else f"{kind}-{index:03d}"
    return root / "attempts" / question_id / label


def _execute_attempt(
    *,
    manifest_path: Path,
    manifest: Mapping[str, Any],
    question: Mapping[str, str],
    kind: Literal["primary", "retry", "hybrid"],
    index: int,
    reason: str | None,
    command_runner: CommandRunner,
    now: Callable[[], str],
) -> dict[str, Any]:
    retrieval_mode = "hybrid" if kind == "hybrid" else "deterministic"
    attempt_dir = _attempt_directory(manifest_path.parent, question["id"], kind, index)
    try:
        attempt_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    except FileExistsError as error:
        raise AttemptError(f"attempt already exists: {attempt_dir}") from error
    prompt = question["question"].encode()
    _write_new(attempt_dir / "prompt.txt", prompt)
    argv = build_ask_argv(manifest, question, retrieval_mode=retrieval_mode)
    attempt = {
        "schema_version": 1,
        "question_id": question["id"],
        "attempt_kind": kind,
        "attempt_index": index,
        "reason": reason,
        "source_commit": question["source_commit"],
        "source_tree_oid": manifest["source"]["commits"][question["source_commit"]]["tree_oid"],
        "question": question["question"],
        "prompt_artifact": "prompt.txt",
        "prompt_sha256": _sha256_bytes(prompt),
        "command_argv": list(argv),
        "requested_retrieval_mode": retrieval_mode,
        "timeout_seconds": TIMEOUT_SECONDS,
        "client_timeout_seconds": CLIENT_TIMEOUT_SECONDS,
        "started_at": now(),
        "prepared_manifest_sha256": manifest_path.with_suffix(".sha256")
        .read_text(encoding="ascii")
        .strip(),
        "requested_profiles": list(PROFILES),
    }
    _write_json_new(attempt_dir / "attempt.json", attempt)
    try:
        environment = _verified_execution_environment(manifest)
        expected_binary_hash = manifest["gcode"]["executable_sha256"]
        if _sha256_file(Path(manifest["gcode"]["path"])) != expected_binary_hash:
            raise PreparationError("gcode executable hash differs from installed CLI acceptance")
        command = command_runner(argv, CLIENT_TIMEOUT_SECONDS, environment)
    except (Exception, KeyboardInterrupt) as error:
        contract_error = isinstance(error, CohortError)
        interruption = "operator_interrupt" if isinstance(error, KeyboardInterrupt) else None
        outcome = {
            **attempt,
            "ended_at": now(),
            "disposition": "contract_error" if contract_error else "interrupted",
            "exit_code": None,
            "termination_signal": None,
            "interruption": interruption or (None if contract_error else type(error).__name__),
            "wall_seconds": "unknown",
            "error": {"type": type(error).__name__, "message": str(error), "stage": "pre_invoke"},
            "result": None,
            "usage": "unknown",
            "export": None,
        }
        _write_json_new(attempt_dir / "outcome.json", outcome)
        raise

    result: dict[str, Any] | None = None
    error_body: dict[str, str] | None = None
    disposition = "interrupted" if command.interruption else "invocation_error"
    interruption = command.interruption
    export: dict[str, Any] | None = None
    fatal_error: PreparationError | None = None
    stdout_artifact = {
        "path": "stdout.bin",
        "sha256": _sha256_bytes(command.stdout),
        "persisted": False,
    }
    stderr_artifact = {
        "path": "stderr.bin",
        "sha256": _sha256_bytes(command.stderr),
        "persisted": False,
    }
    stage = "primary_output"
    try:
        _write_new(attempt_dir / "stdout.bin", command.stdout)
        stdout_artifact["persisted"] = True
        _write_new(attempt_dir / "stderr.bin", command.stderr)
        stderr_artifact["persisted"] = True
        if command.interruption is None:
            stage = "result_validation"
            result = _result_object(command.stdout)
            _validate_result(
                result,
                manifest=manifest,
                question=question,
                retrieval_mode=retrieval_mode,
            )
            disposition = _string(result.get("status"), name="Ask status")
            if disposition == "completed":
                if command.exit_code != 0:
                    raise AttemptError("completed Ask result returned a nonzero exit code")
                stage = "export"
                environment = _verified_execution_environment(manifest)
                export_dir = attempt_dir / "export"
                export_argv = (
                    str(manifest["gcode"]["path"]),
                    "--project",
                    str(manifest["source"]["project_root"]),
                    "--format",
                    "json",
                    "ask",
                    "--export",
                    _string(result.get("run_id"), name="Ask run_id"),
                    "--output",
                    str(export_dir),
                )
                export_result = command_runner(export_argv, 120, environment)
                _write_new(attempt_dir / "export.stdout.bin", export_result.stdout)
                _write_new(attempt_dir / "export.stderr.bin", export_result.stderr)
                export_body = _result_object(
                    _require_success(export_result, operation="Ask publication export")
                )
                export_path = Path(_string(export_body.get("output"), name="Ask export output"))
                if export_path.parent.resolve() != export_dir.resolve():
                    raise AttemptError("Ask export path escaped its attempt directory")
                export = _verify_export(
                    export_path,
                    result=result,
                    manifest=manifest,
                    question=question,
                )
    except (Exception, KeyboardInterrupt) as error:
        if isinstance(error, KeyboardInterrupt):
            disposition = "interrupted"
            interruption = "operator_interrupt"
        elif isinstance(error, CohortError):
            disposition = "contract_error"
            if isinstance(error, PreparationError):
                fatal_error = error
        else:
            disposition = "export_error" if stage == "export" else "artifact_error"
        error_body = {"type": type(error).__name__, "message": str(error), "stage": stage}
    outcome = {
        **attempt,
        "ended_at": now(),
        "disposition": disposition,
        "exit_code": command.exit_code,
        "termination_signal": command.termination_signal,
        "interruption": interruption,
        "wall_seconds": command.wall_seconds,
        "stdout": stdout_artifact,
        "stderr": stderr_artifact,
        "error": error_body,
        "result": result,
        "usage": (result.get("usage") if result and result.get("usage") is not None else "unknown"),
        "export": export,
    }
    _write_json_new(attempt_dir / "outcome.json", outcome)
    if fatal_error is not None:
        raise fatal_error
    return outcome


def _read_account(attempt_dir: Path) -> dict[str, Any]:
    attempt = _mapping(json.loads((attempt_dir / "attempt.json").read_bytes()), name="attempt")
    outcome_path = attempt_dir / "outcome.json"
    if outcome_path.is_file():
        return _mapping(json.loads(outcome_path.read_bytes()), name="attempt outcome")
    return {
        **attempt,
        "disposition": "interrupted",
        "interruption": "outcome_not_recorded",
        "exit_code": None,
        "wall_seconds": "unknown",
        "result": None,
        "usage": "unknown",
        "export": None,
    }


def run_primary(
    manifest_path: Path,
    *,
    command_runner: CommandRunner | None = None,
    now: Callable[[], str] = _utc_now,
) -> list[dict[str, Any]]:
    """Run each missing primary once, serially, without replacing prior attempts."""
    manifest = load_prepared_manifest(manifest_path)
    runner = command_runner or run_command
    accounting: list[dict[str, Any]] = []
    for question_id, _prompt, _commit in QUESTIONS:
        attempt_dir = _attempt_directory(manifest_path.parent, question_id, "primary", 1)
        if (attempt_dir / "attempt.json").is_file():
            accounting.append(_read_account(attempt_dir))
            continue
        outcome = _execute_attempt(
            manifest_path=manifest_path,
            manifest=manifest,
            question=_question(manifest, question_id),
            kind="primary",
            index=1,
            reason=None,
            command_runner=runner,
            now=now,
        )
        accounting.append(outcome)
        if outcome.get("interruption") == "operator_interrupt":
            break
    return accounting


def run_supplement(
    manifest_path: Path,
    *,
    question_id: str,
    kind: Literal["retry", "hybrid"],
    reason: str,
    command_runner: CommandRunner | None = None,
    now: Callable[[], str] = _utc_now,
) -> dict[str, Any]:
    """Run one separately labelled retry or optional hybrid experiment."""
    if kind not in {"retry", "hybrid"}:
        raise AttemptError("supplement kind must be retry or hybrid")
    if not reason.strip():
        raise AttemptError("supplement reason must be recorded")
    manifest = load_prepared_manifest(manifest_path)
    primary = _attempt_directory(manifest_path.parent, question_id, "primary", 1)
    if not (primary / "attempt.json").is_file():
        raise AttemptError("a supplement requires a preserved primary attempt")
    supplement = _attempt_directory(manifest_path.parent, question_id, kind, 1)
    if supplement.exists():
        raise AttemptError(f"only one {kind} supplement is allowed per question")
    return _execute_attempt(
        manifest_path=manifest_path,
        manifest=manifest,
        question=_question(manifest, question_id),
        kind=kind,
        index=1,
        reason=reason,
        command_runner=command_runner or run_command,
        now=now,
    )


def primary_accounting(manifest_path: Path) -> list[dict[str, Any]]:
    """Account for every primary without fabricating missing outcomes."""
    load_prepared_manifest(manifest_path)
    rows: list[dict[str, Any]] = []
    for question_id, _prompt, _commit in QUESTIONS:
        attempt_dir = _attempt_directory(manifest_path.parent, question_id, "primary", 1)
        if (attempt_dir / "attempt.json").is_file():
            rows.append(_read_account(attempt_dir))
        else:
            rows.append(
                {
                    "question_id": question_id,
                    "attempt_kind": "primary",
                    "attempt_index": 1,
                    "disposition": "unrun",
                    "result": None,
                    "usage": "unknown",
                    "export": None,
                }
            )
    return rows


def _numeric_usage(value: object, *, prefix: str = "") -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    totals: dict[str, float] = {}
    for raw_key, child in value.items():
        key = f"{prefix}.{raw_key}" if prefix else str(raw_key)
        if isinstance(child, dict):
            totals.update(_numeric_usage(child, prefix=key))
        elif isinstance(child, (int, float)) and not isinstance(child, bool):
            totals[key] = float(child)
    return totals


def aggregate_runtime(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate recorded latency and usage without inventing missing values."""
    latencies = [
        float(value)
        for row in rows
        if isinstance((value := row.get("wall_seconds")), (int, float))
        and not isinstance(value, bool)
    ]
    usage_rows = [_numeric_usage(row.get("usage")) for row in rows]
    usage_keys = set().union(*(usage.keys() for usage in usage_rows))
    return {
        "latency": {
            "observed_count": len(latencies),
            "unknown_count": len(rows) - len(latencies),
            "sum_seconds": sum(latencies) if latencies else None,
            "median_seconds": statistics.median(latencies) if latencies else None,
            "min_seconds": min(latencies) if latencies else None,
            "max_seconds": max(latencies) if latencies else None,
        },
        "usage": {
            "observed_count": sum(bool(usage) for usage in usage_rows),
            "unknown_count": sum(not usage for usage in usage_rows),
            "numeric_totals": {
                key: sum(usage.get(key, 0.0) for usage in usage_rows) for key in usage_keys
            },
        },
    }


def _terminate_group(process: subprocess.Popen[bytes], termination: int) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, termination)
        else:
            process.send_signal(termination)
    except ProcessLookupError:
        pass


def run_command(
    argv: tuple[str, ...], timeout: float, environment: Mapping[str, str]
) -> CommandResult:
    """Run one owned process group with a bounded caller wait."""
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=dict(environment),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return CommandResult(
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            wall_seconds=time.monotonic() - started,
        )
    except subprocess.TimeoutExpired as error:
        _terminate_group(process, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=5)
            termination = "SIGTERM"
        except subprocess.TimeoutExpired:
            _terminate_group(process, signal.SIGKILL)
            stdout, stderr = process.communicate()
            termination = "SIGKILL"
        return CommandResult(
            exit_code=process.returncode,
            stdout=stdout or error.stdout or b"",
            stderr=stderr or error.stderr or b"",
            wall_seconds=time.monotonic() - started,
            termination_signal=termination,
            interruption="runner_timeout",
        )
    except KeyboardInterrupt:
        _terminate_group(process, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=5)
            termination = "SIGTERM"
        except subprocess.TimeoutExpired:
            _terminate_group(process, signal.SIGKILL)
            stdout, stderr = process.communicate()
            termination = "SIGKILL"
        return CommandResult(
            exit_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            wall_seconds=time.monotonic() - started,
            termination_signal=termination,
            interruption="operator_interrupt",
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare", help="seal identities before cohort execution")
    prepare.add_argument("--runtime-identity", type=Path, required=True)
    prepare.add_argument("--gcode-bin", type=Path, required=True)
    prepare.add_argument("--project-root", type=Path, required=True)
    prepare.add_argument("--output-root", type=Path, required=True)
    primary = subparsers.add_parser("run-primary", help="run missing primaries in frozen order")
    primary.add_argument("--manifest", type=Path, required=True)
    supplement = subparsers.add_parser("supplement", help="run one labelled retry or hybrid")
    supplement.add_argument("--manifest", type=Path, required=True)
    supplement.add_argument("--question", choices=[item[0] for item in QUESTIONS], required=True)
    supplement.add_argument("--kind", choices=["retry", "hybrid"], required=True)
    supplement.add_argument("--reason", required=True)
    audit = subparsers.add_parser("audit", help="print immutable primary accounting")
    audit.add_argument("--manifest", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            result: object = {
                "manifest": str(
                    prepare_cohort(
                        runtime_identity_path=arguments.runtime_identity,
                        gcode_binary=arguments.gcode_bin,
                        project_root=arguments.project_root,
                        output_root=arguments.output_root,
                    )
                )
            }
        elif arguments.command == "run-primary":
            result = run_primary(arguments.manifest)
        elif arguments.command == "supplement":
            result = run_supplement(
                arguments.manifest,
                question_id=arguments.question,
                kind=arguments.kind,
                reason=arguments.reason,
            )
        else:
            result = primary_accounting(arguments.manifest)
    except (CohortError, OSError, json.JSONDecodeError) as error:
        print(
            json.dumps({"error": type(error).__name__, "message": str(error)}, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
