"""Coordinator-owned manifest handoff without adversary evidence."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory

from gobby.plans.digests import canonical_json_sha256
from gobby.plans.manifest_emitter import ManifestSynthesisError, derive_manifest_entries
from gobby.plans.parser import PlanDocument, PlanParseError, parse_plan
from gobby.plans.review_evidence_io import (
    atomic_write_bytes,
    normalize_plan_path,
    render_manifest_plan,
)
from gobby.plans.review_evidence_models import ReviewEvidenceError, canonical_json_object
from gobby.storage.hub.protocol import HubDatabase, PlanReviewEvidenceMutation
from gobby.storage.project_checkouts import CheckoutNotFoundError, require_root
from gobby.storage.projects import LocalProjectManager
from gobby.storage.workspace_machine_scope import require_local_machine_id


class PlanHandoffManifestService:
    """Derive and apply a canonical manifest for explicit human handoff."""

    def __init__(self, db: HubDatabase) -> None:
        self.db = db
        self.projects = LocalProjectManager(db)

    def derive(
        self,
        *,
        project_id: str,
        plan_path: str | Path,
        routing_decisions: Mapping[str, object],
    ) -> dict[str, object]:
        resolved, _ = self._resolve_plan_path(project_id, plan_path)
        source = resolved.read_bytes()
        return self._derive_from_bytes(
            plan_path=resolved,
            source=source,
            routing_decisions=routing_decisions,
        )

    def apply(
        self,
        *,
        project_id: str,
        plan_path: str | Path,
        routing_decisions: Mapping[str, object],
        source_plan_hash: str,
        rendered_plan_hash: str,
        manifest_digest: str,
    ) -> dict[str, object]:
        resolved, relative_path = self._resolve_plan_path(project_id, plan_path)
        mutation = PlanReviewEvidenceMutation(
            project_id=project_id,
            plan_path=relative_path,
        )
        with self.db.transaction_immediate(mutation):
            current = resolved.read_bytes()
            current_hash = _sha256(current)
            expected = self._derive_from_bytes(
                plan_path=resolved,
                source=current,
                routing_decisions=routing_decisions,
            )
            if expected["manifest_digest"] != manifest_digest:
                raise ReviewEvidenceError(
                    "manifest_digest_mismatch",
                    "manifest digest differs from canonical derivation",
                )
            if current_hash == rendered_plan_hash:
                if expected["rendered_plan_hash"] != rendered_plan_hash:
                    raise ReviewEvidenceError(
                        "rendered_plan_hash_mismatch",
                        "current plan hash matches the retry token but not canonical rendering",
                    )
                return {
                    "applied": False,
                    "idempotent": True,
                    "source_plan_hash": source_plan_hash,
                    "rendered_plan_hash": rendered_plan_hash,
                    "manifest_digest": manifest_digest,
                    "entry_count": expected["entry_count"],
                }
            if current_hash != source_plan_hash:
                raise ReviewEvidenceError(
                    "plan_drift",
                    "plan bytes changed after handoff manifest derivation",
                    details={
                        "expected_source_plan_hash": source_plan_hash,
                        "actual_source_plan_hash": current_hash,
                    },
                )
            if expected["rendered_plan_hash"] != rendered_plan_hash:
                raise ReviewEvidenceError(
                    "rendered_plan_hash_mismatch",
                    "rendered plan hash differs from canonical derivation",
                )
            rendered = expected["rendered_plan"]
            if not isinstance(rendered, bytes):  # pragma: no cover - internal invariant.
                raise RuntimeError("handoff derivation did not return rendered bytes")
            atomic_write_bytes(resolved, rendered)
            return {
                "applied": True,
                "idempotent": False,
                "source_plan_hash": source_plan_hash,
                "rendered_plan_hash": rendered_plan_hash,
                "manifest_digest": manifest_digest,
                "entry_count": expected["entry_count"],
            }

    def _derive_from_bytes(
        self,
        *,
        plan_path: Path,
        source: bytes,
        routing_decisions: Mapping[str, object],
    ) -> dict[str, object]:
        try:
            routing = canonical_json_object(routing_decisions)
            document = _parse_source(plan_path, source)
            entries = derive_manifest_entries(document, routing)
            rendered = render_manifest_plan(plan_path, source, entries)
            _parse_expansion(plan_path, rendered, plan_id=document.plan_id)
        except ReviewEvidenceError:
            raise
        except ManifestSynthesisError as exc:
            raise ReviewEvidenceError("invalid_routing_decisions", str(exc)) from exc
        except PlanParseError as exc:
            raise ReviewEvidenceError(
                "invalid_plan",
                "plan does not satisfy the handoff contract",
                details={
                    "diagnostics": [
                        {"line": line, "message": message} for line, message in exc.errors
                    ]
                },
            ) from exc
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise ReviewEvidenceError(
                "invalid_plan", f"plan handoff validation failed: {exc}"
            ) from exc
        return {
            "routing_decisions": routing,
            "manifest_entries": entries,
            "manifest_digest": canonical_json_sha256(entries),
            "entry_count": len(entries),
            "source_plan_hash": _sha256(source),
            "rendered_plan_hash": _sha256(rendered),
            "rendered_plan": rendered,
        }

    def _resolve_plan_path(
        self,
        project_id: str,
        plan_path: str | Path,
    ) -> tuple[Path, str]:
        project = self.projects.get(project_id)
        if project is None:
            raise ReviewEvidenceError(
                "project_not_found",
                f"project has no local repository: {project_id}",
            )
        try:
            machine_id = require_local_machine_id(
                None, resource_kind="project_checkout", resource_id=project_id
            )
            root = Path(require_root(self.db, project_id, machine_id)).resolve(strict=True)
        except CheckoutNotFoundError as exc:
            raise ReviewEvidenceError(
                "project_not_found",
                f"project has no local repository: {project_id}",
            ) from exc
        resolved = normalize_plan_path(root, plan_path)
        return resolved, resolved.relative_to(root).as_posix()


def _parse_source(plan_path: Path, source: bytes) -> PlanDocument:
    with TemporaryDirectory(prefix="gobby-plan-handoff-") as temp_dir:
        temp_path = Path(temp_dir) / plan_path.name
        temp_path.write_bytes(source)
        return parse_plan(temp_path, parse_mode="draft")


def _parse_expansion(plan_path: Path, rendered: bytes, *, plan_id: str | None) -> None:
    with TemporaryDirectory(prefix="gobby-plan-handoff-") as temp_dir:
        temp_path = Path(temp_dir) / plan_path.name
        temp_path.write_bytes(rendered)
        parse_plan(temp_path, parse_mode="expansion", plan_id_override=plan_id)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
