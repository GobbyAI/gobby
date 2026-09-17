"""Plan operations resolve paths inside registered project worktrees."""

from __future__ import annotations

import hashlib
import json
import textwrap
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from gobby.code_index.models import (
    CODE_INDEX_UUID_NAMESPACE,
    IndexedFile,
    IndexedProject,
    IndexWriteMode,
)
from gobby.code_index.storage import CodeIndexStorage
from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.tools.plans import create_plan_registry
from gobby.mcp_proxy.tools.plans.review_evidence import register_review_evidence_tools
from gobby.plans.review_coverage import REVIEW_LANES
from gobby.plans.review_evidence import PlanReviewEvidenceService
from gobby.plans.review_evidence_models import ReviewEvidenceError
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sessions import SessionManager
from gobby.utils.project_context import reset_project_context, set_project_context
from gobby.utils.session_context import session_context_for_test
from tests.fixtures.isolated_checkout import (
    insert_overlay,
    install_isolated_checkout_project,
    write_project_marker,
)

pytestmark = pytest.mark.unit

REVIEW_PLAN = ".gobby/plans/review-evidence.md"
DEMO_PLAN = ".gobby/plans/worktree-demo.md"


@dataclass(frozen=True)
class WorktreeReview:
    service: PlanReviewEvidenceService
    project_id: str
    primary_session_id: str
    primary_plan: Path
    worktree: Path
    worktree_plan: Path
    worktree_session_id: str


@pytest.fixture
def worktree_review(
    review_setup: tuple[PlanReviewEvidenceService, str, str, Path],
    temp_db: HubDatabase,
    tmp_path_factory: pytest.TempPathFactory,
) -> WorktreeReview:
    service, project_id, primary_session_id, primary_plan = review_setup
    sessions = SessionManager(temp_db)
    primary_session = sessions.get(primary_session_id)
    assert primary_session is not None and primary_session.machine_id is not None
    worktree = tmp_path_factory.mktemp("review-worktree").resolve()
    write_project_marker(worktree, project_id=project_id, name="review-evidence")
    insert_overlay(
        temp_db,
        project_id=project_id,
        machine_id=primary_session.machine_id,
        path=str(worktree),
        kind="worktree",
    )
    worktree_plan = worktree / REVIEW_PLAN
    worktree_plan.parent.mkdir(parents=True, exist_ok=True)
    worktree_plan.write_bytes(
        primary_plan.read_bytes().replace(b"# Review Evidence", b"# Review Evidence Worktree", 1)
    )
    worktree_session = sessions.register(
        external_id="review-evidence-worktree",
        machine_id=primary_session.machine_id,
        source="codex",
        project_id=project_id,
        workspace_path=str(worktree),
    )
    return WorktreeReview(
        service=service,
        project_id=project_id,
        primary_session_id=primary_session_id,
        primary_plan=primary_plan,
        worktree=worktree,
        worktree_plan=worktree_plan,
        worktree_session_id=worktree_session.id,
    )


def _review_registry(db: HubDatabase, project_id: str) -> InternalToolRegistry:
    registry = InternalToolRegistry(name="test-plan-worktree-roots")
    register_review_evidence_tools(registry, db, resolve_project_id=lambda _project: project_id)
    return registry


def test_relative_plan_path_uses_primary_checkout_outside_worktrees(
    worktree_review: WorktreeReview,
) -> None:
    review = worktree_review

    with session_context_for_test(review.primary_session_id):
        prepared = review.service.prepare_plan_review_round(
            project_id=review.project_id,
            plan_path=REVIEW_PLAN,
            round_number=1,
            session_id=review.primary_session_id,
        )

    assert review.service.snapshot_bytes(prepared.evidence_id) == review.primary_plan.read_bytes()
    assert review.service.get_evidence(prepared.evidence_id).plan_path == REVIEW_PLAN


