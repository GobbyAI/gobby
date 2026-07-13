from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

import gobby.plans.coverage_manifest as coverage_manifest_module
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


def test_unrelated_malformed_manifest_does_not_block_write(tmp_path: Path) -> None:
    malformed = (
        tmp_path
        / ".gobby"
        / "plans"
        / "coverage"
        / "unrelated-project"
        / "2"
        / "broken.coverage.yaml"
    )
    malformed.parent.mkdir(parents=True)
    malformed.write_text("header: [", encoding="utf-8")

    path = write_manifest(_report(), tmp_path)

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["header"]["plan_id"] == "plan"
    assert malformed.read_text(encoding="utf-8") == "header: ["


@pytest.mark.parametrize(
    "invalid_content",
    ["rows: []\n", "header: ["],
    ids=["headerless", "corrupt"],
)
def test_regenerate_recovers_invalid_own_manifest(tmp_path: Path, invalid_content: str) -> None:
    path = coverage_manifest_path(
        tmp_path,
        project_id="project",
        root_task_ref="#1",
        plan_id="plan",
    )
    path.parent.mkdir(parents=True)
    path.write_text(invalid_content, encoding="utf-8")

    result = write_manifest(_report(), tmp_path, regenerate=True)

    assert result == path
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["header"]["project_id"] == "project"
    assert raw["header"]["root_task_ref"] == "#1"
    assert raw["header"]["plan_id"] == "plan"
    audit = tmp_path / ".gobby" / "plans" / "coverage" / ".regenerate.log"
    assert "project #1 plan invalid-manifest -> hash" in audit.read_text(encoding="utf-8")


def test_invalid_own_manifest_requires_regenerate(tmp_path: Path) -> None:
    path = coverage_manifest_path(
        tmp_path,
        project_id="project",
        root_task_ref="#1",
        plan_id="plan",
    )
    path.parent.mkdir(parents=True)
    path.write_text("rows: []\n", encoding="utf-8")

    with pytest.raises(PathIdentityMismatchError):
        write_manifest(_report(), tmp_path)


def test_manifest_write_uses_same_directory_temp_and_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replace_calls: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def track_replace(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        target: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ) -> None:
        replace_calls.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(coverage_manifest_module.os, "replace", track_replace)

    path = write_manifest(_report(), tmp_path)

    assert len(replace_calls) == 1
    temp_path, target_path = replace_calls[0]
    assert target_path == path
    assert temp_path != path
    assert temp_path.parent == path.parent
    assert not temp_path.exists()


def test_regenerate_preserves_stable_row_decisions(tmp_path: Path) -> None:
    path = write_manifest(
        _report(
            plan_hash="old",
            task_tree_source_hash="stable-tree",
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
            task_tree_source_hash="stable-tree",
            row=CoverageRow(
                section_id="A1",
                item_id="A1.1",
                plan_node_hash="node-hash",
                status=CoverageStatus.covered,
            ),
        ),
        tmp_path,
        regenerate=True,
    )

    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    row = manifest["rows"][0]
    assert manifest["header"]["plan_hash"] == "new"
    assert manifest["header"]["task_tree_source_hash"] == "stable-tree"
    assert manifest["header"]["evidence"][0]["detail"] == "manual acceptance"
    assert row["status"] == "covered"
    assert row["leaves"][0]["leaf_task_ref"] == "#2"
    assert "evidence" not in row


def test_manifest_stores_shared_evidence_once_in_header(tmp_path: Path) -> None:
    evidence = EvidenceRow(
        kind=EvidenceKind.commits,
        ref="abc123",
        status=EvidenceResolveStatus.resolved,
        detail="commit abc123",
        artifacts_touched=("src/example.py",),
    )
    first = CoverageRow(
        section_id="A1",
        item_id="A1.1",
        status=CoverageStatus.covered,
        evidence=(evidence,),
    )
    report = _report(row=first)
    report = CoverageReport(
        header=report.header,
        rows=(
            first,
            CoverageRow(
                section_id="A1",
                item_id="A1.2",
                status=CoverageStatus.covered,
                evidence=(evidence,),
            ),
        ),
    )

    path = write_manifest(report, tmp_path)
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert manifest["header"]["evidence"] == [
        {
            "kind": "commits",
            "ref": "abc123",
            "status": "resolved",
            "detail": "commit abc123",
            "artifacts_touched": ["src/example.py"],
        }
    ]
    assert all("evidence" not in row for row in manifest["rows"])


@pytest.mark.parametrize(
    ("fresh_status", "fresh_tree_hash"),
    [
        (CoverageStatus.missing, "new-tree"),
        (CoverageStatus.invalid, "new-tree"),
        (CoverageStatus.missing, "old-tree"),
    ],
    ids=["deleted-leaf", "relabelled-leaf", "fresh-regression"],
)
def test_regenerate_uses_fresh_row_when_coverage_regresses(
    tmp_path: Path, fresh_status: CoverageStatus, fresh_tree_hash: str
) -> None:
    path = write_manifest(
        _report(
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
            ),
        ),
        tmp_path,
    )
    fresh_leaves = (
        ()
        if fresh_status is CoverageStatus.missing
        else (
            CoverageRowLeaf(
                leaf_task_ref="#3",
                validation_criteria_snippet="relabelled cover is invalid",
                matched_artifact_ref="covers:plan:A9:A9.1",
            ),
        )
    )

    write_manifest(
        _report(
            task_tree_source_hash=fresh_tree_hash,
            row=CoverageRow(
                section_id="A1",
                item_id="A1.1",
                plan_node_hash="node-hash",
                status=fresh_status,
                leaves=fresh_leaves,
            ),
        ),
        tmp_path,
        regenerate=True,
    )

    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    row = manifest["rows"][0]
    assert manifest["header"]["plan_hash"] == "hash"
    assert manifest["header"]["task_tree_source_hash"] == fresh_tree_hash
    assert row["status"] == fresh_status.value
    expected_refs = [] if fresh_status is CoverageStatus.missing else ["#3"]
    assert [leaf["leaf_task_ref"] for leaf in row["leaves"]] == expected_refs


def test_regenerate_does_not_preserve_without_task_tree_provenance(tmp_path: Path) -> None:
    path = write_manifest(
        _report(
            plan_hash="old",
            task_tree_source_hash="",
            row=CoverageRow(
                section_id="A1",
                item_id="A1.1",
                plan_node_hash="node-hash",
                status=CoverageStatus.covered,
                leaves=(
                    CoverageRowLeaf(
                        leaf_task_ref="#2",
                        validation_criteria_snippet="stale decision",
                        matched_artifact_ref="src/example.py",
                    ),
                ),
            ),
        ),
        tmp_path,
    )

    write_manifest(
        _report(plan_hash="new", task_tree_source_hash=""),
        tmp_path,
        regenerate=True,
    )

    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert manifest["rows"][0]["leaves"] == []


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
