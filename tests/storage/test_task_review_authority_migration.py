"""Open task reviews retain their authority when definitions become configurable."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import psycopg
import pytest

pytestmark = pytest.mark.unit

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "crates/gcore/assets/schema/migrations/484_task_review_authority.sql"
).read_text()


@pytest.mark.parametrize(
    ("name", "supplemental", "capabilities"),
    [
        ("security-reviewer", True, ["approve_security_review"]),
        ("orchestrator", False, ["approve_report_review"]),
        ("fixture-reviewer", False, []),
    ],
)
def test_migration_preserves_only_open_review_authority(
    name: str, supplemental: bool, capabilities: list[str]
) -> None:
    old = {"step": "review", "agent_definition": name, "feedback": "Accepted."}
    explicit = {**old, "supplemental_review": False, "agent_capabilities": ["custom-grant"]}
    unchanged: dict[int, dict[str, Any]] = {
        2: old,  # Closed task history is outside the migration's scope.
        3: explicit,  # New snapshots already carry their author's authority.
        4: {**old, "step": "implementation"},
        5: old,  # A normal comment is not a step record.
    }
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn, conn.transaction(force_rollback=True):
        conn.execute("CREATE TEMP TABLE tasks (id int PRIMARY KEY, closed_at timestamptz)")
        conn.execute(
            "CREATE TEMP TABLE task_comments "
            "(id int PRIMARY KEY, task_id int, author_type text, body text)"
        )
        conn.execute("INSERT INTO tasks VALUES (1, NULL), (2, now())")
        for key, value in {1: old, **unchanged}.items():
            conn.execute(
                "INSERT INTO task_comments VALUES (%s, %s, %s, %s)",
                (
                    key,
                    2 if key == 2 else 1,
                    "comment" if key == 5 else "task_step",
                    json.dumps(value),
                ),
            )
        conn.execute(MIGRATION)
        migrated: dict[int, dict[str, Any]] = dict(
            conn.execute("SELECT id, body::jsonb FROM task_comments").fetchall()
        )
        assert migrated == {
            1: {**old, "supplemental_review": supplemental, "agent_capabilities": capabilities},
            **unchanged,
        }
        conn.execute(MIGRATION)
        replayed: dict[int, dict[str, Any]] = dict(
            conn.execute("SELECT id, body::jsonb FROM task_comments").fetchall()
        )
        assert replayed == migrated