async def test_worktree_session_review_tools_resolve_relative_paths_in_worktree(
    worktree_review: WorktreeReview,
    temp_db: HubDatabase,
) -> None:
    review = worktree_review
    source = review.worktree / "src" / "example.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    citation = {
        "path": "src/example.py",
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "line_start": 1,
        "line_end": 1,
    }
    lanes = [
        {
            "lane_id": lane_id,
            "status": (
                "delegated-verified" if lane_id == "repository_blast_radius" else "completed"
            ),
            "section_ids_checked": ["1.1"],
            "source_citations": [citation],
            "candidate_issues": [],
        }
        for lane_id in REVIEW_LANES
    ]
    registry = _review_registry(temp_db, review.project_id)

    with session_context_for_test(review.worktree_session_id):
        prepared = await registry.call(
            "prepare_plan_review_round",
            {"plan_path": REVIEW_PLAN, "round_number": 1},
        )
        manifest = await registry.call(
            "derive_plan_review_manifest",
            {"evidence_id": prepared["evidence_id"], "routing_decisions": {}},
        )
        coverage = await registry.call(
            "validate_plan_review_coverage",
            {
                "evidence_id": prepared["evidence_id"],
                "lane_results": lanes,
                "candidate_dispositions": {
                    "cross_lane_interaction_complete": True,
                    "adjacent_variant_complete": True,
                    "items": [],
                },
                "shadow_manifest_status": manifest,
            },
        )

    assert prepared["ok"] is True, prepared
    evidence = review.service.get_evidence(prepared["evidence_id"])
    assert evidence.snapshot == review.worktree_plan.read_bytes()
    assert evidence.plan_path == REVIEW_PLAN
    assert evidence.session_id == review.worktree_session_id
    assert manifest["ok"] is True
    assert manifest["status"] == "valid"
    assert coverage["ok"] is True, coverage
    assert coverage["coverage_attestation"]["shadow_manifest_status"]["status"] == "valid"


def test_absolute_worktree_plan_path_resolves_against_worktree(
    worktree_review: WorktreeReview,
) -> None:
    review = worktree_review

    prepared = review.service.prepare_plan_review_round(
        project_id=review.project_id,
        plan_path=review.worktree_plan,
        round_number=1,
        session_id=review.primary_session_id,
    )

    evidence = review.service.get_evidence(prepared.evidence_id)
    assert evidence.snapshot == review.worktree_plan.read_bytes()
    assert evidence.plan_path == REVIEW_PLAN
    assert review.service.verify_plan_unchanged(prepared.evidence_id, review.worktree_plan)


