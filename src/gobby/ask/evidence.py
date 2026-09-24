"""Run-scoped durable admission for gcode evidence calls."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from gobby.ask.artifacts import AskArtifactStore
from gobby.ask.contracts import EvidenceReference, RetrievalMode
from gobby.ask.errors import EvidenceAdmissionError
from gobby.ask.evidence_authority import load_evidence_authority
from gobby.ask.snapshots import SnapshotIndexRuntime
from gobby.ask.storage import AskRunStorage
from gobby.storage.hub.operation_deadline import database_operation_deadline
from gobby.utils import spawn
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
_KNOWN_CREDENTIALS = (
    _URI_PASSWORD,
    # Anchored: without the boundary this matches inside ordinary repository
    # identifiers such as "ask-snapshot-preparation" and
    # "task-memory-review-after-close".
    re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{15,}"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{22,})"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bbearer\s+[A-Za-z0-9._-]{16,}", re.IGNORECASE),
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|secret|token|password|passwd)[\"']?\s*[:=]\s*"
    r"([\"'`]?[^\s\"'`]{12,})"
)
_TERMINAL_CHECKPOINT_SECONDS = 2.0


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _content_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _redact_output(value: str) -> str:
    return _URI_PASSWORD.sub(r"\1<redacted>\2", redact_terminal_output(value))


def _sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_output(value)
    if isinstance(value, dict):
        return {str(key): _sanitize(child) for key, child in value.items()}
    if isinstance(value, list):
        return [_sanitize(child) for child in value]
    if isinstance(value, tuple):
        return tuple(_sanitize(child) for child in value)
    return value


def _credential_text(value: str, *, source_references: bool = False) -> bool:
    if any(pattern.search(value) for pattern in _KNOWN_CREDENTIALS):
        return True
    return any(
        not source_references
        or match.group(1).startswith(('"', "'", "`"))
        or match.group(1)[0].isdigit()
        for match in _CREDENTIAL_ASSIGNMENT.finditer(value)
    )


def _contains_credential(value: object, *, source_references: bool = False) -> bool:
    if isinstance(value, str):
        return _credential_text(value, source_references=source_references)
    if isinstance(value, Mapping):
        return any(
            _credential_text(str(key))
            or (
                key not in {"path", "paths", "representatives", "excerpt", "numbered_excerpt"}
                and _contains_credential(child, source_references=key == "query")
            )
            for key, child in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return any(_contains_credential(child) for child in value)
    return False


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
        if not permitted_operations or not permitted_operations <= {
            "search",
            "read",
            "graph",
            "communities",
        }:
            raise ValueError("unsupported evidence operation policy")

        self.run_id = run_id
        self.permitted_operations = frozenset(permitted_operations)
        self.page_size = page_size
        self.artifacts = artifacts
        self.storage = storage
        self.runtime = runtime
        (
            self.source_root,
            self.snapshot_binding,
            self.retrieval_mode,
            self.deadline_at,
            self.executable,
            self.argv_prefix,
            self.env,
        ) = load_evidence_authority(self.run_id, runtime, self.artifacts, self.storage)

    def _refresh_authority(self) -> None:
        remaining = self._remaining_seconds()
        with database_operation_deadline(
            timeout_seconds=remaining,
            operation_timeout_seconds=remaining,
        ):
            (
                self.source_root,
                self.snapshot_binding,
                self.retrieval_mode,
                self.deadline_at,
                self.executable,
                self.argv_prefix,
                self.env,
            ) = load_evidence_authority(self.run_id, self.runtime, self.artifacts, self.storage)

    def _remaining_seconds(self, deadline_at: datetime | None = None) -> float:
        deadline = deadline_at or self.deadline_at
        remaining = (deadline.astimezone(UTC) - datetime.now(UTC)).total_seconds()
        if not math.isfinite(remaining) or remaining <= 0:
            raise TimeoutError("evidence deadline exceeded")
        return remaining

    async def _owned_thread(self, function: Any, /, *args: Any, **kwargs: Any) -> Any:
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            with contextlib.suppress(BaseException):
                await asyncio.shield(task)
            raise

    async def _write_artifact(
        self,
        kind: str,
        body: dict[str, Any],
        *,
        deadline_at: datetime | None = None,
    ) -> dict[str, Any]:
        deadline = deadline_at or self.deadline_at
        pointer: dict[str, Any] = await self._owned_thread(
            self.artifacts.write_body,
            kind,
            body,
            timeout_seconds=self._remaining_seconds(deadline),
        )
        self._remaining_seconds(deadline)
        return pointer

    async def _spawn_process(self, *argv: str, **kwargs: Any) -> Any:
        spawn_task = asyncio.create_task(spawn.create_subprocess_exec(*argv, **kwargs))
        try:
            async with asyncio.timeout(self._remaining_seconds()):
                return await asyncio.shield(spawn_task)
        except (asyncio.CancelledError, TimeoutError):
            spawn_task.cancel()
            process = None
            try:
                async with asyncio.timeout(_TERMINAL_CHECKPOINT_SECONDS):
                    process = await asyncio.shield(spawn_task)
            except (asyncio.CancelledError, TimeoutError):
                pass
            if process is not None:
                await self._terminate_process(process)
            raise

    @staticmethod
    async def _terminate_process(process: Any) -> tuple[bytes, bytes]:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
        try:
            async with asyncio.timeout(_TERMINAL_CHECKPOINT_SECONDS):
                output: tuple[bytes, bytes] = await process.communicate()
                return output
        except TimeoutError:
            return b"", b"process cleanup exceeded its bounded terminal window"

    async def query(
        self,
        operation: str,
        selector: Mapping[str, Any],
        *,
        continuation: str | None = None,
    ) -> dict[str, Any]:
        """Invoke one admitted gcode request and durably checkpoint its outcome."""
        try:
            self._refresh_authority()
        except TimeoutError:
            raise EvidenceAdmissionError("evidence deadline exceeded before invocation") from None
        invocation_id = str(uuid4())
        operation_body = self._normalize_selector(operation, selector)
        request: dict[str, Any] = {
            "schema_version": 1,
            "binding": self.snapshot_binding,
            "operation": operation,
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
        stored_request = _sanitize(request)
        stored_argv = _sanitize(argv)
        invocation_body = {
            "schema_version": 1,
            "run_id": self.run_id,
            "invocation_id": invocation_id,
            "operation": operation,
            "request": stored_request,
            "request_hash": _content_hash(request),
            "binding_digest": _content_hash(self.snapshot_binding),
            "retrieval_mode": self.retrieval_mode.value,
            "deadline_at": self.deadline_at.isoformat().replace("+00:00", "Z"),
            "argv": stored_argv,
        }
        try:
            invocation_pointer = await self._write_artifact(
                "evidence-invocation",
                invocation_body,
            )
        except TimeoutError:
            cleanup_deadline = datetime.now(UTC) + timedelta(seconds=_TERMINAL_CHECKPOINT_SECONDS)
            invocation_pointer = await self._write_artifact(
                "evidence-invocation",
                invocation_body,
                deadline_at=cleanup_deadline,
            )
            await self._checkpoint_timeout(
                operation,
                invocation_id,
                invocation_pointer,
                request,
                stored_argv,
                deadline_at=cleanup_deadline,
            )
            raise EvidenceAdmissionError(
                "evidence deadline exceeded during invocation publication"
            ) from None

        try:
            self._admit(operation, operation_body)
        except EvidenceAdmissionError as error:
            result = {
                "schema_version": 1,
                "run_id": self.run_id,
                "invocation_id": invocation_id,
                "status": "denied",
                "argv": stored_argv,
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
                stored_argv,
            )
            raise EvidenceAdmissionError("evidence deadline exceeded before invocation")

        child_env = {
            key: value
            for key in ("PATH", "SYSTEMROOT")
            if (value := os.environ.get(key)) is not None
        }
        child_env.update(self.env)
        try:
            process = await self._spawn_process(
                *argv,
                cwd=self.source_root,
                env=child_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except TimeoutError:
            await self._checkpoint_timeout(
                operation,
                invocation_id,
                invocation_pointer,
                request,
                stored_argv,
            )
            raise EvidenceAdmissionError(
                "evidence deadline exceeded during process start"
            ) from None
        except OSError as error:
            result = {
                "schema_version": 1,
                "run_id": self.run_id,
                "invocation_id": invocation_id,
                "status": "failed",
                "argv": stored_argv,
                "error": {"code": "process_start_failed", "message": _redact_output(str(error))},
            }
            await self._checkpoint(
                operation=operation,
                invocation_id=invocation_id,
                invocation_pointer=invocation_pointer,
                request=request,
                result=result,
                deadline_at=datetime.now(UTC) + timedelta(seconds=_TERMINAL_CHECKPOINT_SECONDS),
            )
            raise EvidenceAdmissionError(
                "process_start_failed: gcode evidence did not start"
            ) from error
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), self._remaining_seconds()
            )
        except TimeoutError:
            stdout_bytes, stderr_bytes = await self._terminate_process(process)
            await self._checkpoint_timeout(
                operation,
                invocation_id,
                invocation_pointer,
                request,
                stored_argv,
                stdout=stdout_bytes.decode(errors="replace"),
                stderr=stderr_bytes.decode(errors="replace"),
            )
            raise EvidenceAdmissionError("evidence deadline exceeded during invocation") from None
        except asyncio.CancelledError:
            stdout_bytes, stderr_bytes = await self._terminate_process(process)
            result = {
                "schema_version": 1,
                "run_id": self.run_id,
                "invocation_id": invocation_id,
                "status": "cancelled",
                "argv": stored_argv,
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
                deadline_at=datetime.now(UTC) + timedelta(seconds=_TERMINAL_CHECKPOINT_SECONDS),
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
                "argv": stored_argv,
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
            message = error_body.get("message", "gcode evidence invocation failed")
            raise EvidenceAdmissionError(f"{code}: {message}")

        try:
            response = self._validate_response(request, json.loads(stdout))
        except (json.JSONDecodeError, EvidenceAdmissionError) as error:
            result = {
                "schema_version": 1,
                "run_id": self.run_id,
                "invocation_id": invocation_id,
                "status": "invalid_response",
                "argv": stored_argv,
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
            "argv": stored_argv,
            "returncode": process.returncode,
            "stderr": _redact_output(stderr),
            "response": response,
        }
        try:
            await self._checkpoint(
                operation=operation,
                invocation_id=invocation_id,
                invocation_pointer=invocation_pointer,
                request=request,
                result=result,
                response=response,
            )
        except TimeoutError:
            await self._checkpoint_timeout(
                operation,
                invocation_id,
                invocation_pointer,
                request,
                stored_argv,
            )
            raise EvidenceAdmissionError("evidence deadline exceeded during publication") from None
        return response

    @classmethod
    def _normalize_selector(
        cls,
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
            value = cls._without_none(value)
            if operation == "search":
                value.setdefault("paths", [])
                value.setdefault("limit", 1000)
            elif operation == "graph":
                value.setdefault("depth", 3)
                value.setdefault("relations", [])
                value.setdefault("limit", 1000)
            elif operation == "communities":
                if len({"community_id", "label", "path"} & value.keys()) > 1:
                    raise EvidenceAdmissionError(
                        "select a community by at most one of community_id, label, or path"
                    )
                value.setdefault("min_size", 2)
                value.setdefault("max_members", 50)
            operation_body[operation] = value
        return operation_body

    def _admit(self, operation: str, selector: Mapping[str, Any]) -> None:
        if set(selector) != {operation} or not isinstance(selector.get(operation), dict):
            raise EvidenceAdmissionError("evidence selector does not match the requested operation")
        if operation not in self.permitted_operations:
            raise EvidenceAdmissionError(
                f"operation is not permitted for this Ask run: {operation}"
            )
        if _contains_credential(selector):
            raise EvidenceAdmissionError("credential-bearing evidence selector is not admissible")
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
        deadline_at: datetime | None = None,
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
            deadline_at=deadline_at
            or datetime.now(UTC) + timedelta(seconds=_TERMINAL_CHECKPOINT_SECONDS),
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
        deadline_at: datetime | None = None,
    ) -> None:
        deadline = deadline_at or self.deadline_at
        stored_result: dict[str, Any] = _sanitize(
            {key: value for key, value in result.items() if key != "response"}
        )
        if response is not None:
            # Keep indexed source bytes exact so persisted citation hashes remain valid.
            stored_result["response"] = response
        result_pointer = await self._write_artifact(
            "evidence-result",
            stored_result,
            deadline_at=deadline,
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
            binding_digest=_content_hash(self.snapshot_binding),
            contract=contract,
            usage=usage,
        )
        await self._owned_thread(
            self.storage.append_evidence_reference,
            self.run_id,
            reference,
            deadline_at=deadline,
        )
        self._remaining_seconds(deadline)

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
        if _contains_credential(response):
            raise EvidenceAdmissionError("response contains credential-bearing content")
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
