from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from gobby.plans.coverage import (
    CoverageHeader,
    CoverageReport,
    CoverageRow,
    CoverageRowLeaf,
    CoverageStatus,
    TaskTreeSource,
)
from gobby.plans.coverage_manifest import (
    IdentityCollisionError,
    PathIdentityMismatchError,
    coverage_manifest_path,
    write_manifest,
)
from gobby.plans.evidence import EvidenceKind, EvidenceResolveStatus, EvidenceRow

pytestmark = pytest.mark.unit


def _report(
    *,
    project_id: str = "project",
    root_task_ref: str = "#1",
    plan_id: str = "plan",
    plan_hash: str = "hash",
    task_tree_source_hash: str = "tree",
    row: CoverageRow | None = None,
) -> CoverageReport:
    return CoverageReport(
        header=CoverageHeader(
            plan_id=plan_id,
            plan_hash=plan_hash,
            root_task_ref=root_task_ref,
            project_id=project_id,
            generated_at="2026-04-27T00:00:00Z",
            task_tree_source=TaskTreeSource.db,
            task_tree_source_hash=task_tree_source_hash,
            evidence_summary=(),
        ),
        rows=(
            row
            or CoverageRow(
                section_id="A1",
                item_id="A1.1",
                plan_node_hash="node-hash",
                status=CoverageStatus.covered,
            ),
        ),
    )


def test_same_plan_two_root_tasks_distinct_manifests(tmp_path: Path) -> None:
    left = write_manifest(_report(root_task_ref="#1"), tmp_path)
    right = write_manifest(_report(root_task_ref="#2"), tmp_path)

    assert left != right
    assert left.exists()
    assert right.exists()


def test_two_plans_one_root_distinct_manifests(tmp_path: Path) -> None:
    left = write_manifest(_report(plan_id="plan-a"), tmp_path)
    right = write_manifest(_report(plan_id="plan-b"), tmp_path)

    assert left != right


def test_identity_collision_blocks_overwrite(tmp_path: Path) -> None:
    write_manifest(_report(plan_hash="old"), tmp_path)

    with pytest.raises(IdentityCollisionError) as exc_info:
        write_manifest(_report(plan_hash="new"), tmp_path)

    assert exc_info.value.existing_hash == "old"
    assert exc_info.value.new_hash == "new"


def test_regenerate_overwrites_and_audits(tmp_path: Path) -> None:
    path = write_manifest(_report(plan_hash="old"), tmp_path)

    write_manifest(_report(plan_hash="new"), tmp_path, regenerate=True)

    assert "new" in path.read_text(encoding="utf-8")
    log = tmp_path / ".gobby/plans/coverage/.regenerate.log"
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "project #1 plan old -> new" in lines[0]


def test_regenerate_preserves_stable_row_decisions(tmp_path: Path) -> None:
    path = write_manifest(
        _report(
            plan_hash="old",
            task_tree_source_hash="old-tree",
            row=CoverageRow(
                section_id="A1",
                item_id="A1.1",
                plan_node_hash="node-hash",
                status=CoverageStatus.covered,
                leaves=(
                    CoverageRowLeaf(
                        leaf_task_ref="#2",
                        validation_criteria_snippet="acceptance covered",
                        matched_artifact_ref="src/example.py",
                    ),
                ),
                evidence=(
                    EvidenceRow(
                        kind=EvidenceKind.none,
                        ref="none",
                        status=EvidenceResolveStatus.resolved,
                        detail="manual acceptance",
                        artifacts_touched=("src/example.py",),
                    ),
                ),
            ),
        ),
        tmp_path,
    )

    write_manifest(
        _report(
            plan_hash="new",
            task_tree_source_hash="new-tree",
            row=CoverageRow(
                section_id="A1",
                item_id="A1.1",
                plan_node_hash="node-hash",
                status=CoverageStatus.missing,
            ),
        ),
        tmp_path,
        regenerate=True,
    )

    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    row = manifest["rows"][0]
    assert manifest["header"]["plan_hash"] == "new"
    assert manifest["header"]["task_tree_source_hash"] == "new-tree"
    assert row["status"] == "covered"
    assert row["leaves"][0]["leaf_task_ref"] == "#2"
    assert row["evidence"][0]["detail"] == "manual acceptance"


def test_casefold_leaf_collision_raises_path_identity_mismatch(tmp_path: Path) -> None:
    write_manifest(_report(plan_id="Plan"), tmp_path)

    with pytest.raises(PathIdentityMismatchError):
        write_manifest(_report(plan_id="plan"), tmp_path)


def test_casefold_protection_works_on_case_sensitive_fs(tmp_path: Path) -> None:
    write_manifest(_report(project_id="Project"), tmp_path)

    with pytest.raises(PathIdentityMismatchError):
        write_manifest(_report(project_id="project"), tmp_path)


def test_exact_path_different_identity_raises(tmp_path: Path) -> None:
    path = write_manifest(_report(plan_id="plan-a"), tmp_path)

    with pytest.raises(PathIdentityMismatchError):
        write_manifest(_report(plan_id="plan-b"), tmp_path, manifest_path=path)


def test_casefold_ancestor_dir_collision_raises(tmp_path: Path) -> None:
    write_manifest(_report(project_id="Project", root_task_ref="#Root"), tmp_path)

    with pytest.raises(PathIdentityMismatchError):
        write_manifest(_report(project_id="project", root_task_ref="#Root"), tmp_path)


def test_root_task_ref_hash_strip_collision_raises(tmp_path: Path) -> None:
    write_manifest(_report(root_task_ref="#127"), tmp_path)

    with pytest.raises(PathIdentityMismatchError):
        write_manifest(_report(root_task_ref="127"), tmp_path)


def test_sanitize_collapse_collision_raises(tmp_path: Path) -> None:
    path = coverage_manifest_path(tmp_path, project_id="a/b", root_task_ref="#1", plan_id="plan")
    write_manifest(_report(project_id="a/b"), tmp_path)

    with pytest.raises(PathIdentityMismatchError):
        write_manifest(_report(project_id="a-b"), tmp_path, manifest_path=path)


def test_regenerate_does_not_bypass_path_identity_mismatch(tmp_path: Path) -> None:
    path = write_manifest(_report(plan_id="plan-a"), tmp_path)

    with pytest.raises(PathIdentityMismatchError):
        write_manifest(_report(plan_id="plan-b"), tmp_path, regenerate=True, manifest_path=path)
