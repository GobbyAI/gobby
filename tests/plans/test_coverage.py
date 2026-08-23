from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.plans.coverage import (
    CoverageStatus,
    EvidenceKind,
    MissingScopeError,
    StaleHashError,
    TaskTreeSource,
    _coerce_task_record,
    _plan_node_hash,
    evaluate,
)
from gobby.plans.parser import (
    AcceptanceItem,
    ArtifactKind,
    Deferral,
    Kind,
    PlanDocument,
    PlanSection,
    parse_plan,
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


def _deferred_plan(tmp_path: Path, *, task_ref: str = "#999") -> tuple[Path, str]:
    path = tmp_path / "deferred-plan.md"
    path.write_text(
        f"""> **Plan ID:** plan

## A1 Deferred Work
`kind: deferred`

```yaml
deferral:
  task_ref: "{task_ref}"
  reason: "covered by follow-up"
  owner: "backend"
  original_acceptance_items:
    - A1.1
```
""",
        encoding="utf-8",
    )
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_exports_a4_public_api() -> None:
    assert EvidenceKind.none.value == "none"
    assert TaskTreeSource.db.value == "db"
    assert CoverageStatus.covered.value == "covered"


@pytest.mark.parametrize(
    ("raw_state", "expected"),
    [
        ({"is_closed": True, "is_escalated": True}, "closed"),
        ({"is_closed": False, "is_escalated": True}, "escalated"),
        (
            {
                "is_closed": False,
                "is_escalated": False,
                "current_stage": {"name": "development", "state": "in_progress"},
            },
            "in_progress",
        ),
        ("ready", "ready"),
        (None, "unknown"),
        ({"is_closed": "true", "current_stage": {"state": "ready"}}, "unknown"),
        ({"current_stage": {"state": 3}}, "unknown"),
    ],
    ids=["closed", "escalated", "stage", "string", "none", "invalid-bool", "invalid-stage"],
)
def test_coerce_task_record_reads_serialized_state(raw_state: object, expected: str) -> None:
    record = _coerce_task_record({"ref": "#1", "state": raw_state})

    assert record.state == expected


def test_coerce_task_record_missing_state_is_unknown() -> None:
    assert _coerce_task_record({"ref": "#1"}).state == "unknown"


def test_closed_serialized_task_record_rejects_deferral(tmp_path: Path) -> None:
    plan_path, plan_hash = _deferred_plan(tmp_path)

    report = evaluate(
        plan=plan_path,
        plan_id="plan",
        plan_hash=plan_hash,
        task_tree=TaskTreeSource.db,
        root_task_ref="#1",
        project_id="project",
        task_records=[
            {"ref": "#1", "path_cache": "1", "dependencies": ["#999"]},
            {
                "ref": "#999",
                "path_cache": "2.999",
                "state": {
                    "is_closed": True,
                    "is_escalated": False,
                    "current_stage": {"name": "development", "state": "ready"},
                },
                "labels": ["deferred-from:plan:A1"],
                "validation_criteria": "Follow-up owns A1.1.",
            },
        ],
    )

    assert report.rows[0].status is CoverageStatus.invalid


@pytest.mark.parametrize(
    ("closed_reason", "expected"),
    [
        ("completed", CoverageStatus.deferred),
        ("already_implemented", CoverageStatus.deferred),
        ("wont_fix", CoverageStatus.invalid),
        ("duplicate", CoverageStatus.invalid),
    ],
    ids=["completed", "already-implemented", "wont-fix", "duplicate"],
)
def test_task_record_carries_close_reason_into_deferral_validation(
    tmp_path: Path, closed_reason: str, expected: CoverageStatus
) -> None:
    """The store must forward `closed_reason`; state alone serializes both cases as "closed"."""
    plan_path, plan_hash = _deferred_plan(tmp_path)

    report = evaluate(
        plan=plan_path,
        plan_id="plan",
        plan_hash=plan_hash,
        task_tree=TaskTreeSource.db,
        root_task_ref="#1",
        project_id="project",
        task_records=[
            {"ref": "#1", "path_cache": "1", "dependencies": ["#999"]},
            {
                "ref": "#999",
                "path_cache": "2.999",
                "state": {"is_closed": True, "is_escalated": False},
                "closed_reason": closed_reason,
                "labels": ["deferred-from:plan:A1"],
                "validation_criteria": "Follow-up owns A1.1.",
            },
        ],
    )

    assert report.rows[0].status is expected


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


def test_evaluate_surfaces_invalid_covers_labels_without_cross_plan_leakage() -> None:
    report = evaluate(
        plan=_plan(_section(_item("A1.1", "src/covered.py"))),
        plan_id="plan",
        plan_hash="hash",
        task_tree=TaskTreeSource.db,
        root_task_ref="#1",
        project_id="project",
        task_records=[
            {"ref": "#1", "path_cache": "1"},
            {
                "ref": "#101",
                "path_cache": "1.101",
                "labels": ["covers:plan:A1:A1.1"],
                "validation_criteria": "Touches src/covered.py.",
            },
            {
                "ref": "#102",
                "path_cache": "1.102",
                "labels": ["covers:plan:A9:A9.1"],
            },
            {
                "ref": "#103",
                "path_cache": "1.103",
                "labels": ["covers:plan:A1:A1.9"],
            },
            {
                "ref": "#104",
                "path_cache": "1.104",
                "labels": ["covers:plan:A1"],
            },
            {
                "ref": "#105",
                "path_cache": "1.105",
                "labels": ["covers:other-plan:A9:A9.1"],
            },
        ],
    )

    assert report.rows[0].status is CoverageStatus.covered
    invalid_rows = [row for row in report.rows if row.status is CoverageStatus.invalid]
    assert len(invalid_rows) == 3
    assert [(row.section_id, row.item_id) for row in invalid_rows[:2]] == [
        ("A9", "A9.1"),
        ("A1", "A1.9"),
    ]
    assert invalid_rows[2].leaves[0].matched_artifact_ref == "covers:plan:A1"
    assert all("other-plan" not in row.leaves[0].matched_artifact_ref for row in invalid_rows)
    assert report.is_complete is False


@pytest.mark.parametrize(
    ("task_state", "labels", "expected_status"),
    [
        (None, (), CoverageStatus.invalid),
        ("closed", ("deferred-from:plan:A1",), CoverageStatus.invalid),
        ("ready", (), CoverageStatus.invalid),
        ("ready", ("deferred-from:plan:A1",), CoverageStatus.deferred),
    ],
    ids=["missing-task", "closed-task", "missing-provenance", "valid-open-task"],
)
def test_parsed_deferred_section_validates_task_and_provenance(
    tmp_path: Path,
    task_state: str | None,
    labels: tuple[str, ...],
    expected_status: CoverageStatus,
) -> None:
    plan_path, plan_hash = _deferred_plan(tmp_path)
    task_records: list[dict[str, object]] = [
        {"ref": "#1", "path_cache": "1", "dependencies": ["#999"]}
    ]
    if task_state is not None:
        task_records.append(
            {
                "ref": "#999",
                "path_cache": "1.999",
                "state": task_state,
                "labels": list(labels),
                "validation_criteria": "Follow-up owns A1.1.",
            }
        )

    report = evaluate(
        plan=plan_path,
        plan_id="plan",
        plan_hash=plan_hash,
        task_tree=TaskTreeSource.db,
        root_task_ref="#1",
        project_id="project",
        task_records=task_records,
    )

    assert len(report.rows) == 1
    assert report.rows[0].section_id == "A1"
    assert report.rows[0].item_id == "A1.1"
    assert report.rows[0].status is expected_status
    assert report.rows[0].deferral_target == "#999"
    assert report.is_complete is (expected_status is CoverageStatus.deferred)


def test_parsed_deferred_item_accepts_valid_covers_label(tmp_path: Path) -> None:
    plan_path, plan_hash = _deferred_plan(tmp_path)

    report = evaluate(
        plan=plan_path,
        plan_id="plan",
        plan_hash=plan_hash,
        task_tree=TaskTreeSource.db,
        root_task_ref="#1",
        project_id="project",
        task_records=[
            {"ref": "#1", "path_cache": "1"},
            {
                "ref": "#101",
                "path_cache": "1.101",
                "labels": ["covers:plan:A1:A1.1"],
                "validation_criteria": "Implements A1.1.",
            },
        ],
    )

    assert len(report.rows) == 1
    assert report.rows[0].status is CoverageStatus.covered
    assert report.rows[0].leaves[0].leaf_task_ref == "#101"


def test_matrix_reconciliation_recognizes_parsed_deferred_items(tmp_path: Path) -> None:
    plan_path, plan_hash = _deferred_plan(tmp_path)
    plan_doc = parse_plan(plan_path, parse_mode="draft")
    section = plan_doc.sections[0]
    assert section.deferral is not None
    item = section.deferral.original_acceptance_items[0]
    matrix = tmp_path / "deferred.coverage.yaml"
    matrix.write_text(
        yaml.safe_dump(
            {
                "header": {"plan_id": "plan", "plan_hash": plan_hash},
                "rows": [
                    {
                        "section_id": "A1",
                        "item_id": "A1.1",
                        "plan_node_hash": _plan_node_hash(section, item),
                        "status": "deferred",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    report = evaluate(
        plan=plan_path,
        plan_id="plan",
        plan_hash=plan_hash,
        task_tree=TaskTreeSource.matrix_file,
        matrix_file=matrix,
    )

    assert len(report.rows) == 1
    assert report.rows[0].status is CoverageStatus.deferred
    assert report.is_complete is True


def test_db_source_ignores_filesystem_task_export(
    tmp_path: Path,
    temp_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.storage.projects import LocalProjectManager

    monkeypatch.chdir(tmp_path)
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
        db=temp_db,
    )

    assert report.rows[0].status is CoverageStatus.missing


def test_db_source_loads_live_task_records(
    temp_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.tasks import LocalTaskManager

    project = LocalProjectManager(temp_db).create("project")
    manager = LocalTaskManager(temp_db)
    root = manager.create_task(
        project.id, "Root", validation_criteria="Test task completion is observable."
    )
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
        db=temp_db,
    )

    assert report.rows[0].status is CoverageStatus.covered
    assert report.rows[0].leaves[0].leaf_task_ref == f"#{leaf.seq_num}"


def test_db_loader_pages_in_sql_without_hierarchy_sort(
    temp_db: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#19878: the coverage loader must never trigger the hierarchical re-sort.

    The default `sort_by="hierarchy"` materializes and re-sorts the entire
    project task set for every page, which is O(pages x project) pure-Python
    CPU inside the daemon — enough to starve the event loop on large projects.
    """
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.tasks import LocalTaskManager

    project = LocalProjectManager(temp_db).create("project")
    manager = LocalTaskManager(temp_db)
    root = manager.create_task(
        project.id, "Root", validation_criteria="Test task completion is observable."
    )
    leaf = manager.create_task(
        project.id,
        "Leaf",
        parent_task_id=root.id,
        labels=["covers:plan:A1:A1.1"],
        validation_criteria="Touches src/covered.py.",
    )

    def _forbidden(tasks: Any) -> Any:
        raise AssertionError("coverage loading must page in SQL, not via hierarchy sort")

    monkeypatch.setattr(
        "gobby.storage.tasks._queries.order_tasks_hierarchically",
        _forbidden,
    )

    report = evaluate(
        plan=_plan(_section(_item("A1.1", "src/covered.py"))),
        plan_id="plan",
        plan_hash="hash",
        task_tree=TaskTreeSource.db,
        root_task_ref=f"#{root.seq_num}",
        project_id=project.id,
        db=temp_db,
    )

    assert report.rows[0].status is CoverageStatus.covered
    assert report.rows[0].leaves[0].leaf_task_ref == f"#{leaf.seq_num}"


def test_task_tree_source_hash_is_order_insensitive() -> None:
    """The tree hash identifies task-tree state, not the loader's query order."""
    records: list[dict[str, object]] = [
        {
            "ref": "#1",
            "path_cache": "1",
            "validation_criteria": "Test task completion is observable.",
        },
        {
            "ref": "#2",
            "path_cache": "1.2",
            "parent_ref": "#1",
            "labels": ["covers:plan:A1:A1.1"],
            "validation_criteria": "Touches src/covered.py.",
        },
    ]

    def _report(ordered: list[dict[str, object]]) -> Any:
        return evaluate(
            plan=_plan(_section(_item("A1.1", "src/covered.py"))),
            plan_id="plan",
            plan_hash="hash",
            task_tree=TaskTreeSource.db,
            root_task_ref="#1",
            project_id="project",
            task_records=ordered,
        )

    forward = _report(records)
    reversed_order = _report(list(reversed(records)))

    assert forward.header.task_tree_source_hash == reversed_order.header.task_tree_source_hash
    assert forward.rows[0].status is CoverageStatus.covered
    assert reversed_order.rows[0].status is CoverageStatus.covered


def test_db_deferral_uses_project_records_without_widening_root_scope(
    tmp_path: Path,
    temp_db: Any,
) -> None:
    from gobby.storage.projects import LocalProjectManager
    from gobby.storage.task_dependencies import TaskDependencyManager
    from gobby.storage.tasks import LocalTaskManager

    projects = LocalProjectManager(temp_db)
    project = projects.create("project")
    foreign_project = projects.create("foreign-project")
    manager = LocalTaskManager(temp_db)
    root = manager.create_task(
        project.id, "Root", validation_criteria="Test task completion is observable."
    )
    deferral = manager.create_task(
        project.id,
        "Project-wide deferral",
        labels=["deferred-from:plan:A1", "covers:plan:A1:A1.1"],
        validation_criteria="Follow-up owns A1.1.",
    )
    foreign_deferral = manager.create_task(
        foreign_project.id,
        "Foreign deferral",
        labels=["deferred-from:plan:A1", "covers:plan:A1:A1.1"],
        validation_criteria="Follow-up owns A1.1.",
    )
    dependencies = TaskDependencyManager(temp_db)
    dependencies.add_dependency(root.id, deferral.id)
    dependencies.add_dependency(root.id, foreign_deferral.id)
    plan_path, plan_hash = _deferred_plan(tmp_path, task_ref=f"#{deferral.seq_num}")

    report = evaluate(
        plan=plan_path,
        plan_id="plan",
        plan_hash=plan_hash,
        task_tree=TaskTreeSource.db,
        root_task_ref=f"#{root.seq_num}",
        project_id=project.id,
        db=temp_db,
    )

    assert report.rows[0].status is CoverageStatus.deferred
    assert report.rows[0].deferral_target == f"#{deferral.seq_num}"
    scoped_hash = report.header.task_tree_source_hash

    manager.create_task(
        project.id,
        "Unrelated project task",
        validation_criteria="Test task completion is observable.",
    )
    report_with_outsider = evaluate(
        plan=plan_path,
        plan_id="plan",
        plan_hash=plan_hash,
        task_tree=TaskTreeSource.db,
        root_task_ref=f"#{root.seq_num}",
        project_id=project.id,
        db=temp_db,
    )

    assert report_with_outsider.rows[0].status is CoverageStatus.deferred
    assert report_with_outsider.header.task_tree_source_hash == scoped_hash

    foreign_plan_dir = tmp_path / "foreign"
    foreign_plan_dir.mkdir()
    foreign_plan, foreign_plan_hash = _deferred_plan(
        foreign_plan_dir,
        task_ref=f"#{foreign_deferral.seq_num}",
    )
    foreign_report = evaluate(
        plan=foreign_plan,
        plan_id="plan",
        plan_hash=foreign_plan_hash,
        task_tree=TaskTreeSource.db,
        root_task_ref=f"#{root.seq_num}",
        project_id=project.id,
        db=temp_db,
    )

    assert foreign_report.rows[0].status is CoverageStatus.invalid


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
    unchecked_evaluate: Any = evaluate
    with pytest.raises(MissingScopeError):
        unchecked_evaluate(
            plan=_plan(_section(_item())),
            plan_id="plan",
            plan_hash="hash",
            task_tree=TaskTreeSource.matrix_file,
            root_task_ref="#1",
            project_id="project",
            matrix_file=matrix,
        )
