"""Prepare pinned caller worktrees and record serial native Ask attempts."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import statistics
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, cast

from ask_cohort_publication import verify_export
from ask_cohort_records import (
    _GIT_OID,
    _SHA256,
    BASE_COMMIT,
    CHANGE_COMMIT,
    CLIENT_TIMEOUT_SECONDS,
    EXPECTED_TOOL_IDENTITIES,
    PROFILES,
    QUESTIONS,
    REQUIRED_ASK_FLAGS,
    REQUIRED_ASK_RESULT_KEYS,
    TIMEOUT_SECONDS,
    AttemptError,
    CohortError,
    CommandResult,
    CommandRunner,
    PreparationError,
    RuntimeServiceProbe,
    _canonical_json,
    _mapping,
    _require_success,
    _sha256_bytes,
    _sha256_file,
    _string,
    _utc_now,
    _write_json_new,
    _write_new,
    cohort_contract,
)
from ask_cohort_runtime import (
    _command_environment,
    _probe_live_runtime_service,
    _sealed_environment,
    _verified_execution_environment,
    _verify_live_runtime_service,
    create_caller_worktree,
    prepare_caller_worktrees,
    validate_runtime_identity,
)


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
    if "--commit" in flag_map:
        raise PreparationError("installed gcode still exposes removed Ask --commit")
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
    worktree_creator: Callable[
        [Mapping[str, Any], Mapping[str, str], str, str], dict[str, Any]
    ] = create_caller_worktree,
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
    prepare_caller_worktrees(
        identity, environment, commits, destination, binary, runner, worktree_creator
    )
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
        str(manifest["source"]["commits"][question["source_commit"]]["project_root"]),
        "--format",
        "json",
        "ask",
        question["question"],
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
        "repository_root": manifest["source"]["commits"][question["source_commit"]]["project_root"],
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
    runtime_service_probe: RuntimeServiceProbe,
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
        _verify_live_runtime_service(manifest, command_runner, environment, runtime_service_probe)
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
    export_command: dict[str, Any] | None = None
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
                _verify_live_runtime_service(
                    manifest, command_runner, environment, runtime_service_probe
                )
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
                export_stdout = {
                    "path": "export.stdout.bin",
                    "sha256": _sha256_bytes(export_result.stdout),
                    "persisted": False,
                }
                export_stderr = {
                    "path": "export.stderr.bin",
                    "sha256": _sha256_bytes(export_result.stderr),
                    "persisted": False,
                }
                export_command = {
                    "exit_code": export_result.exit_code,
                    "termination_signal": export_result.termination_signal,
                    "interruption": export_result.interruption,
                    "wall_seconds": export_result.wall_seconds,
                    "stdout": export_stdout,
                    "stderr": export_stderr,
                }
                _write_new(attempt_dir / "export.stdout.bin", export_result.stdout)
                export_stdout["persisted"] = True
                _write_new(attempt_dir / "export.stderr.bin", export_result.stderr)
                export_stderr["persisted"] = True
                detail = export_result.stderr.decode(errors="replace").strip()
                if export_result.interruption:
                    disposition = "interrupted"
                    interruption = export_result.interruption
                    error_body = {
                        "type": "CommandInterrupted",
                        "message": (
                            "Ask publication export interrupted: "
                            f"{detail or export_result.interruption}"
                        ),
                        "stage": "export",
                    }
                elif export_result.exit_code != 0:
                    disposition = "export_error"
                    error_body = {
                        "type": "CommandError",
                        "message": (
                            f"Ask publication export failed: {detail or export_result.exit_code}"
                        ),
                        "stage": "export",
                    }
                else:
                    export_body = _result_object(export_result.stdout)
                    export_path = Path(_string(export_body.get("output"), name="Ask export output"))
                    if export_path.parent.resolve() != export_dir.resolve():
                        raise AttemptError("Ask export path escaped its attempt directory")
                    export = verify_export(
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
        "export_command": export_command,
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
    runtime_service_probe: RuntimeServiceProbe = _probe_live_runtime_service,
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
            runtime_service_probe=runtime_service_probe,
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
    runtime_service_probe: RuntimeServiceProbe = _probe_live_runtime_service,
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
        runtime_service_probe=runtime_service_probe,
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
