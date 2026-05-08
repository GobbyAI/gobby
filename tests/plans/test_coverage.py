from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gobby.plans.coverage import (
    CoverageStatus,
    EvidenceKind,
    MissingScopeError,
    StaleHashError,
    TaskTreeSource,
    evaluate,
)
from gobby.plans.parser import (
    AcceptanceItem,
    ArtifactKind,
    Deferral,
    Kind,
    PlanDocument,
    PlanSection,
)

pytestmark = pytest.mark.unit


def _item(
    item_id: str = "A1.1", artifact_ref: str = "src/gobby/plans/coverage.py"
) -> AcceptanceItem:
    return AcceptanceItem(
        item_id=item_id,
        prose=f"Implement {artifact_ref}",
        artifact_kind=ArtifactKind.file,
        artifact_ref=artifact_ref,
        source_line=5,
    )


def _section(
    *items: AcceptanceItem,
    section_id: str = "A1",
    deferral: Deferral | None = None,
) -> PlanSection:
    return PlanSection(
        section_id=section_id,
        parent_id=None,
        heading_level=2,
        title=section_id,
        kind=Kind.deferred if deferral is not None else Kind.deliverable,
        acceptance_items=items,
        deferral=deferral,
        source_span=(1, 5),
    )


def _plan(*sections: PlanSection, plan_id: str = "plan", source_hash: str = "hash") -> PlanDocument:
    return PlanDocument(
        plan_id=plan_id,
        source_path=Path("plan.md"),
        source_hash=source_hash,
        sections=sections,
        framing_headings=(),
    )


def test_exports_a4_public_api() -> None:
    assert EvidenceKind.none.value == "none"
    assert TaskTreeSource.db.value == "db"
    assert CoverageStatus.covered.value == "covered"


def test_evaluate_reports_covered_missing_invalid_and_deferred() -> None:
    covered = _item("A1.1", "src/covered.py")
    invalid = _item("A1.2", "src/invalid.py")
    missing = _item("A1.3", "src/missing.py")
    deferred_item = _item("A2.1", "src/deferred.py")
    deferral = Deferral(
        task_ref="#200",
        reason="needs follow-up",
        owner="backend",
        original_acceptance_items=(deferred_item,),
        raw_block="",
    )
    plan_doc = _plan(
        _section(covered, invalid, missing),
        _section(deferred_item, section_id="A2", deferral=deferral),
    )

    report = evaluate(
        plan=plan_doc,
        plan_id="plan",
        plan_hash="hash",
        task_tree=TaskTreeSource.db,
        root_task_ref="#1",
        project_id="project",
        task_records=[
            {"ref": "#1", "path_cache": "1", "dependencies": ["#200"]},
            {
                "ref": "#101",
                "path_cache": "1.101",
                "labels": ["covers:plan:A1:A1.1"],
                "validation_criteria": "Touches src/covered.py.",
            },
            {
                "ref": "#102",
                "path_cache": "1.102",
                "labels": ["covers:plan:A1:A1.2"],
                "validation_criteria": "Touches src/other.py.",
            },
            {
                "ref": "#200",
                "path_cache": "1.200",
                "state": "ready",
                "labels": ["deferred-from:plan:A2"],
                "validation_criteria": "Follow-up task owns src/deferred.py.",
            },
        ],
    )

    assert [row.status for row in report.rows] == [
        CoverageStatus.covered,
        CoverageStatus.invalid,
        CoverageStatus.missing,
        CoverageStatus.deferred,
    ]
    assert report.rows[0].leaves[0].leaf_task_ref == "#101"
    assert report.rows[3].deferral_target == "#200"


def test_db_source_ignores_filesystem_task_export(
    tmp_path: Path,
    temp_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.storage.projects import LocalProjectManager

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GOBBY_DATABASE_PATH", str(temp_db.db_path))
    project = LocalProjectManager(temp_db).create("project")
    (tmp_path / ".gobby").mkdir()
    (tmp_path / ".gobby" / "tasks.jsonl").write_text(
        (
            '{"ref":"#1","path_cache":"1"}\n'
            '{"ref":"#101","path_cache":"1.101","labels":["covers:plan:A1:A1.1"],'
            '"validation_criteria":"Touches src/covered.py."}\n'
        ),
        encoding="utf-8",
    )

    report = evaluate(
        plan=_plan(_section(_item("A1.1", "src/covered.py"))),
        plan_id="plan",
        plan_hash="hash",
        task_tree=TaskTreeSource.db,
        root_task_ref="#1",
        project_id=project.id,
    )

    assert report.rows[0].status is CoverageStatus.missing


def test_db_source_loads_live_task_records(
    temp_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.tasks import LocalTaskManager

    monkeypatch.setenv("GOBBY_DATABASE_PATH", str(temp_db.db_path))
    project = LocalProjectManager(temp_db).create("project")
    manager = LocalTaskManager(temp_db)
    root = manager.create_task(project.id, "Root")
    leaf = manager.create_task(
        project.id,
        "Leaf",
        parent_task_id=root.id,
        labels=["covers:plan:A1:A1.1"],
        validation_criteria="Touches src/covered.py.",
    )

    report = evaluate(
        plan=_plan(_section(_item("A1.1", "src/covered.py"))),
        plan_id="plan",
        plan_hash="hash",
        task_tree=TaskTreeSource.db,
        root_task_ref=f"#{root.seq_num}",
        project_id=project.id,
    )

    assert report.rows[0].status is CoverageStatus.covered
    assert report.rows[0].leaves[0].leaf_task_ref == f"#{leaf.seq_num}"


def test_evaluate_root_scope_excludes_other_subtree() -> None:
    plan_doc = _plan(_section(_item("A1.1", "src/covered.py")))

    report = evaluate(
        plan=plan_doc,
        plan_id="plan",
        plan_hash="hash",
        task_tree=TaskTreeSource.db,
        root_task_ref="#1",
        project_id="project",
        task_records=[
            {
                "ref": "#999",
                "path_cache": "2.999",
                "labels": ["covers:plan:A1:A1.1"],
                "validation_criteria": "Touches src/covered.py.",
            }
        ],
    )

    assert report.rows[0].status is CoverageStatus.missing


def test_stale_hash_raises() -> None:
    with pytest.raises(StaleHashError):
        evaluate(
            plan=_plan(_section(_item())),
            plan_id="plan",
            plan_hash="old",
            task_tree=TaskTreeSource.db,
            root_task_ref="#1",
            project_id="project",
            task_records=[],
        )


def test_matrix_file_rejects_scope(tmp_path: Path) -> None:
    matrix = tmp_path / "matrix.coverage.yaml"
    matrix.write_text("header:\n  plan_hash: hash\nrows: []\n", encoding="utf-8")
    with pytest.raises(MissingScopeError):
        evaluate(
            plan=_plan(_section(_item())),
            plan_id="plan",
            plan_hash="hash",
            task_tree=TaskTreeSource.matrix_file,
            root_task_ref="#1",
            project_id="project",
            matrix_file=matrix,
        )
