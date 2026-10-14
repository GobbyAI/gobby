"""Re-expansion keeps delivered leaves and adds only the new plan sections."""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from threading import Event, Thread
from typing import Any
from unittest.mock import MagicMock

import psycopg
import pytest
from psycopg import sql

from gobby.storage.expansion_runs import ExpansionRun, LocalExpansionRunManager
from gobby.storage.hub.postgres import PostgresHubDatabase
from gobby.storage.hub.postgres_pool import advisory_lock_keys
from gobby.storage.hub.protocol import HubDatabase, TaskDependencyMutation
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.tasks.expansion_qa_coverage import run_expansion_qa_coverage
from gobby.tasks.expansion_service import ExpansionService
from gobby.utils.machine_id import require_machine_id

pytestmark = pytest.mark.unit

COMMIT = "51151d39d1f7d0a882e08eec2a936052f03f901d"


def _service(temp_db: HubDatabase) -> ExpansionService:
    return ExpansionService(
        task_manager=LocalTaskManager(temp_db),
        llm_service=MagicMock(),
        run_manager=LocalExpansionRunManager(temp_db),
    )


def _save_run(
    service: ExpansionService,
    parent: Task,
    sample_project: dict[str, Any],
    spec: dict[str, Any],
) -> ExpansionRun:
    run = service.run_manager.create(
        parent_task_id=parent.id,
        project_id=sample_project["id"],
        triggering_session_id=None,
        input_source="plan",
    )
    service.run_manager.start(run.id)
    service.run_manager.save_compiled_spec(run.id, spec)
    return run


def _leaf(stable_id: str, section_id: str, title: str, validation: str) -> dict[str, Any]:
    return {
        "id": stable_id,
        "phase_id": "phase-1",
        "title": title,
        "category": "code",
        "task_type": "feature",
        "validation": validation,
        "description": f"Plan section `{section_id}`.\n\n{title}",
        "labels": [f"covers:plan:{section_id}:{section_id}.1"],
        "source_section_id": section_id,
    }


def _spec(
    leaves: list[dict[str, Any]],
    dependencies: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "contract_plan": True,
        "phases": [
            {
                "id": "phase-1",
                "title": "Phase 1",
                "task_ids": [leaf["id"] for leaf in leaves],
            }
        ],
        "tasks": leaves,
        "dependencies": dependencies or [],
    }


def _parent(service: ExpansionService, sample_project: dict[str, Any]) -> Task:
    return service.task_manager.create_task(
        project_id=sample_project["id"],
        title="Hub epic",
        task_type="epic",
        validation_criteria="Expanded children cover the plan.",
    )


def _session(temp_db: HubDatabase, sample_project: dict[str, Any]) -> Session:
    return SessionManager(temp_db).register(
        external_id="reexpand-seat",
        machine_id=require_machine_id(),
        source="codex",
        project_id=sample_project["id"],
    )


def _ids_for(report: dict[str, Any], key: str) -> dict[str, str]:
    rows = report[key]
    assert isinstance(rows, list)
    mapped: dict[str, str] = {}
    for row in rows:
        assert isinstance(row, dict)
        section_id = row["section_id"]
        task_id = row["task_id"]
        assert isinstance(section_id, str)
        assert isinstance(task_id, str)
        mapped[section_id] = task_id
    return mapped


def _task_id(run: Any, stable_id: str) -> str:
    mapping = run.task_id_map
    assert isinstance(mapping, dict)
    task_id = mapping[stable_id]
    assert isinstance(task_id, str)
    return task_id


def _report(run: Any) -> dict[str, Any]:
    checkpoints = run.checkpoints
    assert isinstance(checkpoints, dict)
    report = checkpoints["reconciliation"]
    assert isinstance(report, dict)
    return report


