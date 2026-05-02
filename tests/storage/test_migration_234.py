"""Red tests for migration 234 stage-registry bootstrap."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import (
    BASELINE_VERSION,
    MIGRATIONS,
    _apply_baseline,
    _run_migration_list,
    get_current_version,
    run_migrations,
)

pytestmark = pytest.mark.unit

CANONICAL_STAGE_NAMES = [
    "ideation",
    "research",
    "architecture",
    "prd",
    "planning",
    "test_arch",
    "expansion",
    "development",
    "holistic_qa",
    "pr",
    "merge",
]
DROPPED_STAGE_NAMES = {"adversarial_review", "expansion_qa", "code_review_qa"}
DISCOVERY_DEFAULT_AGENTS = {
    "ideation": "analyst",
    "research": "researcher",
    "architecture": "architect",
    "prd": "product-manager",
}
ARTIFACT_REPORT_COLUMNS = {
    "pr_review_report",
    "structured_pr_verdict",
    "merge_campaign_report",
}
PERSONAL_PROJECT_ID = "00000000-0000-0000-0000-000000060887"
STAGES_YAML = Path("src/gobby/install/shared/registry/stages.yaml")


def _column_info(db: LocalDatabase, table_name: str) -> dict[str, dict[str, Any]]:
    return {row["name"]: dict(row) for row in db.fetchall(f"PRAGMA table_info({table_name})")}


def _index_sql(db: LocalDatabase, index_name: str) -> str:
    row = db.fetchone("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (index_name,))
    assert row is not None
    return str(row["sql"])


def _migration_234():
    for migration in MIGRATIONS:
        if migration[0] == 234:
            return migration
    pytest.fail("migration version 234 is missing from MIGRATIONS")


def _db_before_234(tmp_path: Path) -> LocalDatabase:
    db = LocalDatabase(tmp_path / "before-234.db")
    _apply_baseline(db)
    pre_234 = [migration for migration in MIGRATIONS if BASELINE_VERSION < migration[0] < 234]
    _run_migration_list(db, BASELINE_VERSION, pre_234)
    return db


def _apply_234(db: LocalDatabase) -> None:
    current_version = get_current_version(db)
    assert current_version < 234
    _run_migration_list(db, current_version, [_migration_234()])


def _insert_minimal_task(db: LocalDatabase, task_id: str) -> None:
    db.execute(
        """
        INSERT INTO tasks (id, project_id, title, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (task_id, PERSONAL_PROJECT_ID, f"Task {task_id}", "2026-01-01", "2026-01-01"),
    )


