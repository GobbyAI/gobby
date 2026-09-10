"""Pipeline-backed Ask run persistence."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg

from gobby.ask.contracts import (
    AskBinding,
    AskRequest,
    AskRunRecord,
    EvidenceReference,
    ProfileSnapshot,
    SnapshotGeneration,
)
from gobby.storage.hub.operation_deadline import database_operation_deadline
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.workflows.agent_resolver import resolve_agent_with_row
from gobby.workflows.pipeline_state import ExecutionStatus


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _run_git(project_root: Path, *arguments: str, timeout: float) -> str:
    env = {
        key: value for key in ("PATH", "SYSTEMROOT") if (value := os.environ.get(key)) is not None
    }
    env.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    completed = subprocess.run(
        [
            "git",
            "--no-replace-objects",
            "--literal-pathspecs",
            "-c",
            "core.useReplaceRefs=false",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "diff.external=",
            "-C",
            str(project_root),
            *arguments,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    return completed.stdout.strip()


def _remaining_seconds(cutoff: float) -> float:
    remaining = cutoff - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Ask start deadline exceeded")
    return remaining


def _remaining_until(deadline_at: datetime) -> float:
    if deadline_at.tzinfo is None:
        raise ValueError("Ask operation deadline must be timezone-aware")
    remaining = (deadline_at.astimezone(UTC) - datetime.now(UTC)).total_seconds()
    if not math.isfinite(remaining) or remaining <= 0:
        raise TimeoutError("Ask operation deadline exceeded")
    return remaining


class AskRunStorage:
    def __init__(
        self,
        manager: LocalPipelineExecutionManager,
        *,
        pipeline_name: str = "native-ask",
        pipeline_snapshot: dict[str, Any] | None = None,
        commit_resolver: Callable[[Path, str, float], tuple[str, str]] | None = None,
        profile_resolver: Callable[[str, float], ProfileSnapshot] | None = None,
        now: Callable[[], datetime] | None = None,
        session_id: str | None = None,
    ) -> None:
        self.manager = manager
        self.pipeline_name = pipeline_name
        self.pipeline_snapshot = pipeline_snapshot or {"name": pipeline_name}
        self.commit_resolver = commit_resolver
        self.profile_resolver = profile_resolver
        self.now = now or (lambda: datetime.now(UTC))
        self.session_id = session_id

    def start(self, request: AskRequest, project_root: Path) -> AskRunRecord:
        """Create or find the immutable start checkpoint for one Ask run."""
        if self.manager.project_id is not None and request.project_id != self.manager.project_id:
            raise ValueError("Ask request project does not match pipeline storage scope")
        admitted_at = time.monotonic()
        started_at = self.now()
        if started_at.tzinfo is None:
            raise ValueError("Ask clock must return a timezone-aware timestamp")
        deadline_at = started_at.astimezone(UTC) + timedelta(seconds=request.timeout_seconds)
        cutoff = admitted_at + request.timeout_seconds
        request_body = request.model_dump(mode="json")
        fingerprint_body = dict(request_body)
        fingerprint_body.pop("idempotency_key", None)
        request_fingerprint = _fingerprint(fingerprint_body)

        with database_operation_deadline(
            timeout_seconds=_remaining_seconds(cutoff),
            operation_timeout_seconds=_remaining_seconds(cutoff),
        ):
            with self.manager.db.transaction() as connection:
                if request.idempotency_key is not None:
                    lock_key = (
                        f"ask:{request.project_id}:{self.pipeline_name}:{request.idempotency_key}"
                    )
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,)
                    )
                    row = connection.execute(
                        """
                        SELECT id, inputs_json
                        FROM pipeline_executions
                        WHERE project_id = %s
                          AND pipeline_name = %s
                          AND inputs_json::jsonb #>> '{ask,idempotency_key}' = %s
                        ORDER BY created_at, id
                        LIMIT 1
                        """,
                        (request.project_id, self.pipeline_name, request.idempotency_key),
                    ).fetchone()
                    if row is not None:
                        inputs = self._decode_inputs(row["inputs_json"])
                        if inputs["request_fingerprint"] != request_fingerprint:
                            raise ValueError(
                                "idempotency key was already used for a different request"
                            )
                        return self._record_from_inputs(str(row["id"]), inputs)

                _remaining_seconds(cutoff)
                if self.commit_resolver is None:
                    commit_oid, tree_oid = self._resolve_commit(
                        project_root,
                        request.commit_ref,
                        timeout=_remaining_seconds(cutoff),
                    )
                else:
                    commit_oid, tree_oid = self.commit_resolver(
                        project_root,
                        request.commit_ref,
                        _remaining_seconds(cutoff),
                    )
                _remaining_seconds(cutoff)
                investigator = self._with_content_hash(
                    self._resolve_requested_profile(
                        request.investigator_profile,
                        request.project_id,
                        timeout=_remaining_seconds(cutoff),
                    )
                )
                _remaining_seconds(cutoff)
                reviewer = self._with_content_hash(
                    self._resolve_requested_profile(
                        request.reviewer_profile,
                        request.project_id,
                        timeout=_remaining_seconds(cutoff),
                    )
                )
                _remaining_seconds(cutoff)
                binding = AskBinding(
                    project_id=request.project_id,
                    commit_oid=commit_oid,
                    tree_oid=tree_oid,
                    deadline_at=deadline_at,
                    retrieval_mode=request.retrieval_mode,
                )
                ask_inputs = {
                    "schema_version": 1,
                    "idempotency_key": request.idempotency_key,
                    "request_fingerprint": request_fingerprint,
                    "request": request_body,
                    "binding": binding.model_dump(mode="json"),
                    "investigator": investigator.model_dump(mode="json"),
                    "reviewer": reviewer.model_dump(mode="json"),
                    "runtime": {},
                }
                execution = self.manager.create_execution(
                    pipeline_name=self.pipeline_name,
                    inputs_json=_canonical_json({"ask": ask_inputs}),
                    definition_json=_canonical_json(self.pipeline_snapshot),
                    project_id=request.project_id,
                    session_id=self.session_id,
                )
                _remaining_seconds(cutoff)
                return AskRunRecord(
                    run_id=execution.id,
                    request=request,
                    binding=binding,
                    investigator=investigator,
                    reviewer=reviewer,
                )

    def bind_execution_context(
        self,
        run_id: str,
        *,
        project_root: Path,
        caller_session_id: str,
    ) -> dict[str, Any]:
        """Bind the executor inputs that must survive detached restart recovery."""
        root = str(project_root.resolve())
        with self.manager.db.transaction() as connection:
            row = connection.execute(
                """
                SELECT inputs_json, project_id FROM pipeline_executions
                WHERE id = %s AND pipeline_name = %s
                FOR UPDATE
                """,
                (run_id, self.pipeline_name),
            ).fetchone()
            if row is None or (
                self.manager.project_id is not None
                and str(row["project_id"]) != self.manager.project_id
            ):
                raise ValueError(f"Ask run not found: {run_id}")
            document = self._decode_document(row["inputs_json"])
            ask_inputs = self._narrow_ask(document)
            context = {
                "run_id": run_id,
                "project_id": str(row["project_id"]),
                "project_root": root,
                "caller_session_id": caller_session_id,
            }
            existing = ask_inputs.get("execution_context")
            if existing is not None and existing != context:
                raise ValueError("Ask execution context is immutable")
            ask_inputs["execution_context"] = context
            document.update(context)
            document["ask"] = ask_inputs
            connection.execute(
                "UPDATE pipeline_executions SET inputs_json = %s, updated_at = NOW() WHERE id = %s",
                (_canonical_json(document), run_id),
            )
        return document

    def execution_inputs(self, run_id: str) -> dict[str, Any]:
        """Return the complete persisted inputs supplied to PipelineExecutor."""
        execution = self.manager.get_execution(run_id)
        if execution is None:
            raise ValueError(f"Ask run not found: {run_id}")
        document = self._decode_document(execution.inputs_json)
        ask_inputs = self._narrow_ask(document)
        context = ask_inputs.get("execution_context")
        if not isinstance(context, dict):
            raise RuntimeError("Ask execution context is not bound")
        for key in ("run_id", "project_id", "project_root", "caller_session_id"):
            if document.get(key) != context.get(key):
                raise RuntimeError("Ask executor inputs differ from immutable context")
        return document

    def claim_restart(self, run_id: str, *, project_id: str, owner_id: str) -> bool:
        """Claim one pending/running native Ask row for this daemon instance."""
        with self.manager.db.transaction() as connection:
            row = connection.execute(
                """
                SELECT status, inputs_json, project_id FROM pipeline_executions
                WHERE id = %s AND project_id = %s AND pipeline_name = %s
                FOR UPDATE
                """,
                (run_id, project_id, self.pipeline_name),
            ).fetchone()
            if row is None or (
                self.manager.project_id is not None
                and str(row["project_id"]) != self.manager.project_id
            ):
                raise ValueError(f"Ask run not found: {run_id}")
            status = ExecutionStatus(str(row["status"]))
            if status not in {ExecutionStatus.PENDING, ExecutionStatus.RUNNING}:
                raise ValueError(f"Ask run cannot be recovered from {status.value}")
            document = self._decode_document(row["inputs_json"])
            ask_inputs = self._narrow_ask(document)
            context = ask_inputs.get("execution_context")
            if not isinstance(context, dict):
                raise RuntimeError("Ask execution context is not bound")
            for key in ("run_id", "project_id", "project_root", "caller_session_id"):
                if document.get(key) != context.get(key):
                    raise RuntimeError("Ask executor inputs differ from immutable context")
            runtime = self._runtime(ask_inputs)
            existing = runtime.get("restart_claim")
            if existing is not None and not isinstance(existing, dict):
                raise RuntimeError("Ask restart claim is invalid")
            if isinstance(existing, dict) and existing.get("owner_id") == owner_id:
                return False
            runtime["restart_claim"] = {
                "owner_id": owner_id,
                "execution_status": status.value,
                "claimed_at": datetime.now(UTC).isoformat(),
            }
            ask_inputs["runtime"] = runtime
            document["ask"] = ask_inputs
            connection.execute(
                """
                UPDATE pipeline_executions SET inputs_json = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (_canonical_json(document), run_id),
            )
        return True

    def get(self, run_id: str) -> AskRunRecord | None:
        execution = self.manager.get_execution(run_id)
        if execution is None:
            return None
        return self._record_from_inputs(run_id, self._decode_inputs(execution.inputs_json))

    def attach_snapshot(
        self,
        run_id: str,
        *,
        inventory_digest: str,
        snapshot_artifact: dict[str, Any],
        deadline_at: datetime,
    ) -> AskRunRecord:
        """Persist the prepared source snapshot before an investigator is admitted."""
        remaining = _remaining_until(deadline_at)
        with (
            database_operation_deadline(
                timeout_seconds=remaining,
                operation_timeout_seconds=remaining,
            ),
            self.manager.db.transaction() as connection,
        ):
            row = connection.execute(
                """
                SELECT inputs_json, project_id FROM pipeline_executions
                WHERE id = %s AND pipeline_name = %s
                FOR UPDATE
                """,
                (run_id, self.pipeline_name),
            ).fetchone()
            if row is None or (
                self.manager.project_id is not None
                and str(row["project_id"]) != self.manager.project_id
            ):
                raise ValueError(f"Ask run not found: {run_id}")
            if snapshot_artifact.get("project_id") != str(row["project_id"]) or (
                snapshot_artifact.get("run_id") != run_id
            ):
                raise ValueError("Ask snapshot artifact does not belong to this run")
            document = json.loads(row["inputs_json"] or "{}")
            ask_inputs = self._narrow_ask(document)
            binding = dict(ask_inputs["binding"])
            existing_digest = binding.get("inventory_digest")
            existing_artifact = binding.get("snapshot_artifact")
            if existing_digest not in {None, inventory_digest} or (
                existing_artifact is not None and existing_artifact != snapshot_artifact
            ):
                raise ValueError("Ask run snapshot binding is immutable")
            binding["inventory_digest"] = inventory_digest
            binding["snapshot_artifact"] = snapshot_artifact
            ask_inputs["binding"] = binding
            document["ask"] = ask_inputs
            connection.execute(
                "UPDATE pipeline_executions SET inputs_json = %s, updated_at = NOW() WHERE id = %s",
                (_canonical_json(document), run_id),
            )
        return self._record_from_inputs(run_id, ask_inputs)

    def publish_snapshot_generation(
        self,
        run_id: str,
        *,
        generation: int,
        lifecycle_artifact: dict[str, Any],
        expected_previous_generation: int | None,
        deadline_at: datetime,
    ) -> SnapshotGeneration:
        """Atomically advance the authoritative current snapshot lifecycle pointer."""
        candidate = SnapshotGeneration(
            generation=generation,
            lifecycle_artifact=lifecycle_artifact,
        )
        remaining = _remaining_until(deadline_at)
        with (
            database_operation_deadline(
                timeout_seconds=remaining,
                operation_timeout_seconds=remaining,
            ),
            self.manager.db.transaction() as connection,
        ):
            row = connection.execute(
                """
                SELECT inputs_json, project_id
                FROM pipeline_executions
                WHERE id = %s AND pipeline_name = %s
                FOR UPDATE
                """,
                (run_id, self.pipeline_name),
            ).fetchone()
            if row is None or (
                self.manager.project_id is not None
                and str(row["project_id"]) != self.manager.project_id
            ):
                raise ValueError(f"Ask run not found: {run_id}")
            if lifecycle_artifact.get("project_id") != str(row["project_id"]) or (
                lifecycle_artifact.get("run_id") != run_id
            ):
                raise ValueError("Ask lifecycle artifact does not belong to this run")
            document = self._decode_document(row["inputs_json"])
            ask_inputs = self._narrow_ask(document)
            runtime = self._runtime(ask_inputs)
            current_body = runtime.get("snapshot")
            current = (
                SnapshotGeneration.model_validate(current_body)
                if current_body is not None
                else None
            )
            current_generation = current.generation if current is not None else None
            if current_generation != expected_previous_generation:
                raise ValueError("Ask snapshot lifecycle generation changed concurrently")
            expected_generation = 1 if current_generation is None else current_generation + 1
            if generation != expected_generation:
                raise ValueError("Ask snapshot lifecycle generation must advance exactly once")
            runtime["snapshot"] = candidate.model_dump(mode="json")
            ask_inputs["runtime"] = runtime
            document["ask"] = ask_inputs
            connection.execute(
                "UPDATE pipeline_executions SET inputs_json = %s, updated_at = NOW() WHERE id = %s",
                (_canonical_json(document), run_id),
            )
        return candidate

    def get_snapshot_generation(self, run_id: str) -> SnapshotGeneration | None:
        execution = self.manager.get_execution(run_id)
        if execution is None:
            return None
        document = self._decode_document(execution.inputs_json)
        runtime = self._runtime(self._narrow_ask(document))
        if runtime.get("snapshot") is None:
            return None
        return SnapshotGeneration.model_validate(runtime["snapshot"])

    def append_evidence_reference(
        self,
        run_id: str,
        reference: EvidenceReference,
        *,
        deadline_at: datetime,
    ) -> None:
        """Serialize one evidence checkpoint through the pipeline row transaction."""
        remaining = (deadline_at.astimezone(UTC) - datetime.now(UTC)).total_seconds()
        if not math.isfinite(remaining) or remaining <= 0:
            raise TimeoutError("Ask evidence checkpoint deadline exceeded")
        try:
            with (
                database_operation_deadline(
                    timeout_seconds=remaining,
                    operation_timeout_seconds=remaining,
                ),
                self.manager.db.transaction() as connection,
            ):
                row = connection.execute(
                    """
                    SELECT inputs_json, project_id FROM pipeline_executions
                    WHERE id = %s AND pipeline_name = %s
                    FOR UPDATE
                    """,
                    (run_id, self.pipeline_name),
                ).fetchone()
                if row is None or (
                    self.manager.project_id is not None
                    and str(row["project_id"]) != self.manager.project_id
                ):
                    raise ValueError(f"Ask run not found: {run_id}")
                document = self._decode_document(row["inputs_json"])
                ask_inputs = self._narrow_ask(document)
                runtime = self._runtime(ask_inputs)
                evidence = runtime.setdefault("evidence", [])
                if not isinstance(evidence, list):
                    raise RuntimeError(f"invalid Ask evidence checkpoint for run {run_id}")
                body = reference.model_dump(mode="json")
                if not any(
                    item.get("invocation_id") == reference.invocation_id for item in evidence
                ):
                    evidence.append(body)
                    ask_inputs["runtime"] = runtime
                    document["ask"] = ask_inputs
                    connection.execute(
                        """
                        UPDATE pipeline_executions
                        SET inputs_json = %s, updated_at = NOW()
                        WHERE id = %s
                        """,
                        (_canonical_json(document), run_id),
                    )
        except (psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable) as error:
            raise TimeoutError("Ask evidence checkpoint deadline exceeded") from error

    def evidence_references(self, run_id: str) -> list[EvidenceReference]:
        execution = self.manager.get_execution(run_id)
        if execution is None:
            return []
        document = self._decode_document(execution.inputs_json)
        runtime = self._runtime(self._narrow_ask(document))
        raw = runtime.get("evidence", [])
        if not isinstance(raw, list):
            raise RuntimeError("invalid persisted Ask evidence references")
        return [EvidenceReference.model_validate(item) for item in raw]

    def _resolve_commit(
        self, project_root: Path, commit_ref: str, *, timeout: float
    ) -> tuple[str, str]:
        cutoff = time.monotonic() + timeout
        commit_oid = _run_git(
            project_root,
            "rev-parse",
            "--verify",
            f"{commit_ref}^{{commit}}",
            timeout=_remaining_seconds(cutoff),
        )
        tree_oid = _run_git(
            project_root,
            "rev-parse",
            "--verify",
            f"{commit_oid}^{{tree}}",
            timeout=_remaining_seconds(cutoff),
        )
        return commit_oid, tree_oid

    def _resolve_requested_profile(
        self,
        identifier: str,
        project_id: str,
        *,
        timeout: float,
    ) -> ProfileSnapshot:
        if self.profile_resolver is not None:
            return self.profile_resolver(identifier, timeout)
        with database_operation_deadline(
            timeout_seconds=timeout,
            operation_timeout_seconds=timeout,
        ):
            resolved = resolve_agent_with_row(
                identifier,
                self.manager.db,
                project_id=project_id,
            )
        if resolved is None:
            raise ValueError(f"Ask agent profile not found or invalid: {identifier}")
        effective, row = resolved
        body = effective.model_dump(mode="json")
        return ProfileSnapshot(
            identifier=identifier,
            definition_id=row.id,
            definition_updated_at=row.updated_at.isoformat(),
            effective=body,
            content_hash=_fingerprint(body),
        )

    @staticmethod
    def _with_content_hash(snapshot: ProfileSnapshot) -> ProfileSnapshot:
        if snapshot.content_hash is not None:
            return snapshot
        return snapshot.model_copy(update={"content_hash": _fingerprint(snapshot.effective)})

    @classmethod
    def _decode_inputs(cls, inputs_json: str | None) -> dict[str, Any]:
        document = cls._decode_document(inputs_json)
        return cls._narrow_ask(document)

    @staticmethod
    def _decode_document(inputs_json: str | None) -> dict[str, Any]:
        document = json.loads(inputs_json or "{}")
        if not isinstance(document, dict):
            raise RuntimeError("pipeline execution inputs are invalid")
        return document

    @staticmethod
    def _narrow_ask(document: object) -> dict[str, Any]:
        if not isinstance(document, dict) or not isinstance(document.get("ask"), dict):
            raise RuntimeError("pipeline execution does not contain an Ask start checkpoint")
        return dict(document["ask"])

    @staticmethod
    def _runtime(ask_inputs: dict[str, Any]) -> dict[str, Any]:
        runtime = ask_inputs.get("runtime", {})
        if not isinstance(runtime, dict):
            raise RuntimeError("Ask runtime metadata is invalid")
        return dict(runtime)

    @staticmethod
    def _record_from_inputs(run_id: str, inputs: dict[str, Any]) -> AskRunRecord:
        return AskRunRecord(
            run_id=run_id,
            request=AskRequest.model_validate(inputs["request"]),
            binding=AskBinding.model_validate(inputs["binding"]),
            investigator=ProfileSnapshot.model_validate(inputs["investigator"]),
            reviewer=ProfileSnapshot.model_validate(inputs["reviewer"]),
        )