def test_plan_paths_outside_registered_roots_fail_invalid_plan_path(
    worktree_review: WorktreeReview,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    review = worktree_review
    unregistered = tmp_path_factory.mktemp("unregistered-worktree").resolve()
    write_project_marker(unregistered, project_id=review.project_id, name="review-evidence")
    unregistered_plan = unregistered / REVIEW_PLAN
    unregistered_plan.parent.mkdir(parents=True, exist_ok=True)
    unregistered_plan.write_bytes(review.worktree_plan.read_bytes())
    escaping = review.worktree.parent / f"{review.worktree.name}-outside.md"
    escaping.write_bytes(review.worktree_plan.read_bytes())

    with pytest.raises(ReviewEvidenceError, match="escapes project root") as absolute:
        review.service.prepare_plan_review_round(
            project_id=review.project_id,
            plan_path=unregistered_plan,
            round_number=1,
            session_id=review.primary_session_id,
        )
    with (
        session_context_for_test(review.worktree_session_id),
        pytest.raises(ReviewEvidenceError, match="escapes project root") as relative,
    ):
        review.service.prepare_plan_review_round(
            project_id=review.project_id,
            plan_path=f"../{escaping.name}",
            round_number=1,
            session_id=review.worktree_session_id,
        )

    assert absolute.value.code == "invalid_plan_path"
    assert relative.value.code == "invalid_plan_path"


@dataclass(frozen=True)
class WorktreeValidation:
    registry: InternalToolRegistry
    project_id: str
    primary_root: Path
    worktree_plan: Path
    worktree_session_id: str


@pytest.fixture
def worktree_validation(
    temp_db: HubDatabase,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> WorktreeValidation:
    isolated = install_isolated_checkout_project(
        temp_db, tmp_path / "primary", name="plan-worktree-validation", monkeypatch=monkeypatch
    )
    project_id = isolated.project.id
    primary_root = Path(isolated.root_path)
    worktree = (tmp_path / "worktree").resolve()
    write_project_marker(worktree, project_id=project_id, name="plan-worktree-validation")
    (worktree / ".gobby" / "isolation.json").write_text(
        json.dumps({"parent_project_path": str(primary_root), "parent_project_id": project_id}),
        encoding="utf-8",
    )
    insert_overlay(
        temp_db,
        project_id=project_id,
        machine_id=isolated.machine_id,
        path=str(worktree),
        kind="worktree",
    )
    target = worktree / "docs" / "demo.md"
    target.parent.mkdir(parents=True)
    target.write_text("Worktree demo.\n", encoding="utf-8")
    (worktree / "docs" / "consumer.md").write_text("Reads the demo.\n", encoding="utf-8")
    content_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    overlay_id = str(uuid.uuid5(CODE_INDEX_UUID_NAMESPACE, str(worktree)))
    code_index = CodeIndexStorage(temp_db)
    code_index.upsert_project_stats(
        IndexedProject(id=project_id, root_path=str(primary_root)),
        mode=IndexWriteMode.PRIMARY,
    )
    code_index.upsert_project_stats(
        IndexedProject(id=overlay_id, root_path=str(worktree), total_files=1),
        mode=IndexWriteMode.OVERLAY,
    )
    code_index.upsert_file(
        IndexedFile(
            id=IndexedFile.make_id(overlay_id, "docs/demo.md", content_hash),
            project_id=overlay_id,
            file_path="docs/demo.md",
            language="markdown",
            content_hash=content_hash,
            symbol_count=0,
            byte_size=target.stat().st_size,
        ),
        root_path=str(worktree),
        mode=IndexWriteMode.OVERLAY,
    )
    worktree_plan = worktree / DEMO_PLAN
    worktree_plan.parent.mkdir(parents=True, exist_ok=True)
    worktree_plan.write_text(
        textwrap.dedent(
            """
            > **Plan ID:** worktree-demo

            ## P1 Phase
            `kind: framing`

            ### 1.1 Work [category: docs]
            `kind: deliverable`

            Target: `docs/demo.md`

            Body.

            Consumers unchanged:
            - `docs/consumer.md` — no-edit-reason: Reads the demo only.

            **Acceptance:**
            - 1.1.1 — Docs exist. file: `docs/demo.md`
            """
        ).lstrip(),
        encoding="utf-8",
    )
    session = SessionManager(temp_db).register(
        external_id="plan-worktree-validation",
        machine_id=isolated.machine_id,
        source="codex",
        project_id=project_id,
        workspace_path=str(worktree),
    )
    return WorktreeValidation(
        registry=create_plan_registry(temp_db, default_project_id=project_id),
        project_id=project_id,
        primary_root=primary_root,
        worktree_plan=worktree_plan,
        worktree_session_id=session.id,
    )


async def _validate_plan(
    validation: WorktreeValidation,
    plan_file: str,
    *,
    session_id: str | None,
) -> dict[str, Any]:
    token = set_project_context(
        {"id": validation.project_id, "project_path": str(validation.primary_root)}
    )
    result: dict[str, Any]
    try:
        if session_id is None:
            result = await validation.registry.call("validate_plan", {"plan_file": plan_file})
        else:
            with session_context_for_test(session_id):
                result = await validation.registry.call("validate_plan", {"plan_file": plan_file})
    finally:
        reset_project_context(token)
    return result


async def test_validate_plan_resolves_relative_path_in_worktree_session(
    worktree_validation: WorktreeValidation,
) -> None:
    result = await _validate_plan(
        worktree_validation,
        DEMO_PLAN,
        session_id=worktree_validation.worktree_session_id,
    )

    assert result["valid"] is True, result
    assert result["path"] == str(worktree_validation.worktree_plan)
    assert result["symbol_validation"]["status"] == "passed"
    assert result["symbol_validation"]["checked_targets"] == ["docs/demo.md"]


async def test_validate_plan_resolves_absolute_worktree_path_against_worktree(
    worktree_validation: WorktreeValidation,
) -> None:
    result = await _validate_plan(
        worktree_validation,
        str(worktree_validation.worktree_plan),
        session_id=None,
    )

    assert result["valid"] is True, result
    assert result["symbol_validation"]["status"] == "passed"


async def test_validate_plan_relative_path_outside_worktree_uses_primary_checkout(
    worktree_validation: WorktreeValidation,
) -> None:
    result = await _validate_plan(worktree_validation, DEMO_PLAN, session_id=None)

    assert result["valid"] is False
    assert result["errors"] == [
        f"Plan file not found: {worktree_validation.primary_root / DEMO_PLAN}"
    ]