def _yaml_payload() -> dict[str, Any]:
    assert STAGES_YAML.exists(), f"{STAGES_YAML} must be bundled with migration 234"
    payload = yaml.safe_load(STAGES_YAML.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_migration_234_registered() -> None:
    assert _migration_234()[0] == 234


def test_creates_registry_tables(tmp_path: Path) -> None:
    db = _db_before_234(tmp_path)

    _apply_234(db)

    tables = {
        row["name"]
        for row in db.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('task_stages_registry', 'task_type_default_stages', "
            "'task_stage_states')"
        )
    }
    assert tables == {
        "task_stages_registry",
        "task_type_default_stages",
        "task_stage_states",
    }

    registry_sql = str(
        db.fetchone(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_stages_registry'"
        )["sql"]
    )
    states_sql = str(
        db.fetchone(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_stage_states'"
        )["sql"]
    )

    assert (
        "categoryIN('discovery','design','verification','implementation','delivery')"
        in registry_sql.replace(" ", "")
    )
    assert "stateIN('ready','in_progress','done')" in states_sql.replace(" ", "")
    assert "REFERENCES task_stages_registry(name)" in states_sql

    indexes = {row["name"] for row in db.fetchall("PRAGMA index_list(task_stage_states)")}
    assert {
        "idx_task_stage_states_position",
        "idx_task_stage_states_state",
        "idx_task_stage_states_open",
    }.issubset(indexes)
    assert "WHERE state != 'done'" in _index_sql(db, "idx_task_stage_states_open")


def test_artifact_columns_added(tmp_path: Path) -> None:
    db = _db_before_234(tmp_path)
    _insert_minimal_task(db, "task-1")
    db.execute(
        """
        INSERT INTO task_artifacts (task_id, plan_file_path, expansion_attempts)
        VALUES (?, ?, ?)
        """,
        ("task-1", ".gobby/plans/task-1.md", 2),
    )

    _apply_234(db)

    columns = _column_info(db, "task_artifacts")
    assert ARTIFACT_REPORT_COLUMNS.issubset(columns)
    for column in ARTIFACT_REPORT_COLUMNS:
        assert columns[column]["type"].upper() == "TEXT"
        assert columns[column]["dflt_value"] is None

    row = db.fetchone(
        """
        SELECT plan_file_path, expansion_attempts, pr_review_report,
               structured_pr_verdict, merge_campaign_report
        FROM task_artifacts WHERE task_id = ?
        """,
        ("task-1",),
    )
    assert row is not None
    assert row["plan_file_path"] == ".gobby/plans/task-1.md"
    assert row["expansion_attempts"] == 2
    assert row["pr_review_report"] is None
    assert row["structured_pr_verdict"] is None
    assert row["merge_campaign_report"] is None


def test_fresh_install_matches(tmp_path: Path) -> None:
    db = LocalDatabase(tmp_path / "fresh.db")

    run_migrations(db)

    assert ARTIFACT_REPORT_COLUMNS.issubset(_column_info(db, "task_artifacts"))
    assert "is_escalated" in _column_info(db, "tasks")
    assert db.fetchone("SELECT COUNT(*) AS count FROM task_stages_registry")["count"] == len(
        CANONICAL_STAGE_NAMES
    )


def test_tasks_is_escalated_added(tmp_path: Path) -> None:
    db = _db_before_234(tmp_path)
    _insert_minimal_task(db, "task-1")

    _apply_234(db)

    columns = _column_info(db, "tasks")
    assert columns["is_escalated"]["type"].upper() == "INTEGER"
    assert columns["is_escalated"]["notnull"] == 1
    assert columns["is_escalated"]["dflt_value"] == "0"
    assert db.fetchone("SELECT is_escalated FROM tasks WHERE id = 'task-1'")["is_escalated"] == 0


def test_registry_seeded_inline(tmp_path: Path) -> None:
    db = _db_before_234(tmp_path)
    payload = _yaml_payload()
    expected_hash = hashlib.sha256(STAGES_YAML.read_bytes()).hexdigest()

    _apply_234(db)

    rows = db.fetchall(
        """
        SELECT name, display_label, description, category, default_agent,
               position_hint, requires_human, is_terminal, bundled_hash
        FROM task_stages_registry
        ORDER BY position_hint
        """
    )
    assert [row["name"] for row in rows] == CANONICAL_STAGE_NAMES
    assert len(payload["stages"]) == len(rows)
    assert {row["bundled_hash"] for row in rows} == {expected_hash}
    assert DISCOVERY_DEFAULT_AGENTS == {
        row["name"]: row["default_agent"] for row in rows if row["name"] in DISCOVERY_DEFAULT_AGENTS
    }


def test_bundled_stages_yaml_present_with_11_stages() -> None:
    payload = _yaml_payload()

    assert payload["version"] == 1
    stages = payload["stages"]
    assert [stage["name"] for stage in stages] == CANONICAL_STAGE_NAMES
    assert {stage["name"] for stage in stages}.isdisjoint(DROPPED_STAGE_NAMES)

    required = {"name", "display_label", "description", "category", "position_hint"}
    for stage in stages:
        assert required.issubset(stage)
        assert stage["category"] in {
            "discovery",
            "design",
            "verification",
            "implementation",
            "delivery",
        }
        assert isinstance(stage["position_hint"], int)

    assert {stage["name"]: stage.get("default_agent") for stage in stages}.items() >= (
        DISCOVERY_DEFAULT_AGENTS.items()
    )
    assert next(stage for stage in stages if stage["name"] == "merge")["is_terminal"] is True


def test_default_stages_seeded_inline(tmp_path: Path) -> None:
    db = _db_before_234(tmp_path)

    _apply_234(db)

    rows = db.fetchall(
        """
        SELECT task_type, stage_name, position
        FROM task_type_default_stages
        ORDER BY task_type, position
        """
    )
    by_task_type: dict[str, list[str]] = {}
    for row in rows:
        by_task_type.setdefault(row["task_type"], []).append(row["stage_name"])

    assert by_task_type == {
        "bug": ["development", "pr", "merge"],
        "chore": ["development", "pr", "merge"],
        "epic": [
            "ideation",
            "research",
            "architecture",
            "prd",
            "planning",
            "test_arch",
            "expansion",
            "development",
            "holistic_qa",
            "pr",
            "merge",
        ],
        "feature": ["planning", "test_arch", "expansion", "development", "pr", "merge"],
        "refactor": ["planning", "development", "pr", "merge"],
        "task": ["development", "pr", "merge"],
    }

    for task_type, stages in by_task_type.items():
        positions = [row["position"] for row in rows if row["task_type"] == task_type]
        assert positions == list(range(1, len(stages) + 1))


def test_fresh_db_fk_resolution_into_234(tmp_path: Path) -> None:
    db = LocalDatabase(tmp_path / "fresh-fk.db")

    run_migrations(db)

    assert get_current_version(db) >= 234
    assert db.fetchall("PRAGMA foreign_key_check") == []
    assert db.fetchone("SELECT COUNT(*) AS count FROM task_stages_registry")["count"] == len(
        CANONICAL_STAGE_NAMES
    )
