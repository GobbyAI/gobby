"""Durable preparation and lifecycle service for plan-review evidence."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from gobby.code_index.storage import CodeIndexStorage
from gobby.plans.parser import PlanDocument
from gobby.plans.plan_roots import resolve_plan_root
from gobby.plans.review_checkpoint_service import ReviewCheckpointService
from gobby.plans.review_coverage import (
    review_complexity,
    validate_review_coverage,
)
from gobby.plans.review_evidence_io import (
    atomic_write_bytes,
    build_section_manifest,
    normalize_plan_path,
    parse_checkpoints,
    parse_plan_bytes,
    reviewed_section_hashes,
)
from gobby.plans.review_evidence_models import (
    PlanReviewEvidence,
    PreparedReviewEvidence,
    ReviewEvidenceError,
)
from gobby.plans.review_evidence_store import PlanReviewEvidenceStore
from gobby.plans.review_findings import validate_plan_review_findings
from gobby.plans.review_manifest_service import ReviewManifestService
from gobby.plans.review_repairs import apply_repairs, select_accepted_repairs
from gobby.storage.agents import LocalAgentRunManager
from gobby.storage.hub.protocol import (
    HubDatabase,
    PlanReviewEvidenceMutation,
)
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import LIVE_SESSION_STATUSES
from gobby.workflows.state_manager import SessionVariableManager

EVIDENCE_LEASE_SECONDS = 7_200


class PlanReviewEvidenceService:
    """Coordinate immutable snapshots with durable evidence lifecycle state."""

    def __init__(self, db: HubDatabase) -> None:
        # LocalTaskManager imports review transitions, which import this service.
        # Keep this delayed to break that cycle during module initialization.
        from gobby.storage.tasks import LocalTaskManager

        self.db = db
        self.store = PlanReviewEvidenceStore(db)
        self.projects = LocalProjectManager(db)
        self.tasks = LocalTaskManager(db)
        self.session_variables = SessionVariableManager(db)
        self.agent_runs = LocalAgentRunManager(db)
        self.checkpoints = ReviewCheckpointService(db=db, store=self.store)
        self.manifests = ReviewManifestService(
            db=db,
            store=self.store,
            projects=self.projects,
        )

    def prepare_plan_review_round(
        self,
        *,
        project_id: str,
        plan_path: str | Path,
        round_number: int,
        session_id: str | None = None,
        task_id: str | None = None,
        stage: str | None = None,
    ) -> PreparedReviewEvidence:
        """Capture one immutable round snapshot under a per-plan mutation lock."""
        if round_number <= 0:
            raise ReviewEvidenceError(
                "invalid_round_number",
                "round_number must be a positive integer",
            )
        self._validate_attempt_binding(
            session_id=session_id,
            task_id=task_id,
            stage=stage,
        )
        project = self.projects.get(project_id)
        if project is None:
            raise ReviewEvidenceError(
                "project_not_found",
                f"project has no local repository: {project_id}",
            )
        root = resolve_plan_root(self.db, project_id, plan_path)
        resolved = normalize_plan_path(root, plan_path)
        relative_path = resolved.relative_to(root).as_posix()
        mutation = PlanReviewEvidenceMutation(
            project_id=project_id,
            plan_path=relative_path,
        )
        prepared: PreparedReviewEvidence | None = None
        pending_payload: list[dict[str, object]] = []
        with self.db.transaction_immediate(mutation) as transaction:
            snapshot = resolved.read_bytes()
            checkpoints = parse_checkpoints(snapshot)
            self.checkpoints.reconcile_checkpoints(
                transaction=transaction,
                project_id=project_id,
                plan_path=relative_path,
                checkpoints=checkpoints,
            )
            active = self.store.active_for_path(
                project_id=project_id,
                plan_path=relative_path,
                transaction=transaction,
            )
            if (
                active is not None
                and active.is_interactive
                and not active.is_static_bound
                and active.round_result is not None
            ):
                self.checkpoints.drain_interactive_intent(
                    transaction=transaction,
                    evidence=active,
                    plan_path=resolved,
                    verify_plan_unchanged=self.verify_plan_unchanged,
                )
                snapshot = resolved.read_bytes()
                checkpoints = parse_checkpoints(snapshot)
                active = self.store.active_for_path(
                    project_id=project_id,
                    plan_path=relative_path,
                    transaction=transaction,
                )
            if session_id is not None:
                pending_mints = self.store.pending_interactive_mints(
                    project_id=project_id,
                    plan_path=relative_path,
                    session_id=session_id,
                    transaction=transaction,
                )
                if pending_mints:
                    pending_payload = [
                        {
                            "evidence_id": row.evidence_id,
                            "round_number": row.round_number,
                            "round_result": row.round_result,
                        }
                        for row in pending_mints
                    ]
            if not pending_payload:
                if active is not None:
                    if self._matches_attempt(
                        active,
                        round_number=round_number,
                        session_id=session_id,
                        task_id=task_id,
                        stage=stage,
                        source_plan_path=str(resolved),
                        snapshot=snapshot,
                    ):
                        prepared = active.prepared_result()
                    elif self._attempt_is_dead(active):
                        self.store.expire(
                            transaction=transaction,
                            evidence_id=active.evidence_id,
                        )
                        active = None
                if active is not None and prepared is None:
                    raise ReviewEvidenceError(
                        "review_round_active",
                        f"another plan review round is active for {relative_path}",
                        retryable=True,
                        details={"evidence_id": active.evidence_id},
                    )
                if prepared is None:
                    prepared = next(
                        (
                            evidence.prepared_result()
                            for evidence in self.store.list_for_path(
                                project_id=project_id,
                                plan_path=relative_path,
                                transaction=transaction,
                                for_update=True,
                            )
                            if evidence.expired_at is None
                            and self._matches_attempt(
                                evidence,
                                round_number=round_number,
                                session_id=session_id,
                                task_id=task_id,
                                stage=stage,
                                source_plan_path=str(resolved),
                                snapshot=snapshot,
                            )
                        ),
                        None,
                    )
                if prepared is None:
                    self._validate_review_snapshot(snapshot, resolved, root, project_id)
                    plan_hash = hashlib.sha256(snapshot).hexdigest()
                    sections = build_section_manifest(snapshot)
                    evidence = self.store.insert(
                        transaction=transaction,
                        project_id=project_id,
                        plan_path=relative_path,
                        source_plan_path=str(resolved),
                        plan_hash=plan_hash,
                        sections=sections,
                        snapshot=snapshot,
                        round_number=round_number,
                        lease_seconds=EVIDENCE_LEASE_SECONDS,
                        session_id=session_id,
                        task_id=task_id,
                        stage=stage,
                    )
                    prepared = evidence.prepared_result()
        if pending_payload:
            raise ReviewEvidenceError(
                "pending_lesson_mint",
                "interactive approval lessons must be checkpointed before another round",
                details={"pending": pending_payload},
            )
        if prepared is None:  # pragma: no cover - guarded by the branches above.
            raise RuntimeError("plan review preparation produced no result")
        return prepared

    def _validate_review_snapshot(
        self,
        snapshot: bytes,
        plan_path: Path,
        root: Path,
        project_id: str,
    ) -> None:
        """Gate evidence creation on the same immutable bytes the reviewer receives."""
        from gobby.tasks.expansion._validate import validate_plan_file
        from gobby.utils.project_context import get_project_context

        document = parse_plan_bytes(plan_path.name, snapshot)
        context = get_project_context(root)
        with self.db.transaction() as transaction:
            # Roll back failed index reads so they cannot poison evidence writes (#23296).
            savepoint = transaction.savepoint("plan_review_snapshot_validation")
            result = validate_plan_file(
                None,
                plan_path,
                plan_document=document,
                project_context=context,
                expected_project_id=project_id,
                code_index=CodeIndexStorage(self.db),
                task_manager=self.tasks,
                require_symbol_validation=bool(document.manifest_entries),
            )
            try:
                savepoint.release()
            except psycopg.errors.InFailedSqlTransaction:
                savepoint.rollback()
        if not result["valid"]:
            raise ReviewEvidenceError(
                "plan_validation_failed",
                "Plan must pass deterministic validation before review evidence is created",
                details=result,
            )

    def get_evidence(self, evidence_id: str) -> PlanReviewEvidence:
        return self.store.require(evidence_id)

    def snapshot_bytes(self, evidence_id: str) -> bytes:
        return self.get_evidence(evidence_id).snapshot

    def snapshot_payload(self, evidence_id: str) -> dict[str, object]:
        evidence = self.get_evidence(evidence_id)
        document = self._snapshot_document(evidence)
        changed_sections = self._changed_sections_since_prior_round(evidence)
        return {
            "evidence_id": evidence.evidence_id,
            "plan_hash": evidence.plan_hash,
            "sections": [section.to_dict() for section in evidence.section_manifest],
            "snapshot": evidence.snapshot,
            "changed_section_ids": changed_sections,
            "review_complexity": review_complexity(
                document,
                changed_section_count=len(changed_sections),
            ),
        }

    def derive_plan_review_manifest(
        self,
        evidence_id: str,
        routing_decisions: Mapping[str, object],
    ) -> dict[str, object]:
        return self.manifests.derive_plan_review_manifest(
            evidence_id,
            routing_decisions,
        )

    def validate_plan_review_coverage(
        self,
        evidence_id: str,
        lane_results: list[object],
        candidate_dispositions: Mapping[str, object],
        shadow_manifest_status: Mapping[str, object],
    ) -> dict[str, object]:
        """Validate all research lanes and return a canonical coverage attestation."""
        evidence = self.get_evidence(evidence_id)
        routing_raw = shadow_manifest_status.get("routing_decisions")
        if not isinstance(routing_raw, Mapping):
            raise ReviewEvidenceError(
                "invalid_shadow_manifest",
                "shadow_manifest_status.routing_decisions must be an object",
            )
        routing = dict(routing_raw)
        expected_shadow = self.derive_plan_review_manifest(evidence_id, routing)
        project = self.projects.get(evidence.project_id)
        if project is None:
            raise ReviewEvidenceError(
                "project_not_found",
                f"project has no local repository: {evidence.project_id}",
            )
        return validate_review_coverage(
            evidence_id=evidence_id,
            project_root=resolve_plan_root(self.db, evidence.project_id, evidence.plan_path),
            document=self._snapshot_document(evidence),
            plan_hash=evidence.plan_hash,
            lane_results=lane_results,
            candidate_dispositions=candidate_dispositions,
            shadow_manifest_status=shadow_manifest_status,
            expected_shadow_manifest_status=expected_shadow,
        )

    def verify_plan_unchanged(
        self,
        evidence_id: str,
        plan_path: str | Path,
    ) -> bool:
        evidence = self.get_evidence(evidence_id)
        resolved = self.manifests.resolve_evidence_path(evidence, plan_path)
        self._verify_reviewed_bytes(evidence, resolved.read_bytes())
        return True

    @staticmethod
    def _verify_reviewed_bytes(
        evidence: PlanReviewEvidence,
        current_bytes: bytes,
    ) -> None:
        current = reviewed_section_hashes(build_section_manifest(current_bytes))
        captured = reviewed_section_hashes(evidence.section_manifest)
        if current != captured:
            changed = sorted(
                key for key in set(current) | set(captured) if current.get(key) != captured.get(key)
            )
            raise ReviewEvidenceError(
                "stale_plan_evidence",
                f"reviewed plan sections changed: {', '.join(changed)}",
                details={"changed_sections": changed},
            )

    def bind_evidence_run(self, evidence_id: str, run_id: str) -> PlanReviewEvidence:
        evidence = self.get_evidence(evidence_id)
        run = self.agent_runs.get(run_id)
        if run is None:
            raise ReviewEvidenceError("run_not_found", f"agent run not found: {run_id}")
        if evidence.is_interactive:
            valid_lineage = run.parent_session_id == evidence.session_id and run.task_id is None
        else:
            valid_lineage = run.task_id == evidence.task_id
        if not valid_lineage:
            if (
                evidence.dispatch_run_id is None
                and not evidence.is_static_bound
                and evidence.is_live
            ):
                self.expire_plan_review_evidence(evidence_id, spawn_failed=True)
                if run.status in {"pending", "running"}:
                    self.agent_runs.cancel(
                        run.id,
                        result="plan review evidence bind failed",
                    )
            raise ReviewEvidenceError(
                "run_lineage_mismatch",
                "agent run does not belong to the evidence attempt",
            )
        if run.status not in {"pending", "running"}:
            raise ReviewEvidenceError(
                "run_not_active",
                f"agent run is already terminal: {run_id}",
            )
        mutation = PlanReviewEvidenceMutation(
            project_id=evidence.project_id,
            plan_path=evidence.plan_path,
        )
        try:
            with self.db.transaction_immediate(mutation) as transaction:
                return self.store.bind_run(
                    transaction=transaction,
                    evidence_id=evidence_id,
                    run_id=run_id,
                )
        except ReviewEvidenceError:
            current = self.get_evidence(evidence_id)
            if run.status in {"pending", "running"}:
                self.agent_runs.cancel(
                    run.id,
                    result="plan review evidence bind failed",
                )
            if current.dispatch_run_id is None and not current.is_static_bound and current.is_live:
                self.expire_plan_review_evidence(evidence_id, spawn_failed=True)
            raise

    def bind_static_review_seats(
        self,
        evidence_id: str,
        *,
        writer_session_id: str,
        coordinator_session_id: str,
        caller_session_id: str | None,
        plan_path: str | Path | None = None,
    ) -> PlanReviewEvidence:
        """Bind one live interactive attempt to three exact, persisted session seats."""
        evidence = self.get_evidence(evidence_id)
        if caller_session_id is None or caller_session_id != evidence.session_id:
            raise ReviewEvidenceError("unauthorized_seat", "only the evidence owner may bind seats")
        if not evidence.is_interactive:
            raise ReviewEvidenceError(
                "not_interactive_evidence", "static seats require an interactive round"
            )
        try:
            writer_session_id = str(uuid.UUID(writer_session_id))
            coordinator_session_id = str(uuid.UUID(coordinator_session_id))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ReviewEvidenceError(
                "invalid_seats", "review seats require session UUIDs"
            ) from exc
        mutation = PlanReviewEvidenceMutation(
            project_id=evidence.project_id,
            plan_path=evidence.plan_path,
        )
        with self.db.transaction_immediate(mutation) as transaction:
            locked = self.store.require(evidence_id, transaction=transaction, for_update=True)
            if not locked.is_live:
                raise ReviewEvidenceError("evidence_replay", "evidence row is no longer live")
            if locked.is_static_bound:
                if (
                    locked.static_writer_session_id == writer_session_id
                    and locked.static_coordinator_session_id == coordinator_session_id
                ):
                    return locked
                raise ReviewEvidenceError("evidence_already_bound", "static seats are immutable")
            if locked.dispatch_run_id is not None:
                raise ReviewEvidenceError("evidence_already_bound", "evidence has a run binding")
            active = self.store.active_for_path(
                project_id=locked.project_id,
                plan_path=locked.plan_path,
                transaction=transaction,
            )
            if active is None or active.evidence_id != evidence_id:
                raise ReviewEvidenceError(
                    "wrong_attempt", "evidence is not the current review attempt"
                )
            seats = {caller_session_id, writer_session_id, coordinator_session_id}
            if len(seats) != 3:
                raise ReviewEvidenceError("invalid_seats", "review seats must be distinct sessions")
            rows = transaction.execute(
                "SELECT id, project_id, status FROM sessions "
                "WHERE id IN (%s, %s, %s) ORDER BY id FOR UPDATE",
                (caller_session_id, writer_session_id, coordinator_session_id),
            ).fetchall()
            if len(rows) != 3 or any(
                str(row["project_id"]) != locked.project_id
                or row["status"] not in LIVE_SESSION_STATUSES
                for row in rows
            ):
                raise ReviewEvidenceError(
                    "invalid_seats", "review seats must be live sessions in the project"
                )
            source_path = plan_path or locked.source_plan_path
            if source_path is None or (
                locked.source_plan_path is None and not Path(source_path).is_absolute()
            ):
                raise ReviewEvidenceError(
                    "invalid_plan_path", "legacy evidence requires an absolute plan_path"
                )
            resolved = self.manifests.resolve_evidence_path(locked, source_path)
            if resolved.read_bytes() != locked.snapshot:
                raise ReviewEvidenceError(
                    "stale_snapshot", "plan bytes differ from reviewed evidence"
                )
            return self.store.bind_static_seats(
                transaction=transaction,
                evidence_id=evidence_id,
                writer_session_id=writer_session_id,
                coordinator_session_id=coordinator_session_id,
                source_plan_path=str(resolved),
            )

    def expire_plan_review_evidence(
        self,
        evidence_id: str,
        *,
        spawn_failed: bool = False,
        caller_session_id: str | None = None,
    ) -> PlanReviewEvidence:
        evidence = self.get_evidence(evidence_id)
        mutation = PlanReviewEvidenceMutation(
            project_id=evidence.project_id,
            plan_path=evidence.plan_path,
        )
        with self.db.transaction_immediate(mutation) as transaction:
            locked = self.store.require(evidence_id, transaction=transaction, for_update=True)
            if locked.is_static_bound:
                try:
                    caller_session_id = str(uuid.UUID(caller_session_id or ""))
                except (TypeError, ValueError) as exc:
                    raise ReviewEvidenceError(
                        "unauthorized_seat", "live project session required"
                    ) from exc
                seats = {
                    locked.session_id,
                    locked.static_writer_session_id,
                    locked.static_coordinator_session_id,
                }
                rows = transaction.execute(
                    "SELECT id, project_id, status FROM sessions "
                    "WHERE id IN (%s, %s, %s, %s) ORDER BY id FOR UPDATE",
                    (*seats, caller_session_id),
                ).fetchall()
                sessions = {str(row["id"]): row for row in rows}
                caller = sessions.get(caller_session_id) if caller_session_id is not None else None
                if (
                    caller is None
                    or str(caller["project_id"]) != locked.project_id
                    or caller["status"] not in LIVE_SESSION_STATUSES
                ):
                    raise ReviewEvidenceError("unauthorized_seat", "live project session required")
                abandoned = {
                    seat
                    for seat in seats
                    if seat is not None
                    and (
                        seat not in sessions
                        or sessions[seat]["status"] not in LIVE_SESSION_STATUSES
                    )
                }
                if not abandoned:
                    raise ReviewEvidenceError(
                        "attempt_still_live", "all static review seats are live", retryable=True
                    )
                if caller_session_id not in seats and abandoned != seats:
                    raise ReviewEvidenceError("unauthorized_seat", "bound seat required")
                # The successor round snapshots the current plan bytes; retain this
                # abandoned row and its checkpoint or manifest intent for audit.
            else:
                if locked.round_result is not None and locked.manifest_state != "revoked":
                    raise ReviewEvidenceError(
                        "durable_result_present",
                        "evidence with a durable round result must be reconciled",
                    )
                explicit_prebind_failure = spawn_failed and locked.dispatch_run_id is None
                if not explicit_prebind_failure and not self._attempt_is_dead(locked):
                    raise ReviewEvidenceError(
                        "attempt_still_live",
                        "evidence attempt is still live",
                        retryable=True,
                    )
            return self.store.expire(
                transaction=transaction,
                evidence_id=evidence_id,
            )

    def authorize_current_attempt(
        self,
        evidence_id: str,
        *,
        project_id: str,
        plan_path: str | Path,
        round_number: int,
        session_id: str | None = None,
        task_id: str | None = None,
        stage: str | None = None,
        run_id: str | None = None,
        caller_session_id: str | None = None,
        allow_rejection_replay: bool = False,
        allow_approval_replay: bool = False,
    ) -> PlanReviewEvidence:
        evidence = self.get_evidence(evidence_id)
        resolved, relative_path = self._resolve_plan_path(project_id, plan_path)
        token_matches = (
            evidence.project_id == project_id
            and evidence.plan_path == relative_path
            and (evidence.source_plan_path is None or str(resolved) == evidence.source_plan_path)
            and evidence.round_number == round_number
            and evidence.session_id == session_id
            and evidence.task_id == task_id
            and evidence.stage == stage
        )
        run_matches = evidence.dispatch_run_id == run_id and run_id is not None
        static_matches = (
            evidence.is_static_bound
            and run_id is None
            and caller_session_id is not None
            and caller_session_id == evidence.static_coordinator_session_id
        )
        binding_matches = run_matches or static_matches
        if (
            allow_rejection_replay
            and evidence.finalized_at is not None
            and evidence.round_result is not None
            and evidence.round_result.get("verdict") == "needs_review"
            and token_matches
            and binding_matches
        ):
            return evidence
        if (
            allow_approval_replay
            and evidence.finalized_at is not None
            and evidence.approval_result is not None
            and evidence.approval_result.get("verdict") == "approved"
            and token_matches
            and binding_matches
        ):
            return evidence
        if not token_matches:
            raise ReviewEvidenceError(
                "wrong_attempt",
                "evidence does not belong to the current review attempt",
            )
        if not evidence.is_live:
            raise ReviewEvidenceError("evidence_replay", "evidence row is no longer live")
        if evidence.is_static_bound:
            if not static_matches:
                raise ReviewEvidenceError("unauthorized_seat", "coordinator seat required")
            return evidence
        if evidence.dispatch_run_id is None:
            raise ReviewEvidenceError(
                "binding_pending",
                "evidence run binding is pending",
                retryable=True,
            )
        if not run_matches:
            raise ReviewEvidenceError(
                "wrong_attempt",
                "caller run does not own this evidence row",
            )
        return evidence

    def resolve_historical_proof(
        self,
        evidence_id: str,
        *,
        project_id: str,
        plan_path: str | Path,
        session_id: str | None = None,
        task_id: str | None = None,
    ) -> PlanReviewEvidence:
        evidence = self.get_evidence(evidence_id)
        _, relative_path = self._resolve_plan_path(project_id, plan_path)
        same_lineage = (
            session_id is not None
            and evidence.session_id == session_id
            and evidence.task_id is None
        ) or (task_id is not None and evidence.task_id == task_id and evidence.session_id is None)
        if (
            evidence.project_id != project_id
            or evidence.plan_path != relative_path
            or not same_lineage
        ):
            raise ReviewEvidenceError(
                "historical_lineage_mismatch",
                "evidence is outside the requested plan lineage",
            )
        if evidence.finalized_at is None:
            raise ReviewEvidenceError(
                "historical_proof_incomplete",
                "unfinalized evidence is not historical proof",
            )
        return evidence

    def render_plan_changelog_round(
        self,
        evidence_id: str,
        round_result: Mapping[str, object] | None = None,
    ) -> bytes:
        return self.checkpoints.render_plan_changelog_round(
            evidence_id,
            round_result,
        )

    def append_plan_changelog_round(
        self,
        evidence_id: str,
        prose: str,
        round_result: Mapping[str, object] | None = None,
        *,
        plan_path: str | Path | None = None,
        caller_session_id: str | None = None,
    ) -> dict[str, object]:
        evidence = self.get_evidence(evidence_id)
        if plan_path is None:
            resolved = self._evidence_path(evidence)
        else:
            resolved = self.manifests.resolve_evidence_path(evidence, plan_path)
        mutation = PlanReviewEvidenceMutation(
            project_id=evidence.project_id,
            plan_path=evidence.plan_path,
        )
        # The immediate mutation transaction serializes this read-modify-write
        # against concurrent plan writers (manifest apply, checkpoint drain).
        with self.db.transaction_immediate(mutation) as transaction:
            locked = self.store.require(evidence_id, transaction=transaction, for_update=True)
            if locked.expired_at is not None:
                raise ReviewEvidenceError("evidence_replay", "evidence is expired")
            if locked.is_static_bound:
                if (
                    caller_session_id is None
                    or caller_session_id != locked.static_writer_session_id
                ):
                    raise ReviewEvidenceError("unauthorized_seat", "writer seat required")
            elif locked.dispatch_run_id is None:
                raise ReviewEvidenceError(
                    "binding_pending",
                    "evidence run binding is pending",
                    retryable=True,
                )
            payload = locked.round_result
            if round_result is not None:
                payload = self._round_result_for_evidence(locked.evidence_id, round_result)
            if payload is None:
                raise ReviewEvidenceError(
                    "missing_round_result",
                    "round_result is required until a durable approval intent exists",
                )
            # Vote → repair → fence: accepted needs_review repairs change
            # reviewed sections, so only approved verdicts stay identity-checked.
            if payload.get("verdict") != "needs_review":
                self._verify_reviewed_bytes(locked, resolved.read_bytes())
            return self.checkpoints.append_plan_changelog_round(
                evidence_id,
                prose,
                payload,
                plan_path=resolved,
            )

    def finalize_plan_review_evidence(
        self,
        evidence_id: str,
        round_result: Mapping[str, object],
        *,
        caller_session_id: str | None = None,
    ) -> PlanReviewEvidence:
        evidence = self.get_evidence(evidence_id)
        if evidence.is_static_bound and (
            caller_session_id is None or caller_session_id != evidence.session_id
        ):
            raise ReviewEvidenceError("unauthorized_seat", "reviewer seat required")
        payload = self._round_result_for_evidence(evidence_id, round_result)
        if evidence.round_result is not None and evidence.round_result != payload:
            raise ReviewEvidenceError(
                "round_result_conflict",
                "supplied round result conflicts with durable evidence intent",
            )
        if evidence.finalized_at is not None:
            if evidence.round_result == payload:
                return evidence
            raise ReviewEvidenceError("evidence_replay", "evidence is already finalized")
        if evidence.expired_at is not None:
            raise ReviewEvidenceError("evidence_replay", "evidence is expired")
        mutation = PlanReviewEvidenceMutation(
            project_id=evidence.project_id,
            plan_path=evidence.plan_path,
        )
        with self.db.transaction_immediate(mutation) as transaction:
            current = self.store.require(evidence_id, transaction=transaction, for_update=True)
            if current.is_static_bound and caller_session_id != current.session_id:
                raise ReviewEvidenceError("unauthorized_seat", "reviewer seat required")
            if current.is_interactive:
                self.checkpoints.require_durable_checkpoint(
                    current,
                    payload,
                    plan_path=self._evidence_path(current),
                )
            return self.checkpoints.finalize_evidence(
                transaction=transaction,
                evidence=current,
                payload=payload,
            )

    def apply_plan_review_manifest(
        self,
        evidence_id: str,
        round_result: Mapping[str, object],
        *,
        plan_path: str | Path,
        run_id: str | None,
        caller_session_id: str | None = None,
    ) -> dict[str, object]:
        return self.manifests.apply_plan_review_manifest(
            evidence_id,
            round_result,
            plan_path=plan_path,
            run_id=run_id,
            caller_session_id=caller_session_id,
            resolve_round_result=self._round_result_for_evidence,
            authorize_attempt=self.authorize_current_attempt,
            verify_reviewed_bytes=self._verify_reviewed_bytes,
        )

    def apply_plan_review_repairs(
        self,
        evidence_id: str,
        accepted_finding_ids: Sequence[str],
        *,
        plan_path: str | Path | None = None,
        caller_session_id: str | None = None,
    ) -> dict[str, object]:
        """Apply the accepted findings' typed repairs to the plan under the evidence lock."""
        evidence = self.get_evidence(evidence_id)
        if plan_path is None:
            resolved = self._evidence_path(evidence)
        else:
            resolved = self.manifests.resolve_evidence_path(evidence, plan_path)
        if not evidence.is_interactive:
            raise ReviewEvidenceError(
                "not_interactive_evidence",
                "repairs apply only to interactive review evidence",
            )
        mutation = PlanReviewEvidenceMutation(
            project_id=evidence.project_id,
            plan_path=evidence.plan_path,
        )
        with self.db.transaction_immediate(mutation) as transaction:
            locked = self.store.require(evidence_id, transaction=transaction, for_update=True)
            if locked.is_static_bound and (
                caller_session_id is None or caller_session_id != locked.static_writer_session_id
            ):
                raise ReviewEvidenceError("unauthorized_seat", "writer seat required")
            if locked.finalized_at is None:
                raise ReviewEvidenceError(
                    "evidence_not_finalized",
                    "finalize the rejection checkpoint before applying repairs",
                )
            if locked.expired_at is not None:
                raise ReviewEvidenceError("evidence_replay", "evidence is expired")
            round_result = locked.round_result or {}
            if round_result.get("verdict") != "needs_review":
                raise ReviewEvidenceError(
                    "not_rejection_round",
                    "repairs apply only to needs_review rounds",
                )
            repairs, skipped = select_accepted_repairs(locked, accepted_finding_ids)
            current = resolved.read_bytes()
            plan_hash_before = hashlib.sha256(current).hexdigest()
            result: dict[str, object] = {
                "ok": True,
                "evidence_id": evidence_id,
                "changed": False,
                "applied": [],
                "skipped": skipped,
                "diff": "",
                "plan_hash_before": plan_hash_before,
                "plan_hash_after": plan_hash_before,
            }
            if not accepted_finding_ids:
                return result
            outcome = apply_repairs(current, plan_name=resolved.name, repairs=repairs)
            result["applied"] = outcome.applied
            result["skipped"] = [*skipped, *outcome.skipped]
            if outcome.updated == current:
                return result
            atomic_write_bytes(resolved, outcome.updated)
            result["changed"] = True
            result["diff"] = outcome.diff
            result["plan_hash_after"] = hashlib.sha256(outcome.updated).hexdigest()
            return result

    def _snapshot_document(self, evidence: PlanReviewEvidence) -> PlanDocument:
        return self.manifests.snapshot_document(evidence)

    def _round_result_for_evidence(
        self,
        evidence_id: str,
        round_result: Mapping[str, object],
    ) -> dict[str, object]:
        payload = self.checkpoints.round_result_for_evidence(
            evidence_id,
            round_result,
        )
        evidence = self.get_evidence(evidence_id)
        raw_findings = payload["findings"]
        if not isinstance(raw_findings, list):
            raise ReviewEvidenceError(
                "invalid_round_result",
                "reviewed round result findings must be an array",
            )
        findings = validate_plan_review_findings(raw_findings, evidence=evidence)
        payload["findings"] = findings
        return payload

    def _changed_sections_since_prior_round(
        self,
        evidence: PlanReviewEvidence,
    ) -> list[str]:
        prior_rows = [
            row
            for row in self.store.list_for_path(
                project_id=evidence.project_id,
                plan_path=evidence.plan_path,
            )
            if row.finalized_at is not None
            and row.expired_at is None
            and row.round_number < evidence.round_number
        ]
        if not prior_rows:
            return []
        prior = prior_rows[-1]
        current_hashes = reviewed_section_hashes(evidence.section_manifest)
        prior_hashes = reviewed_section_hashes(prior.section_manifest)
        return sorted(
            section_id
            for section_id in set(current_hashes) | set(prior_hashes)
            if current_hashes.get(section_id) != prior_hashes.get(section_id)
        )

    def checkpoint_plan_review_lesson_mint(
        self,
        evidence_id: str,
        *,
        status: str,
        detail: Mapping[str, object],
        caller_session_id: str | None = None,
    ) -> PlanReviewEvidence:
        return self.checkpoints.checkpoint_plan_review_lesson_mint(
            evidence_id,
            status=status,
            detail=detail,
            caller_session_id=caller_session_id,
        )

    def _resolve_plan_path(
        self,
        project_id: str,
        plan_path: str | Path,
    ) -> tuple[Path, str]:
        return self.manifests.resolve_plan_path(project_id, plan_path)

    def _evidence_path(self, evidence: PlanReviewEvidence) -> Path:
        return self.manifests.evidence_path(evidence)

    def _attempt_is_dead(self, evidence: PlanReviewEvidence) -> bool:
        if not evidence.is_live:
            return False
        if evidence.is_static_bound:
            return False
        if evidence.dispatch_run_id is None:
            expires = evidence.lease_expires_at
            return expires is not None and expires <= datetime.now(UTC)
        run = self.agent_runs.get(evidence.dispatch_run_id)
        return run is None or run.status not in {"pending", "running"}

    @staticmethod
    def _validate_attempt_binding(
        *,
        session_id: str | None,
        task_id: str | None,
        stage: str | None,
    ) -> None:
        interactive = session_id is not None and task_id is None and stage is None
        staged = session_id is None and task_id is not None and stage is not None
        if not (interactive or staged):
            raise ReviewEvidenceError(
                "invalid_attempt_binding",
                "provide session_id, or provide both task_id and stage",
            )

    @staticmethod
    def _matches_attempt(
        evidence: PlanReviewEvidence,
        *,
        round_number: int,
        session_id: str | None,
        task_id: str | None,
        stage: str | None,
        source_plan_path: str,
        snapshot: bytes,
    ) -> bool:
        return (
            evidence.round_number == round_number
            and evidence.session_id == session_id
            and evidence.task_id == task_id
            and evidence.stage == stage
            and (
                evidence.source_plan_path == source_plan_path
                or (evidence.source_plan_path is None and evidence.snapshot == snapshot)
            )
        )
