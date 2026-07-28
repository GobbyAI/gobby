from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from gobby.plans import review_evidence_io as snapshot_io
from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_models import PlanReviewEvidence, ReviewEvidenceError
from gobby.plans.review_requirements import (
    REQUEST_ANCHOR_VARIABLE,
    assemble_requirements_bundle,
    build_request_anchor,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.workflows.state_manager import SessionVariableManager

OFFLOAD_THRESHOLD_CHARS = 15_000
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_snapshot_page_rejects_non_bytes_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    service = PlanReviewEvidenceService.__new__(PlanReviewEvidenceService)
    corrupt_evidence = cast(PlanReviewEvidence, SimpleNamespace(snapshot="invalid"))
    monkeypatch.setattr(service, "get_evidence", lambda _evidence_id: corrupt_evidence)

    with pytest.raises(ReviewEvidenceError, match="stored plan snapshot is not bytes"):
        service.snapshot_page("evidence-1")


def _valid_plan_bytes(*, extra: str = "") -> bytes:
    return "\n".join(
        [
            "# Snapshot Transport",
            "**Plan ID:** snapshot-transport",
            "",
            "## P1 Phase",
            "`kind: framing`",
            "",
            "### 1.1 Work",
            "`kind: deliverable`",
            "",
            "Target: `src/example.py`",
            "",
            "**Acceptance:**",
            "- 1.1.1 — Behavior exists. test: `tests/test_example.py`",
            extra,
            "",
            "## Task Mapping",
            "`kind: framing`",
            "",
            "Pending.",
            "",
            "## V1 Plan Changelog",
            "`kind: verification`",
            "",
            "No rounds yet.",
            "",
            "## M1 Task Manifest",
            "`kind: manifest`",
            "",
            "```yaml",
            "- title: Implement example",
            "  source_section: '1.1'",
            "  covers:",
            "    - 1.1.1",
            "  category: code",
            "  implementation_domain: backend",
            "  priority: 2",
            "  task_type: feature",
            "  tdd: false",
            "  labels:",
            "    - covers:snapshot-transport:1.1:1.1.1",
            "  description: Implement the example.",
            "  validation_criteria: Example behavior is tested.",
            "```",
            "",
        ]
    ).encode()


def _envelope(
    snapshot: bytes,
    *,
    prior_round_context: Mapping[str, object] | None = None,
    quality_ledger: list[dict[str, object]] | None = None,
) -> snapshot_io.SnapshotEnvelope:
    return snapshot_io.serialize_snapshot_envelope(
        evidence_id="evidence-1",
        plan_hash=hashlib.sha256(snapshot).hexdigest(),
        round_number=2,
        snapshot=snapshot,
        section_manifest=snapshot_io.build_section_manifest(snapshot),
        changed_section_ids=["1.1"],
        prior_round_context=prior_round_context,
        quality_ledger=quality_ledger or [],
        review_complexity={
            "deliverable_count": 1,
            "acceptance_item_count": 1,
            "target_file_count": 1,
            "changed_section_count": 1,
        },
    )


def _pages(envelope: snapshot_io.SnapshotEnvelope) -> list[dict[str, object]]:
    pages: list[dict[str, object]] = []
    offset = 0
    while True:
        page = snapshot_io.paginate_snapshot_envelope(
            envelope,
            offset=offset,
            limit=8_000,
        )
        pages.append(page)
        next_offset = page["next_offset"]
        if next_offset is None:
            return pages
        assert isinstance(next_offset, int)
        assert next_offset > offset
        offset = next_offset


def _reassembled_bytes(pages: list[dict[str, object]]) -> bytes:
    return "".join(cast(str, page["content"]) for page in pages).encode()


def test_paged_fetch_under_threshold() -> None:
    snapshot = _valid_plan_bytes(extra=('quote " slash \\ emoji 🧪\n' * 7_000))
    envelope = _envelope(snapshot)
    pages = _pages(envelope)

    assert len(snapshot) > 140_000
    assert len(pages) > 1
    for page in pages:
        assert len(json.dumps({"ok": True, **page}, ensure_ascii=True)) < (OFFLOAD_THRESHOLD_CHARS)
        assert page["snapshot_hash"]
        assert page["total_sections"] == len(snapshot_io.build_section_manifest(snapshot))
        assert "next_offset" in page


def test_manifest_cache_hit(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = LocalProjectManager(temp_db).create(
        name="snapshot-manifest-cache",
        repo_path=str(tmp_path),
    )
    session = SessionManager(temp_db).register(
        external_id="snapshot-manifest-cache",
        machine_id="test-machine",
        source="codex",
        project_id=project.id,
    )
    plan_dir = tmp_path / ".gobby" / "plans"
    plan_dir.mkdir(parents=True)
    plan_path = plan_dir / "snapshot-transport.md"
    plan_path.write_bytes(_valid_plan_bytes())
    SessionVariableManager(temp_db).merge_variables(
        session.id,
        {
            REQUEST_ANCHOR_VARIABLE: build_request_anchor(
                "snapshot-manifest-request",
                "Review the snapshot manifest",
            )
        },
    )
    service = PlanReviewEvidenceService(temp_db)
    prepared = service.prepare_plan_review_round(
        project_id=project.id,
        plan_path=plan_path,
        round_number=1,
        session_id=session.id,
    )
    render_calls = 0
    original_render = snapshot_io.render_manifest_plan

    def count_render(
        candidate_path: Path,
        candidate_bytes: bytes,
        entries: list[dict[str, object]],
    ) -> bytes:
        nonlocal render_calls
        render_calls += 1
        return original_render(candidate_path, candidate_bytes, entries)

    def accept_coverage(**kwargs: object) -> dict[str, object]:
        shadow = kwargs["shadow_manifest_status"]
        expected = kwargs["expected_shadow_manifest_status"]
        assert shadow == expected
        return {"evidence_id": prepared.evidence_id}

    monkeypatch.setattr(
        "gobby.plans.review_manifest_service.render_manifest_plan",
        count_render,
    )
    monkeypatch.setattr(
        "gobby.plans.review_evidence.validate_review_coverage",
        accept_coverage,
    )

    for _ in range(2):
        service.validate_plan_review_coverage(
            prepared.evidence_id,
            lane_results=[],
            candidate_dispositions={},
            routing_decisions={},
        )

    assert render_calls == 1


def test_page_union_and_local_hash_verification() -> None:
    snapshot = _valid_plan_bytes(extra=("bounded page\n" * 15_000))
    section_manifest = snapshot_io.build_section_manifest(snapshot)
    pages = _pages(_envelope(snapshot))
    serialized = _reassembled_bytes(pages)
    parsed = snapshot_io.parse_snapshot_envelope(
        serialized,
        snapshot_hash=cast(str, pages[0]["snapshot_hash"]),
    )

    assert parsed["snapshot"] == snapshot
    assert parsed["section_manifest"] == [section.to_dict() for section in section_manifest]
    with pytest.raises(ReviewEvidenceError, match="snapshot hash"):
        snapshot_io.parse_snapshot_envelope(
            serialized[:-1],
            snapshot_hash=cast(str, pages[0]["snapshot_hash"]),
        )


def test_sidecar_records_paged_and_bounded() -> None:
    snapshot = (REPO_ROOT / ".gobby/plans/adversary-convergence-improvements.md").read_bytes()
    large_value = 'immutable sidecar 🧪 "quoted" \\\\ value\n' * 1_200
    requirements_bundle = assemble_requirements_bundle(
        project_root=REPO_ROOT,
        plan_snapshot=snapshot,
        request_anchor=build_request_anchor("snapshot-sidecar", large_value),
    )
    inventory: dict[str, object] = {
        "sites": [{"path": "src/example.py", "context": large_value}],
    }
    prior_round_context: dict[str, object] = {
        "prior_evidence_id": "prior-1",
        "requirements_bundle": requirements_bundle,
        "consumer_site_inventory": inventory,
    }
    quality_ledger: list[dict[str, object]] = [{"ledger_id": "ledger-1", "detail": large_value}]
    pages = _pages(
        _envelope(
            snapshot,
            prior_round_context=prior_round_context,
            quality_ledger=quality_ledger,
        )
    )

    for page in pages:
        assert len(json.dumps({"ok": True, **page}, ensure_ascii=True)) < (OFFLOAD_THRESHOLD_CHARS)
    parsed = snapshot_io.parse_snapshot_envelope(
        _reassembled_bytes(pages),
        snapshot_hash=cast(str, pages[0]["snapshot_hash"]),
    )
    records = cast(list[dict[str, object]], parsed["records"])
    record_types = {cast(str, record["record_type"]) for record in records}
    assert {
        "plan_section",
        "requirement_source",
        "quality_ledger_entry",
        "consumer_inventory",
        "prior_round_context",
    } <= record_types
    assert parsed["snapshot"] == snapshot
    assert parsed["prior_round_context"] == prior_round_context
    assert parsed["quality_ledger"] == quality_ledger