def test_reexpand_preserves_closed_leaf_reuses_open_leaf_and_adds_new_section(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    service = _service(temp_db)
    parent = _parent(service, sample_project)
    first = _spec(
        [
            _leaf("leaf-11", "1.1", "Hub-node contract", "Touches src/keep.py."),
            _leaf("leaf-12", "1.2", "Open section", "Touches src/reuse.py."),
        ],
        [{"task_id": "leaf-12", "depends_on": "leaf-11"}],
    )
    applied = service.apply_run(
        _save_run(service, parent, sample_project, first).id, session_id=None
    )
    kept_id = _task_id(applied, "leaf-11")
    reused_id = _task_id(applied, "leaf-12")
    service.task_manager.close_task_with_commit(kept_id, COMMIT, reason="completed", force=True)

    amended = _spec(
        [
            _leaf("again-11", "1.1", "Hub-node contract renamed", "Touches src/keep.py."),
            _leaf("again-12", "1.2", "Open section amended", "Touches src/reuse.py."),
            _leaf("new-13", "1.3", "Added section", "Touches src/add.py."),
        ],
        [{"task_id": "new-13", "depends_on": "again-11"}],
    )
    reconciled = service.apply_run(
        _save_run(service, parent, sample_project, amended).id, session_id=None
    )

    report = _report(reconciled)
    assert _ids_for(report, "preserved") == {"1.1": kept_id}
    assert _ids_for(report, "reused") == {"1.2": reused_id}
    assert _ids_for(report, "created") == {"1.3": _task_id(reconciled, "new-13")}
    assert report["removed"] == []
    assert report["untouched"] == []
    kept = service.task_manager.get_task(kept_id)
    assert kept.closed_at is not None
    assert kept.closed_reason == "completed"
    assert kept.title == "Hub-node contract"
    reused = service.task_manager.get_task(reused_id)
    assert reused.closed_at is None
    assert reused.title == "Open section amended"
    created_id = _task_id(reconciled, "new-13")
    assert created_id not in {kept_id, reused_id}
    children = service.task_manager.list_tasks(parent_task_id=parent.id)
    assert sorted(child.title for child in children) == [
        "Added section",
        "Hub-node contract",
        "Open section amended",
    ]
    blockers = {dep.depends_on for dep in service.dep_manager.get_blockers(created_id)}
    assert kept_id in blockers


def test_reexpand_reports_claimed_target_without_reset_or_reassignment(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    service = _service(temp_db)
    parent = _parent(service, sample_project)
    session = _session(temp_db, sample_project)
    first = _spec([_leaf("leaf-11", "1.1", "Claimed section", "Touches src/keep.py.")])
    applied = service.apply_run(
        _save_run(service, parent, sample_project, first).id, session_id=None
    )
    claimed_id = _task_id(applied, "leaf-11")
    service.task_manager.claim_task(claimed_id, session.id)

    amended = _spec([_leaf("again-11", "1.1", "Stolen title", "Touches src/keep.py.")])
    reconciled = service.apply_run(
        _save_run(service, parent, sample_project, amended).id, session_id=None
    )

    report = _report(reconciled)
    assert report["untouched"] == [
        {"section_id": "1.1", "task_id": claimed_id, "reason": "claimed"}
    ]
    assert report["preserved"] == []
    assert report["reused"] == []
    assert report["created"] == []
    task = service.task_manager.get_task(claimed_id)
    assert task.claimed_by_session_id == session.id
    assert task.title == "Claimed section"
    assert _task_id(reconciled, "again-11") == claimed_id
    assert len(service.task_manager.list_tasks(parent_task_id=parent.id)) == 1


def test_reexpand_reports_in_progress_target_without_changing_it(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    service = _service(temp_db)
    parent = _parent(service, sample_project)
    session = _session(temp_db, sample_project)
    service.task_manager.initialize_task_manifest(
        parent.id,
        stage_names=["development", "pr", "merge"],
    )
    first = _spec([_leaf("leaf-11", "1.1", "Started section", "Touches src/keep.py.")])
    applied = service.apply_run(
        _save_run(service, parent, sample_project, first).id, session_id=None
    )
    started_id = _task_id(applied, "leaf-11")
    service.task_manager.stage_states.start_stage(
        started_id, "development", by_session_id=session.id
    )

    amended = _spec([_leaf("again-11", "1.1", "Rewritten while active", "Touches src/keep.py.")])
    reconciled = service.apply_run(
        _save_run(service, parent, sample_project, amended).id, session_id=None
    )

    report = _report(reconciled)
    assert report["untouched"] == [
        {"section_id": "1.1", "task_id": started_id, "reason": "in_progress"}
    ]
    task = service.task_manager.get_task(started_id)
    assert task.title == "Started section"
    assert _task_id(reconciled, "again-11") == started_id


def test_reexpand_reports_removed_section_and_keeps_the_task(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    service = _service(temp_db)
    parent = _parent(service, sample_project)
    first = _spec(
        [
            _leaf("leaf-11", "1.1", "Kept section", "Touches src/keep.py."),
            _leaf("leaf-12", "1.2", "Removed section", "Touches src/gone.py."),
        ]
    )
    applied = service.apply_run(
        _save_run(service, parent, sample_project, first).id, session_id=None
    )
    kept_id = _task_id(applied, "leaf-11")
    removed_id = _task_id(applied, "leaf-12")

    amended = _spec([_leaf("again-11", "1.1", "Kept section amended", "Touches src/keep.py.")])
    reconciled = service.apply_run(
        _save_run(service, parent, sample_project, amended).id, session_id=None
    )

    report = _report(reconciled)
    assert report["removed"] == [{"section_id": "1.2", "task_id": removed_id}]
    removed = service.task_manager.get_task(removed_id)
    assert removed.title == "Removed section"
    assert removed.closed_at is None
    assert _ids_for(report, "reused") == {"1.1": kept_id}
    assert service.task_manager.get_task(removed_id).id == removed_id


def test_reexpand_coverage_counts_preserved_closed_leaf(
    temp_db: HubDatabase, sample_project: dict[str, Any], tmp_path: Path
) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text(
        """> **Plan ID:** plan

## 1.1 Keep [category: code]
`kind: deliverable`

Keep the delivered section.

**Acceptance:**
- 1.1.1 - Kept. file: `src/keep.py`

## 1.2 Reuse [category: code]
`kind: deliverable`

Reuse the open section.

**Acceptance:**
- 1.2.1 - Reused. file: `src/reuse.py`

## 1.3 Add [category: code]
`kind: deliverable`

Add the new section.

**Acceptance:**
- 1.3.1 - Added. file: `src/add.py`
""",
        encoding="utf-8",
    )
    plan_hash = hashlib.sha256(plan.read_bytes()).hexdigest()
    service = _service(temp_db)
    parent = _parent(service, sample_project)
    first = _spec(
        [
            _leaf("leaf-11", "1.1", "Hub-node contract", "Touches src/keep.py."),
            _leaf("leaf-12", "1.2", "Open section", "Touches src/reuse.py."),
        ]
    )
    applied = service.apply_run(
        _save_run(service, parent, sample_project, first).id, session_id=None
    )
    kept_id = _task_id(applied, "leaf-11")
    kept = service.task_manager.close_task_with_commit(
        kept_id, COMMIT, reason="completed", force=True
    )
    amended = _spec(
        [
            _leaf("again-11", "1.1", "Hub-node contract", "Touches src/keep.py."),
            _leaf("again-12", "1.2", "Open section amended", "Touches src/reuse.py."),
            _leaf("new-13", "1.3", "Added section", "Touches src/add.py."),
        ]
    )
    reconciled = service.apply_run(
        _save_run(service, parent, sample_project, amended).id, session_id=None
    )

    result = run_expansion_qa_coverage(
        task_manager=service.task_manager,
        run=reconciled,
        repo_path=tmp_path,
        plan_path="plan.md",
        plan_id="plan",
        plan_hash=plan_hash,
        root_task_ref=str(parent.seq_num),
        project_id=sample_project["id"],
        is_spawned_agent=False,
    )

    assert result["ok"] is True
    assert result["passed"] is True
    qa_result = result["qa_result"]
    assert isinstance(qa_result, dict)
    coverage = qa_result["coverage"]
    assert isinstance(coverage, dict)
    rows = coverage["rows"]
    assert isinstance(rows, list)
    covered = next(
        row
        for row in rows
        if isinstance(row, dict) and row["section_id"] == "1.1" and row["item_id"] == "1.1.1"
    )
    assert covered["status"] == "covered"
    leaves = covered["leaves"]
    assert isinstance(leaves, list)
    first_leaf = leaves[0]
    assert isinstance(first_leaf, dict)
    assert first_leaf["leaf_task_ref"] == f"#{kept.seq_num}"


def _blocker_ids(service: ExpansionService, task_id: str) -> set[str]:
    return {dep.depends_on for dep in service.dep_manager.get_blockers(task_id)}


def _wait_for_lock(
    postgres_database_url: str,
    finished: Event,
    application_name: str,
    *,
    timeout: float = 5.0,
) -> None:
    deadline = time.monotonic() + timeout
    with psycopg.connect(postgres_database_url) as monitor:
        while time.monotonic() < deadline:
            if finished.is_set():
                raise AssertionError("writer finished before waiting on a lock")
            row = monitor.execute(
                """
                SELECT count(*)
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND pid <> pg_backend_pid()
                  AND application_name = %s
                  AND wait_event_type = 'Lock'
                """,
                (application_name,),
            ).fetchone()
            if row is not None and int(row[0]) >= 1:
                return
            time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {application_name} to block on a lock")


def _schema_name(db: HubDatabase) -> str:
    row = db.fetchone("SELECT current_schema() AS schema")
    assert row is not None
    return str(row["schema"])


@pytest.mark.parametrize("transition", ["claim", "close"])
def test_reexpand_transition_committed_first_is_not_rewritten(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    postgres_database_url: str,
    transition: str,
) -> None:
    assert isinstance(temp_db, PostgresHubDatabase)
    service = _service(temp_db)
    parent = _parent(service, sample_project)
    session = _session(temp_db, sample_project)
    first = _spec([_leaf("leaf-11", "1.1", "Original title", "Touches src/keep.py.")])
    applied = service.apply_run(
        _save_run(service, parent, sample_project, first).id, session_id=None
    )
    leaf_id = _task_id(applied, "leaf-11")
    amended = _spec([_leaf("again-11", "1.1", "Rewritten title", "Touches src/keep.py.")])
    amended_run = _save_run(service, parent, sample_project, amended)
    writer_db = PostgresHubDatabase(temp_db.conninfo)
    writer_db.open()
    finished = Event()
    errors: list[BaseException] = []

    def run_apply() -> None:
        try:
            _service(writer_db).apply_run(amended_run.id, session_id=None)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    writer = Thread(target=run_apply, daemon=True)
    schema = _schema_name(temp_db)
    try:
        with psycopg.connect(postgres_database_url) as holder:
            holder.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            holder.execute("BEGIN")
            try:
                locked = holder.execute(
                    "SELECT id FROM tasks WHERE id = %s FOR UPDATE",
                    (leaf_id,),
                ).fetchone()
                assert locked is not None
                writer.start()
                _wait_for_lock(postgres_database_url, finished, writer_db.application_name)
                if transition == "claim":
                    holder.execute(
                        """
                        UPDATE tasks
                        SET claimed_by_session_id = %s, updated_at = now()
                        WHERE id = %s
                        """,
                        (session.id, leaf_id),
                    )
                else:
                    holder.execute(
                        """
                        UPDATE tasks
                        SET closed_at = now(),
                            closed_reason = 'completed',
                            closed_commit_sha = %s,
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (COMMIT, leaf_id),
                    )
                holder.execute("COMMIT")
            except Exception:
                holder.execute("ROLLBACK")
                raise
        assert finished.wait(timeout=5)
        writer.join(timeout=1)
        if errors:
            raise errors[0]
        task = service.task_manager.get_task(leaf_id)
        assert task.title == "Original title"
        report = _report(_service(writer_db).run_manager.get(amended_run.id))
        if transition == "claim":
            assert task.claimed_by_session_id == session.id
            assert report["untouched"] == [
                {"section_id": "1.1", "task_id": leaf_id, "reason": "claimed"}
            ]
        else:
            assert task.closed_at is not None
            assert task.closed_reason == "completed"
            assert report["preserved"] == [{"section_id": "1.1", "task_id": leaf_id}]
    finally:
        writer_db.close()


def test_reexpand_update_commits_before_claim(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    postgres_database_url: str,
) -> None:
    assert isinstance(temp_db, PostgresHubDatabase)
    service = _service(temp_db)
    parent = _parent(service, sample_project)
    session = _session(temp_db, sample_project)
    first = _spec([_leaf("leaf-11", "1.1", "Original title", "Touches src/keep.py.")])
    applied = service.apply_run(
        _save_run(service, parent, sample_project, first).id, session_id=None
    )
    leaf_id = _task_id(applied, "leaf-11")
    amended = _spec([_leaf("again-11", "1.1", "Rewritten title", "Touches src/keep.py.")])
    amended_run = _save_run(service, parent, sample_project, amended)
    writer_db = PostgresHubDatabase(temp_db.conninfo)
    writer_db.open()
    apply_finished = Event()
    claim_finished = Event()
    errors: list[BaseException] = []

    def run_apply() -> None:
        try:
            _service(writer_db).apply_run(amended_run.id, session_id=None)
        except BaseException as exc:
            errors.append(exc)
        finally:
            apply_finished.set()

    def run_claim() -> None:
        try:
            service.task_manager.claim_task(leaf_id, session.id)
        except BaseException as exc:
            errors.append(exc)
        finally:
            claim_finished.set()

    apply_thread = Thread(target=run_apply, daemon=True)
    claim_thread = Thread(target=run_claim, daemon=True)
    schema = _schema_name(temp_db)
    lock_key = advisory_lock_keys(TaskDependencyMutation())[0]
    try:
        with psycopg.connect(postgres_database_url) as holder:
            holder.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            holder.execute("BEGIN")
            try:
                holder.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))
                apply_thread.start()
                _wait_for_lock(postgres_database_url, apply_finished, writer_db.application_name)
                claim_thread.start()
                _wait_for_lock(postgres_database_url, claim_finished, temp_db.application_name)
                holder.execute("COMMIT")
            except Exception:
                holder.execute("ROLLBACK")
                raise
        assert apply_finished.wait(timeout=5)
        assert claim_finished.wait(timeout=5)
        apply_thread.join(timeout=1)
        claim_thread.join(timeout=1)
        if errors:
            raise errors[0]
        task = service.task_manager.get_task(leaf_id)
        assert task.title == "Rewritten title"
        assert task.claimed_by_session_id == session.id
    finally:
        writer_db.close()


def test_reexpand_skips_outgoing_edges_on_protected_leaves(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    service = _service(temp_db)
    parent = _parent(service, sample_project)
    session = _session(temp_db, sample_project)
    service.task_manager.initialize_task_manifest(
        parent.id,
        stage_names=["development", "pr", "merge"],
    )
    first = _spec(
        [
            _leaf("leaf-11", "1.1", "Closed section", "Touches src/keep.py."),
            _leaf("leaf-12", "1.2", "Claimed section", "Touches src/claim.py."),
            _leaf("leaf-13", "1.3", "Started section", "Touches src/start.py."),
        ]
    )
    applied = service.apply_run(
        _save_run(service, parent, sample_project, first).id, session_id=None
    )
    closed_id = _task_id(applied, "leaf-11")
    claimed_id = _task_id(applied, "leaf-12")
    started_id = _task_id(applied, "leaf-13")
    service.task_manager.close_task_with_commit(closed_id, COMMIT, reason="completed", force=True)
    service.task_manager.claim_task(claimed_id, session.id)
    service.task_manager.stage_states.start_stage(
        started_id, "development", by_session_id=session.id
    )
    amended = _spec(
        [
            _leaf("again-11", "1.1", "Closed renamed", "Touches src/keep.py."),
            _leaf("again-12", "1.2", "Claimed renamed", "Touches src/claim.py."),
            _leaf("again-13", "1.3", "Started renamed", "Touches src/start.py."),
            _leaf("new-14", "1.4", "Added blocker", "Touches src/add.py."),
            _leaf("new-15", "1.5", "Depends on closed", "Touches src/keep.py."),
        ],
        [
            {"task_id": "again-11", "depends_on": "new-14"},
            {"task_id": "again-12", "depends_on": "new-14"},
            {"task_id": "again-13", "depends_on": "new-14"},
            {"task_id": "new-15", "depends_on": "again-11"},
        ],
    )
    reconciled = service.apply_run(
        _save_run(service, parent, sample_project, amended).id, session_id=None
    )

    assert _blocker_ids(service, closed_id) == set()
    assert _blocker_ids(service, claimed_id) == set()
    assert _blocker_ids(service, started_id) == set()
    new_id = _task_id(reconciled, "new-14")
    assert closed_id in _blocker_ids(service, _task_id(reconciled, "new-15"))
    skipped = _report(reconciled)["skipped_edges"]
    assert isinstance(skipped, list)
    by_section: dict[str, dict[str, Any]] = {}
    for row in skipped:
        assert isinstance(row, dict)
        section_id = row["section_id"]
        assert isinstance(section_id, str)
        by_section[section_id] = row
    assert by_section["1.1"] == {
        "section_id": "1.1",
        "task_id": closed_id,
        "depends_on": new_id,
    }
    assert by_section["1.2"]["task_id"] == claimed_id
    assert by_section["1.2"]["depends_on"] == new_id
    assert by_section["1.3"]["task_id"] == started_id
    assert by_section["1.3"]["depends_on"] == new_id
    assert service.task_manager.get_task(closed_id).title == "Closed section"
    assert service.task_manager.get_task(claimed_id).title == "Claimed section"
    assert service.task_manager.get_task(started_id).title == "Started section"


def test_reexpand_reopens_closed_phase_for_a_new_section(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    service = _service(temp_db)
    parent = _parent(service, sample_project)
    delivered = _leaf("leaf-11", "1.1", "Delivered section", "Touches src/keep.py.")
    other = _leaf("leaf-21", "2.1", "Other phase", "Touches src/other.py.")
    other["phase_id"] = "phase-2"
    first = _spec([delivered, other])
    first["phases"] = [
        {"id": "phase-1", "title": "Phase 1", "task_ids": ["leaf-11"]},
        {"id": "phase-2", "title": "Phase 2", "task_ids": ["leaf-21"]},
    ]
    applied = service.apply_run(
        _save_run(service, parent, sample_project, first).id, session_id=None
    )
    delivered_id = _task_id(applied, "leaf-11")
    service.task_manager.close_task_with_commit(
        delivered_id, COMMIT, reason="completed", force=True
    )
    checkpoints = applied.checkpoints
    assert isinstance(checkpoints, dict)
    phase_map = checkpoints["phase_parent_map"]
    assert isinstance(phase_map, dict)
    phase_id = phase_map["phase-1"]
    assert isinstance(phase_id, str)
    assert service.task_manager.get_task(phase_id).closed_at is not None

    added = _leaf("new-12", "1.2", "Added in phase 1", "Touches src/add.py.")
    kept = _leaf("again-11", "1.1", "Delivered section", "Touches src/keep.py.")
    still = _leaf("again-21", "2.1", "Other phase", "Touches src/other.py.")
    still["phase_id"] = "phase-2"
    amended = _spec([kept, added, still])
    amended["phases"] = [
        {"id": "phase-1", "title": "Phase 1", "task_ids": ["again-11", "new-12"]},
        {"id": "phase-2", "title": "Phase 2", "task_ids": ["again-21"]},
    ]
    reconciled = service.apply_run(
        _save_run(service, parent, sample_project, amended).id, session_id=None
    )
    created_id = _task_id(reconciled, "new-12")
    created = service.task_manager.get_task(created_id)
    assert created.parent_task_id == phase_id
    assert service.task_manager.get_task(phase_id).closed_at is None


def test_reexpand_reuses_placeholder_and_reports_a_dropped_holder(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    service = _service(temp_db)
    parent = _parent(service, sample_project)
    leaf = _leaf("1.1::single", "1.1", "Gate", "Touches src/keep.py.")
    first = _spec([leaf])
    first["plan_id"] = "plan"
    first["deferrals"] = [
        {
            "section_id": "D1",
            "title": "Deferred holder",
            "task_ref": "#TBD-created-at-expansion",
            "depends_on": [],
            "reason": "later",
            "owner": "backend",
            "original_acceptance_items": [],
        }
    ]
    applied = service.apply_run(
        _save_run(service, parent, sample_project, first).id, session_id=None
    )
    assert applied.checkpoints is not None
    holders = applied.checkpoints["deferral_task_map"]
    assert isinstance(holders, dict)
    holder_id = holders["D1"]
    assert isinstance(holder_id, str)

    again = _spec([_leaf("1.1::single", "1.1", "Gate amended", "Touches src/keep.py.")])
    again["plan_id"] = "plan"
    again_deferral = dict(first["deferrals"][0])
    again_deferral["depends_on"] = ["1.1"]
    again["deferrals"] = [again_deferral]
    reapplied = service.apply_run(
        _save_run(service, parent, sample_project, again).id, session_id=None
    )
    assert reapplied.checkpoints is not None
    reused = reapplied.checkpoints["deferral_task_map"]
    assert isinstance(reused, dict)
    assert reused["D1"] == holder_id
    labeled = [
        task
        for task in service.task_manager.list_tasks(parent_task_id=parent.id)
        if "deferred-from:plan:D1" in (task.labels or [])
    ]
    assert len(labeled) == 1
    assert _blocker_ids(service, holder_id) == {_task_id(reapplied, "1.1::single")}

    dropped = _spec([_leaf("later-11", "1.1", "Gate stays", "Touches src/keep.py.")])
    dropped["plan_id"] = "plan"
    dropped["deferrals"] = []
    reconciled = service.apply_run(
        _save_run(service, parent, sample_project, dropped).id, session_id=None
    )
    assert _report(reconciled)["removed"] == [{"section_id": "D1", "task_id": holder_id}]
    holder = service.task_manager.get_task(holder_id)
    assert holder.title == "Deferred holder"
    assert holder.closed_at is None


def test_reexpand_reused_leaf_uses_created_leaf_labels(
    temp_db: HubDatabase, sample_project: dict[str, Any]
) -> None:
    service = _service(temp_db)
    parent = _parent(service, sample_project)
    leaf = _leaf("leaf-11", "1.1", "Grouped section", "Touches src/keep.py.")
    leaf["execution_group"] = "wave"
    applied = service.apply_run(
        _save_run(service, parent, sample_project, _spec([leaf])).id, session_id=None
    )
    amended_leaf = _leaf("again-11", "1.1", "Grouped section amended", "Touches src/keep.py.")
    amended_leaf["execution_group"] = "wave"
    reconciled = service.apply_run(
        _save_run(service, parent, sample_project, _spec([amended_leaf])).id,
        session_id=None,
    )
    reused = service.task_manager.get_task(_task_id(applied, "leaf-11"))
    labels = set(reused.labels or [])
    assert "parallel:wave" in labels
    assert f"expansion-run:{reconciled.id}" in labels
    assert "covers:plan:1.1:1.1.1" in labels


def _phase_leaf(stable_id: str, section_id: str, phase_id: str, title: str) -> dict[str, Any]:
    row = _leaf(stable_id, section_id, title, "Touches src/keep.py.")
    row["phase_id"] = phase_id
    return row


def test_reexpand_stage_start_blocks_until_apply_commits(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    postgres_database_url: str,
) -> None:
    """Apply holds the stage rows, so a second connection's start waits."""
    assert isinstance(temp_db, PostgresHubDatabase)
    service = _service(temp_db)
    parent = _parent(service, sample_project)
    session = _session(temp_db, sample_project)
    service.task_manager.initialize_task_manifest(
        parent.id, stage_names=["development", "pr", "merge"]
    )
    first = _spec([_leaf("leaf-11", "1.1", "Original title", "Touches src/keep.py.")])
    applied = service.apply_run(
        _save_run(service, parent, sample_project, first).id, session_id=None
    )
    leaf_id = _task_id(applied, "leaf-11")
    amended = _spec([_leaf("again-11", "1.1", "Rewritten title", "Touches src/keep.py.")])
    amended_run = _save_run(service, parent, sample_project, amended)
    writer_db = PostgresHubDatabase(temp_db.conninfo)
    writer_db.open()
    apply_finished = Event()
    start_finished = Event()
    errors: list[BaseException] = []

    def run_apply() -> None:
        try:
            _service(writer_db).apply_run(amended_run.id, session_id=None)
        except BaseException as exc:
            errors.append(exc)
        finally:
            apply_finished.set()

    def run_start() -> None:
        try:
            service.task_manager.stage_states.start_stage(
                leaf_id, "development", by_session_id=session.id
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            start_finished.set()

    apply_thread = Thread(target=run_apply, daemon=True)
    start_thread = Thread(target=run_start, daemon=True)
    schema = _schema_name(temp_db)
    lock_key = advisory_lock_keys(TaskDependencyMutation())[0]
    try:
        with psycopg.connect(postgres_database_url) as holder:
            holder.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            holder.execute("BEGIN")
            try:
                holder.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))
                apply_thread.start()
                _wait_for_lock(postgres_database_url, apply_finished, writer_db.application_name)
                start_thread.start()
                _wait_for_lock(postgres_database_url, start_finished, temp_db.application_name)
                holder.execute("COMMIT")
            except Exception:
                holder.execute("ROLLBACK")
                raise
        assert apply_finished.wait(timeout=5)
        assert start_finished.wait(timeout=5)
        apply_thread.join(timeout=1)
        start_thread.join(timeout=1)
        if errors:
            raise errors[0]
        task = service.task_manager.get_task(leaf_id)
        assert task.title == "Rewritten title"
        stage = service.task_manager.stage_states.get(leaf_id, "development")
        assert stage is not None
        assert stage.state == "in_progress"
    finally:
        writer_db.close()


def test_reexpand_stage_progress_committed_first_is_not_rewritten(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    postgres_database_url: str,
) -> None:
    """A stage start that commits before apply classifies the leaf as in progress."""
    assert isinstance(temp_db, PostgresHubDatabase)
    service = _service(temp_db)
    parent = _parent(service, sample_project)
    service.task_manager.initialize_task_manifest(
        parent.id, stage_names=["development", "pr", "merge"]
    )
    first = _spec([_leaf("leaf-11", "1.1", "Original title", "Touches src/keep.py.")])
    applied = service.apply_run(
        _save_run(service, parent, sample_project, first).id, session_id=None
    )
    leaf_id = _task_id(applied, "leaf-11")
    amended = _spec([_leaf("again-11", "1.1", "Rewritten title", "Touches src/keep.py.")])
    amended_run = _save_run(service, parent, sample_project, amended)
    writer_db = PostgresHubDatabase(temp_db.conninfo)
    writer_db.open()
    finished = Event()
    errors: list[BaseException] = []

    def run_apply() -> None:
        try:
            _service(writer_db).apply_run(amended_run.id, session_id=None)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    writer = Thread(target=run_apply, daemon=True)
    schema = _schema_name(temp_db)
    try:
        with psycopg.connect(postgres_database_url) as holder:
            holder.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            holder.execute("BEGIN")
            try:
                holder.execute(
                    """
                    SELECT stage_name
                      FROM task_stage_states
                     WHERE task_id = %s
                     ORDER BY position, stage_name
                       FOR UPDATE
                    """,
                    (leaf_id,),
                )
                writer.start()
                _wait_for_lock(postgres_database_url, finished, writer_db.application_name)
                holder.execute(
                    """
                    UPDATE task_stage_states
                       SET state = 'in_progress', entered_at = now(), updated_at = now()
                     WHERE task_id = %s AND stage_name = 'development'
                    """,
                    (leaf_id,),
                )
                holder.execute("COMMIT")
            except Exception:
                holder.execute("ROLLBACK")
                raise
        assert finished.wait(timeout=5)
        writer.join(timeout=1)
        if errors:
            raise errors[0]
        task = service.task_manager.get_task(leaf_id)
        assert task.title == "Original title"
        report = _report(_service(writer_db).run_manager.get(amended_run.id))
        assert report["untouched"] == [
            {"section_id": "1.1", "task_id": leaf_id, "reason": "in_progress"}
        ]
    finally:
        writer_db.close()


def test_reexpand_close_of_removed_leaf_does_not_deadlock_on_root(
    temp_db: HubDatabase,
    sample_project: dict[str, Any],
    postgres_database_url: str,
) -> None:
    """Apply locks phase epics before the root, so a close holding the phase can take it."""
    assert isinstance(temp_db, PostgresHubDatabase)
    service = _service(temp_db)
    parent = _parent(service, sample_project)

    def spec_for(
        phase_one: list[dict[str, Any]], phase_two: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "version": 1,
            "contract_plan": True,
            "phases": [
                {"id": "phase-1", "title": "Phase 1", "task_ids": [row["id"] for row in phase_one]},
                {"id": "phase-2", "title": "Phase 2", "task_ids": [row["id"] for row in phase_two]},
            ],
            "tasks": [*phase_one, *phase_two],
            "dependencies": [],
        }

    first = spec_for(
        [
            _phase_leaf("leaf-a", "1.1", "phase-1", "Kept"),
            _phase_leaf("leaf-c", "1.2", "phase-1", "Removed"),
        ],
        [_phase_leaf("leaf-b", "2.1", "phase-2", "Other")],
    )
    applied = service.apply_run(
        _save_run(service, parent, sample_project, first).id, session_id=None
    )
    kept_id = _task_id(applied, "leaf-a")
    removed_id = _task_id(applied, "leaf-c")
    service.task_manager.close_task_with_commit(kept_id, COMMIT, reason="completed", force=True)
    assert applied.checkpoints is not None
    phase_map = applied.checkpoints["phase_parent_map"]
    assert isinstance(phase_map, dict)
    phase_id = phase_map["phase-1"]
    assert isinstance(phase_id, str)
    amended = spec_for(
        [
            _phase_leaf("again-a", "1.1", "phase-1", "Kept"),
            _phase_leaf("new-n", "1.3", "phase-1", "Added"),
        ],
        [_phase_leaf("again-b", "2.1", "phase-2", "Other")],
    )
    amended_run = _save_run(service, parent, sample_project, amended)
    writer_db = PostgresHubDatabase(temp_db.conninfo)
    writer_db.open()
    finished = Event()
    errors: list[BaseException] = []

    def run_apply() -> None:
        try:
            _service(writer_db).apply_run(amended_run.id, session_id=None)
        except BaseException as exc:
            errors.append(exc)
        finally:
            finished.set()

    writer = Thread(target=run_apply, daemon=True)
    schema = _schema_name(temp_db)
    try:
        with psycopg.connect(postgres_database_url) as holder:
            holder.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
            holder.execute("BEGIN")
            try:
                holder.execute("SELECT id FROM tasks WHERE id = %s FOR UPDATE", (removed_id,))
                holder.execute("SELECT id FROM tasks WHERE id = %s FOR UPDATE", (phase_id,))
                writer.start()
                _wait_for_lock(postgres_database_url, finished, writer_db.application_name)
                holder.execute("SET lock_timeout = '2s'")
                locked_root = holder.execute(
                    "SELECT id FROM tasks WHERE id = %s FOR UPDATE",
                    (parent.id,),
                ).fetchone()
                assert locked_root is not None
                holder.execute(
                    """
                    UPDATE tasks
                       SET closed_at = now(),
                           closed_reason = 'completed',
                           closed_commit_sha = %s,
                           updated_at = now()
                     WHERE id = %s
                    """,
                    (COMMIT, removed_id),
                )
                holder.execute("COMMIT")
            except Exception:
                holder.execute("ROLLBACK")
                raise
        assert finished.wait(timeout=5)
        writer.join(timeout=1)
        if errors:
            raise errors[0]
        removed = service.task_manager.get_task(removed_id)
        assert removed.closed_at is not None
        reconciled = _service(writer_db).run_manager.get(amended_run.id)
        added = service.task_manager.get_task(_task_id(reconciled, "new-n"))
        assert added.parent_task_id == phase_id
    finally:
        writer_db.close()
