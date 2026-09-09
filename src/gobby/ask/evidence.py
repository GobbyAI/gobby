"""Run-scoped durable admission for gcode evidence calls."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from gobby.ask.artifacts import AskArtifactStore
from gobby.ask.contracts import EvidenceReference, RetrievalMode
from gobby.ask.snapshots import SnapshotIndexRuntime
from gobby.ask.storage import AskRunStorage
from gobby.utils.terminal_output import redact_terminal_output

_MAX_PAGE_SIZE = 1024 * 1024
_DETERMINISTIC_SEARCH_LANES = {"symbol", "literal", "regex", "content", "lexical_symbol"}
_COMPLETE_STATES = {"complete", "complete_empty", "excluded_scope"}
_INCOMPLETE_STATES = {"paginated", "truncated_index", "truncated_traversal"}
_SENSITIVE_NAMES = {
    ".env",
    ".git",
    ".gobby",
    "credentials",
    "credential",
    "secrets",
    "secret",
    "private_key",
    "id_rsa",
    "id_ed25519",
}
_SENSITIVE_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
_URI_PASSWORD = re.compile(r"([a-z][a-z0-9+.-]*://[^:/\s]+:)[^@\s]+(@)", re.IGNORECASE)


class EvidenceAdmissionError(RuntimeError):
    """Evidence invocation failed admission, execution, or contract validation."""


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _content_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _redact_output(value: str) -> str:
    return _URI_PASSWORD.sub(r"\1<redacted>\2", redact_terminal_output(value))


class EvidenceAdmission:
    """Immutable policy and subprocess boundary for one Ask run."""

    def __init__(
        self,
        *,
        run_id: str,
        runtime: SnapshotIndexRuntime,
        permitted_operations: set[str],
        page_size: int,
        artifacts: AskArtifactStore,
        storage: AskRunStorage,
    ) -> None:
        if not 0 < page_size <= _MAX_PAGE_SIZE:
            raise ValueError(f"evidence page_size must be between 1 and {_MAX_PAGE_SIZE}")
        if not permitted_operations or not permitted_operations <= {"search", "read", "graph"}:
            raise ValueError("unsupported evidence operation policy")

        self.run_id = run_id
        self.permitted_operations = frozenset(permitted_operations)
        self.page_size = page_size
        self.artifacts = artifacts
        self.storage = storage
        (
            self.source_root,
            self.snapshot_binding,
            self.retrieval_mode,
            self.deadline_at,
            self.executable,
            self.argv_prefix,
            self.env,
        ) = self._load_authority(runtime)

    def _load_authority(
        self,
        runtime: SnapshotIndexRuntime,
    ) -> tuple[
        Path,
        dict[str, Any],
        RetrievalMode,
        datetime,
        Path,
        tuple[str, ...],
        dict[str, str],
    ]:
        if self.artifacts.run_id != self.run_id:
            raise EvidenceAdmissionError("artifact store does not belong to the Ask run")
        record = self.storage.get(self.run_id)
        if record is None:
            raise EvidenceAdmissionError(f"Ask run does not exist: {self.run_id}")
        if self.artifacts.project_id != record.binding.project_id:
            raise EvidenceAdmissionError("artifact store does not belong to the Ask project")
        current = self.storage.get_snapshot_generation(self.run_id)
        if current is None:
            raise EvidenceAdmissionError("Ask run has no current snapshot generation")
        if record.binding.snapshot_artifact is None or record.binding.inventory_digest is None:
            raise EvidenceAdmissionError("Ask run has no persisted snapshot identity")
        try:
            self.artifacts.verify_manifest()
            lifecycle = self.artifacts.read_body(current.lifecycle_artifact)
        except (OSError, ValueError, RuntimeError) as error:
            raise EvidenceAdmissionError(
                "current snapshot lifecycle artifact is invalid"
            ) from error

        deadline_at = record.binding.deadline_at
        if deadline_at.tzinfo is None:
            raise EvidenceAdmissionError("persisted evidence deadline is not timezone-aware")
        deadline_at = deadline_at.astimezone(UTC)
        remaining = (deadline_at - datetime.now(UTC)).total_seconds()
        if not math.isfinite(remaining):
            raise EvidenceAdmissionError("persisted evidence deadline is not finite")
        expected = {
            "generation": current.generation,
            "run_id": record.run_id,
            "project_id": record.binding.project_id,
            "commit_oid": record.binding.commit_oid,
            "deadline_at": deadline_at.isoformat().replace("+00:00", "Z"),
            "retrieval_mode": record.binding.retrieval_mode.value,
            "inventory_digest": record.binding.inventory_digest,
            "snapshot_artifact": record.binding.snapshot_artifact,
        }
        for key, value in expected.items():
            if lifecycle.get(key) != value:
                raise EvidenceAdmissionError(
                    f"current snapshot lifecycle {key} differs from the persisted Ask run"
                )
        try:
            identity = self.artifacts.read_body(record.binding.snapshot_artifact)
        except (OSError, ValueError, RuntimeError) as error:
            raise EvidenceAdmissionError(
                "persisted snapshot identity artifact is invalid"
            ) from error
        for key in ("run_id", "project_id", "commit_oid", "deadline_at", "retrieval_mode"):
            if identity.get(key) != expected[key]:
                raise EvidenceAdmissionError(
                    f"snapshot identity {key} differs from the persisted Ask run"
                )
        binding = identity.get("binding")
        inventory = identity.get("inventory")
        if not isinstance(binding, dict) or not isinstance(inventory, dict):
            raise EvidenceAdmissionError("snapshot identity is missing its native payload")
        if binding.get("project_id") != record.binding.project_id or (
            binding.get("commit_oid") != record.binding.commit_oid
        ):
            raise EvidenceAdmissionError(
                "native snapshot binding differs from the persisted Ask run"
            )
        if binding.get("inventory_digest") != record.binding.inventory_digest or (
            inventory.get("digest") != record.binding.inventory_digest
        ):
            raise EvidenceAdmissionError("native snapshot inventory identity changed")

        source_value = lifecycle.get("source_root")
        runtime_value = lifecycle.get("index_runtime")
        if not isinstance(source_value, str) or not isinstance(runtime_value, dict):
            raise EvidenceAdmissionError("current snapshot lifecycle is incomplete")
        source_root = Path(source_value).resolve()
        expected_source = (self.artifacts.run_root / "source").resolve()
        if source_root != expected_source or not source_root.is_dir():
            raise EvidenceAdmissionError("current snapshot source root is unavailable")
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
        return (
            source_root,
            json.loads(_canonical_json(binding)),
            record.binding.retrieval_mode,
            deadline_at,
            executable,
            argv_prefix,
            env,
        )

    async def query(
        self,
        operation: str,
        selector: Mapping[str, Any],
        *,
        continuation: str | None = None,
    ) -> dict[str, Any]:
        """Invoke one admitted gcode request and durably checkpoint its outcome."""
        invocation_id = str(uuid4())
        operation_body = self._normalize_selector(operation, selector)
        request: dict[str, Any] = {
            "schema_version": 1,
            "binding": self.snapshot_binding,
            **operation_body,
            "max_bytes": self.page_size,
        }
        if continuation is not None:
            if not continuation:
                raise EvidenceAdmissionError("evidence continuation must not be empty")
            request["continuation"] = continuation
        argv = [
            str(self.executable),
            *self.argv_prefix,
            "--quiet",
            "--format",
            "json",
            "--project",
            str(self.source_root),
            "evidence",
            "--request-json",
            _canonical_json(request),
        ]
        invocation_body = {
            "schema_version": 1,
            "run_id": self.run_id,
            "invocation_id": invocation_id,
            "operation": operation,
            "request": request,
            "request_hash": _content_hash(request),
            "snapshot_inventory_digest": self.snapshot_binding["inventory_digest"],
            "retrieval_mode": self.retrieval_mode.value,
            "deadline_at": self.deadline_at.isoformat().replace("+00:00", "Z"),
            "argv": argv,
        }
        invocation_pointer = await asyncio.to_thread(
            self.artifacts.write_body,
            "evidence-invocation",
            invocation_body,
        )

        try:
            self._admit(operation, operation_body)
        except EvidenceAdmissionError as error:
            result = {
                "schema_version": 1,
                "run_id": self.run_id,
                "invocation_id": invocation_id,
                "status": "denied",
                "argv": argv,
                "error": {"code": "policy_denied", "message": str(error)},
            }
            await self._checkpoint(
                operation=operation,
                invocation_id=invocation_id,
                invocation_pointer=invocation_pointer,
                request=request,
                result=result,
            )
            raise

        remaining = (self.deadline_at - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            await self._checkpoint_timeout(
                operation,
                invocation_id,
                invocation_pointer,
                request,
                argv,
            )
            raise EvidenceAdmissionError("evidence deadline exceeded before invocation")

        child_env = {
            key: value
            for key in ("PATH", "SYSTEMROOT")
            if (value := os.environ.get(key)) is not None
        }
        child_env.update(self.env)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self.source_root,
                env=child_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as error:
            result = {
                "schema_version": 1,
                "run_id": self.run_id,
                "invocation_id": invocation_id,
                "status": "failed",
                "argv": argv,
                "error": {"code": "process_start_failed", "message": _redact_output(str(error))},
            }
            await self._checkpoint(
                operation=operation,
                invocation_id=invocation_id,
                invocation_pointer=invocation_pointer,
                request=request,
                result=result,
            )
            raise EvidenceAdmissionError(
                "process_start_failed: gcode evidence did not start"
            ) from error
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), remaining)
        except TimeoutError:
            process.kill()
            stdout_bytes, stderr_bytes = await process.communicate()
            await self._checkpoint_timeout(
                operation,
                invocation_id,
                invocation_pointer,
                request,
                argv,
                stdout=stdout_bytes.decode(errors="replace"),
                stderr=stderr_bytes.decode(errors="replace"),
            )
            raise EvidenceAdmissionError("evidence deadline exceeded during invocation") from None
        except asyncio.CancelledError:
            process.kill()
            stdout_bytes, stderr_bytes = await process.communicate()
            result = {
                "schema_version": 1,
                "run_id": self.run_id,
                "invocation_id": invocation_id,
                "status": "cancelled",
                "argv": argv,
                "returncode": process.returncode,
                "stdout": _redact_output(stdout_bytes.decode(errors="replace")),
                "stderr": _redact_output(stderr_bytes.decode(errors="replace")),
            }
            await self._checkpoint(
                operation=operation,
                invocation_id=invocation_id,
                invocation_pointer=invocation_pointer,
                request=request,
                result=result,
            )
            raise

        stdout = stdout_bytes.decode(errors="replace")
        stderr = stderr_bytes.decode(errors="replace")
        if process.returncode != 0:
            error_body = self._parse_error(stderr)
            result = {
                "schema_version": 1,
                "run_id": self.run_id,
                "invocation_id": invocation_id,
                "status": "failed",
                "argv": argv,
                "returncode": process.returncode,
                "stdout": _redact_output(stdout),
                "stderr": _redact_output(stderr),
                "error": error_body,
            }
            await self._checkpoint(
                operation=operation,
                invocation_id=invocation_id,
                invocation_pointer=invocation_pointer,
                request=request,
                result=result,
            )
            code = error_body.get("code", "gcode_failed")
            raise EvidenceAdmissionError(f"{code}: gcode evidence invocation failed")

        try:
            response = self._validate_response(request, json.loads(stdout))
        except (json.JSONDecodeError, EvidenceAdmissionError) as error:
            result = {
                "schema_version": 1,
                "run_id": self.run_id,
                "invocation_id": invocation_id,
                "status": "invalid_response",
                "argv": argv,
                "returncode": process.returncode,
                "stdout": _redact_output(stdout),
                "stderr": _redact_output(stderr),
                "error": {"code": "invalid_response", "message": str(error)},
            }
            await self._checkpoint(
                operation=operation,
                invocation_id=invocation_id,
                invocation_pointer=invocation_pointer,
                request=request,
                result=result,
            )
            raise EvidenceAdmissionError(f"invalid gcode evidence response: {error}") from error

        result = {
            "schema_version": 1,
            "run_id": self.run_id,
            "invocation_id": invocation_id,
            "status": "succeeded",
            "argv": argv,
            "returncode": process.returncode,
            "stderr": _redact_output(stderr),
            "response": response,
        }
        await self._checkpoint(
            operation=operation,
            invocation_id=invocation_id,
            invocation_pointer=invocation_pointer,
            request=request,
            result=result,
            response=response,
        )
        return response

    def _normalize_selector(
        self,
        operation: str,
        selector: Mapping[str, Any],
    ) -> dict[str, Any]:
        if operation in selector:
            parsed: object = json.loads(_canonical_json(selector))
        else:
            selector_body: object = json.loads(_canonical_json(selector))
            parsed = {operation: selector_body}
        if not isinstance(parsed, dict):
            raise EvidenceAdmissionError("evidence selector must be a JSON object")
        operation_body = dict(parsed)
        value = operation_body.get(operation)
        if isinstance(value, dict):
            value = self._without_none(value)
            if operation == "search":
                value.setdefault("paths", [])
                value.setdefault("limit", 1000)
            elif operation == "graph":
                value.setdefault("depth", 3)
                value.setdefault("relations", [])
                value.setdefault("limit", 1000)
            operation_body[operation] = value
        return operation_body

    def _admit(self, operation: str, selector: Mapping[str, Any]) -> None:
        if set(selector) != {operation} or not isinstance(selector.get(operation), dict):
            raise EvidenceAdmissionError("evidence selector does not match the requested operation")
        if operation not in self.permitted_operations:
            raise EvidenceAdmissionError(
                f"operation is not permitted for this Ask run: {operation}"
            )
        search = selector.get("search")
        if self.retrieval_mode is RetrievalMode.DETERMINISTIC and isinstance(search, dict):
            lane = search.get("lane")
            if lane not in _DETERMINISTIC_SEARCH_LANES:
                raise EvidenceAdmissionError(f"search lane is not deterministic: {lane}")
        for path in self._iter_paths(selector):
            self._validate_path(path)

    @classmethod
    def _without_none(cls, value: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, child in value.items():
            if child is None:
                continue
            if isinstance(child, Mapping):
                result[key] = cls._without_none(child)
            elif isinstance(child, list):
                result[key] = [
                    cls._without_none(item) if isinstance(item, Mapping) else item for item in child
                ]
            else:
                result[key] = child
        return result

    @classmethod
    def _iter_paths(cls, value: object, parent_key: str | None = None) -> list[str]:
        paths: list[str] = []
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key == "path" and isinstance(child, str):
                    paths.append(child)
                elif key == "paths" and isinstance(child, Sequence) and not isinstance(child, str):
                    paths.extend(item for item in child if isinstance(item, str))
                else:
                    paths.extend(cls._iter_paths(child, key))
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for child in value:
                paths.extend(cls._iter_paths(child, parent_key))
        return paths

    def _validate_path(self, path: str) -> None:
        parsed = PurePosixPath(path)
        if (
            not path
            or "\\" in path
            or parsed.is_absolute()
            or any(part in {"", ".", ".."} for part in parsed.parts)
        ):
            raise EvidenceAdmissionError(f"path escapes the snapshot root: {path}")
        lowered_parts = [part.casefold() for part in parsed.parts]
        name = lowered_parts[-1]
        if (
            any(part in _SENSITIVE_NAMES or part.startswith(".env.") for part in lowered_parts)
            or PurePosixPath(name).suffix in _SENSITIVE_SUFFIXES
        ):
            raise EvidenceAdmissionError(f"sensitive path is not admissible: {path}")
        candidate = (self.source_root / Path(*parsed.parts)).resolve()
        if not candidate.is_relative_to(self.source_root):
            raise EvidenceAdmissionError(f"path escapes the snapshot root: {path}")

    async def _checkpoint_timeout(
        self,
        operation: str,
        invocation_id: str,
        invocation_pointer: dict[str, Any],
        request: dict[str, Any],
        argv: list[str],
        *,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        result = {
            "schema_version": 1,
            "run_id": self.run_id,
            "invocation_id": invocation_id,
            "status": "timeout",
            "argv": argv,
            "stdout": _redact_output(stdout),
            "stderr": _redact_output(stderr),
            "error": {"code": "deadline_exceeded", "message": "evidence deadline exceeded"},
        }
        await self._checkpoint(
            operation=operation,
            invocation_id=invocation_id,
            invocation_pointer=invocation_pointer,
            request=request,
            result=result,
        )

    async def _checkpoint(
        self,
        *,
        operation: str,
        invocation_id: str,
        invocation_pointer: dict[str, Any],
        request: dict[str, Any],
        result: dict[str, Any],
        response: dict[str, Any] | None = None,
    ) -> None:
        result_pointer = await asyncio.to_thread(
            self.artifacts.write_body,
            "evidence-result",
            result,
        )
        items = response.get("items", []) if response is not None else []
        evidence_ids = tuple(
            item["evidence_id"]
            for item in items
            if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)
        )
        usage = response.get("usage") if response is not None else None
        if not isinstance(usage, dict):
            usage = None
        contract = response.get("contract") if response is not None else None
        if not isinstance(contract, dict):
            contract = None
        reference = EvidenceReference(
            invocation_id=invocation_id,
            operation=operation,
            status=str(result["status"]),
            invocation_artifact=invocation_pointer,
            result_artifact=result_pointer,
            evidence_ids=evidence_ids,
            request_hash=_content_hash(request),
            response_hash=_content_hash(response) if response is not None else None,
            snapshot_inventory_digest=str(self.snapshot_binding["inventory_digest"]),
            contract=contract,
            usage=usage,
        )
        await asyncio.to_thread(self.storage.append_evidence_reference, self.run_id, reference)

    def _validate_response(
        self,
        request: dict[str, Any],
        response: object,
    ) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise EvidenceAdmissionError("response must be a JSON object")
        if response.get("request") != request:
            canonical_request = dict(request)
            canonical_request.pop("continuation", None)
            if response.get("request") != canonical_request:
                raise EvidenceAdmissionError("response request identity does not match invocation")
        if response.get("binding") != self.snapshot_binding:
            raise EvidenceAdmissionError("response snapshot binding does not match Ask run")
        contract = response.get("contract")
        if not isinstance(contract, dict) or (
            contract.get("name") != "gcode-evidence"
            or contract.get("schema_version") != 1
            or contract.get("tool") != "gobby-code"
        ):
            raise EvidenceAdmissionError("response contract identity is unsupported")
        response_request = response["request"]
        request_fingerprint = response.get("request_fingerprint")
        expected_fingerprint = hashlib.sha256(
            json.dumps(response_request, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()
        if request_fingerprint != expected_fingerprint:
            raise EvidenceAdmissionError("response request fingerprint does not match identity")
        items = response.get("items")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise EvidenceAdmissionError("response items must be JSON objects")
        evidence_ids = [item.get("evidence_id") for item in items]
        if any(not isinstance(value, str) or not value for value in evidence_ids):
            raise EvidenceAdmissionError("response item is missing evidence identity")
        if len(set(evidence_ids)) != len(evidence_ids):
            raise EvidenceAdmissionError("response evidence identities are not unique")
        complete = response.get("complete")
        completeness = response.get("completeness")
        continuation = response.get("continuation")
        if complete is True:
            if completeness not in _COMPLETE_STATES or continuation is not None:
                raise EvidenceAdmissionError(
                    "complete response has inconsistent completeness fields"
                )
        elif complete is False:
            if completeness not in _INCOMPLETE_STATES:
                raise EvidenceAdmissionError("partial response has an unsupported completeness")
            if completeness == "paginated":
                if not isinstance(continuation, str) or not continuation:
                    raise EvidenceAdmissionError("paginated response lacks a valid continuation")
            elif continuation is not None:
                raise EvidenceAdmissionError("terminal partial response has a continuation")
        else:
            raise EvidenceAdmissionError("response complete field must be boolean")
        bounds = response.get("bounds")
        if not isinstance(bounds, dict):
            raise EvidenceAdmissionError("response is missing applied bounds")
        if bounds.get("max_bytes") != self.page_size or bounds.get("returned_items") != len(items):
            raise EvidenceAdmissionError("response applied bounds do not match the request")
        serialized_bytes = bounds.get("serialized_item_bytes")
        if not isinstance(serialized_bytes, int) or not 0 <= serialized_bytes <= self.page_size:
            raise EvidenceAdmissionError("response serialized byte bound is invalid")
        total_items = bounds.get("total_items")
        if not isinstance(total_items, int) or total_items < len(items):
            raise EvidenceAdmissionError("response total item bound is invalid")
        return response

    @staticmethod
    def _parse_error(stderr: str) -> dict[str, Any]:
        redacted = _redact_output(stderr)
        try:
            value = json.loads(redacted)
        except json.JSONDecodeError:
            return {"code": "gcode_failed", "message": redacted.strip() or "gcode failed"}
        if not isinstance(value, dict):
            return {"code": "gcode_failed", "message": redacted.strip() or "gcode failed"}
        code = value.pop("error", value.pop("code", "gcode_failed"))
        return {"code": str(code), **value}
