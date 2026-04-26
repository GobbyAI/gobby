"""Storage migration registry after flattening SQLite history."""

from collections.abc import Callable

from gobby.storage.database import LocalDatabase

MigrationAction = str | Callable[[LocalDatabase], None]


_RETIRED_PIPELINES = (
    "orchestrator",
    "front-half-orchestrator",
    "conductor",
    "dev-orchestrator",
    "delivery-orchestrator",
)
_RETIRED_AGENTS = ("conductor", "developer", "pipeline-worker")


def _disable_retired_workflow_definitions(db: LocalDatabase) -> None:
    """Disable installed tombstoned workflow definitions without deleting rows."""

    def disable(names: tuple[str, ...], workflow_type: str) -> None:
        placeholders = ",".join("?" for _ in names)
        db.execute(
            f"""
            UPDATE workflow_definitions
            SET enabled = 0, updated_at = datetime('now')
            WHERE project_id IS NULL
              AND source = 'installed'
              AND workflow_type = ?
              AND tags LIKE ?
              AND deleted_at IS NULL
              AND name IN ({placeholders})
            """,  # nosec B608 - placeholders are generated from static tuple length.
            (workflow_type, '%"gobby"%', *names),
        )

    disable(_RETIRED_PIPELINES, "pipeline")
    disable(_RETIRED_AGENTS, "agent")


def _task_columns(db: LocalDatabase) -> set[str]:
    return {row["name"] for row in db.fetchall("PRAGMA table_info(tasks)")}


def _add_task_column_if_missing(
    db: LocalDatabase,
    existing_columns: set[str],
    column_name: str,
    column_definition: str,
) -> None:
    if column_name in existing_columns:
        return
    db.execute(f"ALTER TABLE tasks ADD COLUMN {column_definition}")  # nosec B608
    existing_columns.add(column_name)


def _add_lifecycle_dispatch_schema(db: LocalDatabase) -> None:
    """Add task lifecycle dispatch columns and adjacent storage tables."""
    columns = _task_columns(db)
    _add_task_column_if_missing(
        db,
        columns,
        "lifecycle",
        """
        lifecycle TEXT NOT NULL DEFAULT 'open'
            CHECK(lifecycle IN (
                'open',
                'plan_review',
                'test_arch',
                'expanding',
                'in_development',
                'holistic_review',
                'pr',
                'merging',
                'merged'
            ))
        """,
    )
    _add_task_column_if_missing(
        db,
        columns,
        "allow_automation",
        "allow_automation INTEGER NOT NULL DEFAULT 0 CHECK(allow_automation IN (0, 1))",
    )
    _add_task_column_if_missing(
        db,
        columns,
        "yolo",
        "yolo INTEGER NOT NULL DEFAULT 0 CHECK(yolo IN (0, 1))",
    )
    _add_task_column_if_missing(
        db,
        columns,
        "isolation",
        "isolation TEXT NOT NULL DEFAULT 'worktree' CHECK(isolation IN ('none', 'worktree', 'clone'))",
    )
    _add_task_column_if_missing(db, columns, "assigned_agent", "assigned_agent TEXT")
    _add_task_column_if_missing(db, columns, "additional_skills", "additional_skills TEXT")

    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_tasks_dispatch_scan
            ON tasks (allow_automation, lifecycle, status)
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS task_dispatch_mutex (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            lease_until TEXT,
            lease_holder TEXT,
            run_id TEXT,
            action_kind TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dispatch_mutex_scan
            ON task_dispatch_mutex (lease_until, run_id)
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS task_artifacts (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            plan_file_path TEXT,
            worktree_path TEXT,
            worktree_id TEXT,
            clone_path TEXT,
            clone_id TEXT,
            target_branch TEXT,
            expansion_run_id TEXT,
            expansion_attempts INTEGER NOT NULL DEFAULT 0,
            pr_url TEXT,
            merge_commit_sha TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CHECK (
                (worktree_path IS NULL) = (worktree_id IS NULL)
                AND (clone_path IS NULL) = (clone_id IS NULL)
                AND (worktree_path IS NULL OR clone_path IS NULL)
            )
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS task_lifecycle_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            from_state TEXT,
            to_state TEXT NOT NULL,
            reason TEXT NOT NULL,
            by_actor TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_lifecycle_events_task
            ON task_lifecycle_events (task_id, created_at)
        """
    )


# Historical SQLite migrations through v219 are folded into baseline_schema.sql.
MIGRATIONS: list[tuple[int, str, MigrationAction]] = [
    (
        220,
        "Add terminal_reason to agent_runs",
        """
        ALTER TABLE agent_runs ADD COLUMN terminal_reason TEXT
        """,
    ),
    (
        221,
        "Disable retired workflow definitions",
        _disable_retired_workflow_definitions,
    ),
    (
        222,
        "Add lifecycle dispatch task schema",
        _add_lifecycle_dispatch_schema,
    ),
]
