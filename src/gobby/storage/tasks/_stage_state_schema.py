"""Schema compatibility helpers for task stage-state storage."""

from __future__ import annotations

import re

from gobby.storage.database import DatabaseProtocol
from gobby.storage.tasks._stage_state_rows import StageStateRows
from gobby.storage.tasks._stage_utils import _now

_SQLITE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class StageStateSchema:
    def __init__(self, db: DatabaseProtocol, rows: StageStateRows) -> None:
        self.db = db
        self.rows = rows

    def ensure_phase2_columns(self) -> None:
        columns = self.columns("task_stage_states")
        table_sql = self.db.fetchone(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_stage_states'"
        )
        if "attempt_count" in columns or (
            table_sql is not None and "needs_review" not in str(table_sql["sql"])
        ):
            self.rebuild_stage_states_table()
            return
        additions = {
            "review_policy": (
                "ALTER TABLE task_stage_states ADD COLUMN review_policy TEXT "
                "NOT NULL DEFAULT 'none'"
            ),
            "reviewer_agent": "ALTER TABLE task_stage_states ADD COLUMN reviewer_agent TEXT",
            "work_attempt_count": (
                "ALTER TABLE task_stage_states ADD COLUMN work_attempt_count "
                "INTEGER NOT NULL DEFAULT 0"
            ),
            "review_round_count": (
                "ALTER TABLE task_stage_states ADD COLUMN review_round_count "
                "INTEGER NOT NULL DEFAULT 0"
            ),
            "max_work_attempts": (
                "ALTER TABLE task_stage_states ADD COLUMN max_work_attempts INTEGER"
            ),
            "max_review_rounds": (
                "ALTER TABLE task_stage_states ADD COLUMN max_review_rounds INTEGER"
            ),
        }
        with self.db.transaction() as conn:
            for column, sql in additions.items():
                if column not in columns:
                    conn.execute(sql)
            if "attempt_count" in columns and "work_attempt_count" not in columns:
                conn.execute(
                    """
                    UPDATE task_stage_states
                       SET work_attempt_count = COALESCE(attempt_count, 0)
                    """
                )

    def rebuild_stage_states_table(self) -> None:
        rows = [dict(row) for row in self.db.fetchall("SELECT * FROM task_stage_states")]
        with self.db.transaction() as conn:
            conn.execute("DROP INDEX IF EXISTS idx_task_stage_states_position")
            conn.execute("DROP INDEX IF EXISTS idx_task_stage_states_state")
            conn.execute("DROP INDEX IF EXISTS idx_task_stage_states_open")
            conn.execute("DROP TABLE IF EXISTS task_stage_states")
            conn.execute(
                """
                CREATE TABLE task_stage_states (
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    stage_name TEXT NOT NULL
                        REFERENCES task_stages_registry(name) ON DELETE RESTRICT,
                    position INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'ready'
                        CHECK (state IN (
                            'ready','in_progress','needs_review','review_approved','done'
                        )),
                    review_policy TEXT NOT NULL DEFAULT 'none'
                        CHECK (review_policy IN ('none','required','optional')),
                    reviewer_agent TEXT,
                    entered_at TEXT,
                    entered_by_session_id TEXT,
                    completed_at TEXT,
                    completed_by_session_id TEXT,
                    completed_commit_sha TEXT,
                    work_attempt_count INTEGER NOT NULL DEFAULT 0,
                    review_round_count INTEGER NOT NULL DEFAULT 0,
                    max_work_attempts INTEGER,
                    max_review_rounds INTEGER,
                    artifact_refs TEXT,
                    notes TEXT,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (task_id, stage_name)
                )
                """
            )
            for row in rows:
                registry = self.rows.registry_entry(row["stage_name"])
                conn.execute(
                    """
                    INSERT INTO task_stage_states (
                        task_id, stage_name, position, state, review_policy, reviewer_agent,
                        entered_at, entered_by_session_id, completed_at,
                        completed_by_session_id, completed_commit_sha, work_attempt_count,
                        review_round_count, max_work_attempts, max_review_rounds,
                        artifact_refs, notes, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["task_id"],
                        row["stage_name"],
                        row["position"],
                        row.get("state", "ready"),
                        row.get("review_policy") or registry.review_policy,
                        row.get("reviewer_agent") or registry.reviewer_agent,
                        row.get("entered_at"),
                        row.get("entered_by_session_id"),
                        row.get("completed_at"),
                        row.get("completed_by_session_id"),
                        row.get("completed_commit_sha"),
                        row.get("work_attempt_count", row.get("attempt_count", 0)) or 0,
                        row.get("review_round_count", 0) or 0,
                        row.get("max_work_attempts"),
                        row.get("max_review_rounds"),
                        row.get("artifact_refs"),
                        row.get("notes"),
                        row.get("updated_at") or _now(),
                    ),
                )
            conn.execute(
                """
                CREATE UNIQUE INDEX idx_task_stage_states_position
                    ON task_stage_states (task_id, position)
                """
            )
            conn.execute(
                """
                CREATE INDEX idx_task_stage_states_state
                    ON task_stage_states (stage_name, state)
                """
            )
            conn.execute(
                """
                CREATE INDEX idx_task_stage_states_open
                    ON task_stage_states (task_id, position) WHERE state != 'done'
                """
            )

    def columns(self, table_name: str) -> set[str]:
        if not _SQLITE_IDENTIFIER_RE.fullmatch(table_name):
            raise ValueError(f"Invalid table name: {table_name!r}")
        return {
            row["name"]
            for row in self.db.fetchall(f"PRAGMA table_info({table_name})")  # nosec B608
        }
