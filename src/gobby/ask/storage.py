"""Pipeline-backed Ask run persistence."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from gobby.ask.contracts import (
    AskBinding,
    AskRequest,
    AskRunRecord,
    EvidenceReference,
    ProfileSnapshot,
)
from gobby.storage.pipelines import LocalPipelineExecutionManager
from gobby.workflows.agent_resolver import resolve_agent_with_row


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _run_git(project_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=project_root,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.strip()


class AskRunStorage:
    def __init__(
        self,
        manager: LocalPipelineExecutionManager,
        *,
        pipeline_name: str = "native-ask",
        pipeline_snapshot: dict[str, Any] | None = None,
        commit_resolver: Callable[[Path, str], tuple[str, str]] | None = None,
        profile_resolver: Callable[[str], ProfileSnapshot] | None = None,
        now: Callable[[], datetime] | None = None,
        session_id: str | None = None,
    ) -> None:
        self.manager = manager
        self.pipeline_name = pipeline_name
        self.pipeline_snapshot = pipeline_snapshot or {"name": pipeline_name}
        self.commit_resolver = commit_resolver or self._resolve_commit
        self.profile_resolver = profile_resolver
        self.now = now or (lambda: datetime.now(UTC))
        self.session_id = session_id

    def start(self, request: AskRequest, project_root: Path) -> AskRunRecord:
        """Create or find the immutable start checkpoint for one Ask run."""
        if self.manager.project_id is not None and request.project_id != self.manager.project_id:
            raise ValueError("Ask request project does not match pipeline storage scope")
        request_body = request.model_dump(mode="json")
        fingerprint_body = dict(request_body)
        fingerprint_body.pop("idempotency_key", None)
        request_fingerprint = _fingerprint(fingerprint_body)

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
                        raise ValueError("idempotency key was already used for a different request")
                    return self._record_from_inputs(str(row["id"]), inputs)

            commit_oid, tree_oid = self.commit_resolver(project_root, request.commit_ref)
            investigator = self._with_content_hash(
                self._resolve_requested_profile(request.investigator_profile, request.project_id)
            )
            reviewer = self._with_content_hash(
                self._resolve_requested_profile(request.reviewer_profile, request.project_id)
            )
            started_at = self.now()
            if started_at.tzinfo is None:
                raise ValueError("Ask clock must return a timezone-aware timestamp")
            deadline_at = started_at.astimezone(UTC) + timedelta(seconds=request.timeout_seconds)
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
            }
            execution = self.manager.create_execution(
                pipeline_name=self.pipeline_name,
                inputs_json=_canonical_json({"ask": ask_inputs}),
                definition_json=_canonical_json(self.pipeline_snapshot),
                project_id=request.project_id,
                session_id=self.session_id,
            )
            return AskRunRecord(
                run_id=execution.id,
                request=request,
                binding=binding,
                investigator=investigator,
                reviewer=reviewer,
            )

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
    ) -> AskRunRecord:
        """Persist the prepared source snapshot before an investigator is admitted."""
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

    def append_evidence_reference(self, run_id: str, reference: EvidenceReference) -> None:
        """Serialize one evidence checkpoint through the pipeline row transaction."""
        with self.manager.db.transaction() as connection:
            row = connection.execute(
                """
                SELECT outputs_json, project_id FROM pipeline_executions
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
            outputs = json.loads(row["outputs_json"] or "{}")
            if not isinstance(outputs, dict):
                raise RuntimeError(f"invalid pipeline outputs for Ask run {run_id}")
            ask_output = outputs.setdefault("ask", {})
            if not isinstance(ask_output, dict):
                raise RuntimeError(f"invalid Ask outputs for run {run_id}")
            evidence = ask_output.setdefault("evidence", [])
            if not isinstance(evidence, list):
                raise RuntimeError(f"invalid Ask evidence checkpoint for run {run_id}")
            body = reference.model_dump(mode="json")
            if not any(item.get("invocation_id") == reference.invocation_id for item in evidence):
                evidence.append(body)
                connection.execute(
                    "UPDATE pipeline_executions SET outputs_json = %s, updated_at = NOW() WHERE id = %s",
                    (_canonical_json(outputs), run_id),
                )

    def _resolve_commit(self, project_root: Path, commit_ref: str) -> tuple[str, str]:
        commit_oid = _run_git(project_root, "rev-parse", "--verify", f"{commit_ref}^{{commit}}")
        tree_oid = _run_git(project_root, "rev-parse", "--verify", f"{commit_oid}^{{tree}}")
        return commit_oid, tree_oid

    def _resolve_requested_profile(self, identifier: str, project_id: str) -> ProfileSnapshot:
        if self.profile_resolver is not None:
            return self.profile_resolver(identifier)
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
        document = json.loads(inputs_json or "{}")
        return cls._narrow_ask(document)

    @staticmethod
    def _narrow_ask(document: object) -> dict[str, Any]:
        if not isinstance(document, dict) or not isinstance(document.get("ask"), dict):
            raise RuntimeError("pipeline execution does not contain an Ask start checkpoint")
        return dict(document["ask"])

    @staticmethod
    def _record_from_inputs(run_id: str, inputs: dict[str, Any]) -> AskRunRecord:
        return AskRunRecord(
            run_id=run_id,
            request=AskRequest.model_validate(inputs["request"]),
            binding=AskBinding.model_validate(inputs["binding"]),
            investigator=ProfileSnapshot.model_validate(inputs["investigator"]),
            reviewer=ProfileSnapshot.model_validate(inputs["reviewer"]),
        )
