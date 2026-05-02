"""Database migrations for local storage.

For new databases (version == 0):
    BASELINE_SCHEMA is applied, jumping directly to BASELINE_VERSION.

For existing databases at the launch baseline:
    Any migrations in MIGRATIONS beyond BASELINE_VERSION are applied incrementally.

Older pre-launch SQLite databases are intentionally unsupported. Newer
versions are left untouched so a newer build's schema is never downgraded.

To add a new migration:
    1. Add helper callables to gobby.storage.migration_helpers when needed.
    2. Add the migration to gobby.storage._migration_registry.MIGRATIONS.
    3. Also add the migration to BASELINE_SCHEMA for future fresh installs.
"""

import json
import logging
from collections import Counter
from collections.abc import Callable
from pathlib import Path

__path__ = [str(Path(__file__).with_suffix(""))]

from gobby.storage._migration_registry import MIGRATIONS as _REGISTRY_MIGRATIONS
from gobby.storage.database import LocalDatabase
from gobby.storage.migration_helpers import (
    _setup_code_content_fts,
    _setup_code_symbols_fts,
    _setup_memories_fts,
    _setup_skills_fts,
    _setup_tasks_fts,
)
from gobby.storage.migrations.add_cron_is_system import up as add_cron_is_system
from gobby.storage.migrations.add_last_reviewed_plan_hash import up as add_last_reviewed_plan_hash
from gobby.storage.migrations.clear_session_summary_sentinels import (
    up as clear_session_summary_sentinels,
)

logger = logging.getLogger(__name__)

__all__ = [
    "BASELINE_VERSION",
    "BASELINE_SCHEMA",
    "MIGRATIONS",
    "MigrationAction",
    "MigrationUnsupportedError",
    "_run_migration_list",
    "_setup_code_content_fts",
    "_setup_code_symbols_fts",
    "_setup_memories_fts",
    "_setup_skills_fts",
    "_setup_tasks_fts",
    "get_current_version",
    "latest_known_version",
    "migrations_needed",
    "run_migrations",
]


class MigrationUnsupportedError(Exception):
    """Raised when database version is too old to migrate."""

    pass


MigrationAction = str | Callable[[LocalDatabase], None]

BASELINE_VERSION = 220
_MIN_MIGRATION_VERSION = 219
BASELINE_SCHEMA = (Path(__file__).parent / "baseline_schema.sql").read_text()
_STAGES_REGISTRY_PATH = Path(__file__).parent.parent / "install/shared/registry/stages.yaml"
_ARTIFACT_REPORT_COLUMNS = (
    "pr_review_report",
    "structured_pr_verdict",
    "merge_campaign_report",
)
_MIGRATION_234_SESSION_ID = "migration:234"
_PERSONAL_PROJECT_ID = "00000000-0000-0000-0000-000000060887"
_REQUIRED_REVIEW_STAGES = {
    "planning": "plan-adversary",
    "expansion": "expansion-qa",
    "development": "qa-reviewer",
    "holistic_qa": "holistic-reviewer",
    "pr": None,
}
_CONDUCTOR_STAGE_OVERRIDES = {
    "requirements": "prd",
    "planning": "planning",
    "expansion": "expansion",
    "test-architecture": "test_arch",
}
_LIFECYCLE_STAGE_TARGETS = {
    "plan_review": "planning",
    "test_arch": "test_arch",
    "expanding": "expansion",
    "in_development": "development",
    "holistic_review": "holistic_qa",
    "pr": "pr",
    "merging": "merge",
}
_STAGE_ORDER = {
    "ideation": 10,
    "research": 20,
    "architecture": 30,
    "prd": 40,
    "planning": 50,
    "adversarial_review": 60,
    "test_arch": 70,
    "expansion": 80,
    "expansion_qa": 90,
    "development": 100,
    "code_review_qa": 110,
    "holistic_qa": 120,
    "pr": 130,
    "merge": 140,
}
_NORMALIZED_STATUSES = {"open", "in_progress", "needs_review", "review_approved", "closed"}
_DEFAULT_STAGE_MANIFESTS = {
    "epic": (
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
    ),
    "feature": (
        "planning",
        "test_arch",
        "expansion",
        "development",
        "pr",
        "merge",
    ),
    "bug": ("development", "pr", "merge"),
    "refactor": ("planning", "development", "pr", "merge"),
    "chore": ("development", "pr", "merge"),
    "task": ("development", "pr", "merge"),
}
NEW_TASK_TYPE_DEFAULTS = {
    "simple_fix": ["development", "pr", "merge"],
    "research_spike": ["ideation", "research", "prd"],
    "architecture_doc": ["research", "architecture"],
    "prd_doc": ["ideation", "prd"],
}


def _table_columns(db: LocalDatabase, table_name: str) -> set[str]:
    # PRAGMA does not accept SQL parameter binding; table_name is internally controlled.
    return {row["name"] for row in db.fetchall(f"PRAGMA table_info({table_name})")}


def _task_artifacts_create_sql(table_name: str) -> str:
    return f"""
        CREATE TABLE {table_name} (
            task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
            plan_file_path TEXT,
            plan_file_hash TEXT,
            worktree_path TEXT,
            worktree_id TEXT,
            clone_path TEXT,
            clone_id TEXT,
            base_commit_sha TEXT,
            target_branch TEXT,
            expansion_run_id TEXT,
            expansion_attempts INTEGER NOT NULL DEFAULT 0,
            max_expansion_attempts INTEGER,
            max_qa_rounds INTEGER,
            max_merge_attempts INTEGER,
            max_holistic_rounds INTEGER,
            max_review_rounds INTEGER,
            pr_url TEXT,
            merge_commit_sha TEXT,
            pr_review_report TEXT,
            structured_pr_verdict TEXT,
            merge_campaign_report TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            CHECK (
                (worktree_path IS NULL) = (worktree_id IS NULL)
                AND (clone_path IS NULL) = (clone_id IS NULL)
                AND (worktree_path IS NULL OR clone_path IS NULL)
                AND (
                    base_commit_sha IS NULL
                    OR worktree_path IS NOT NULL
                    OR clone_path IS NOT NULL
                )
            )
        )
        """


def _add_task_artifact_evidence_columns(db: LocalDatabase) -> None:
    row = db.fetchone("SELECT sql FROM sqlite_master WHERE type='table' AND name='task_artifacts'")
    if row is None:
        db.execute(_task_artifacts_create_sql("task_artifacts"))
        return

    existing_columns = _table_columns(db, "task_artifacts")
    table_sql = str(row["sql"] or "")
    required_columns = {"base_commit_sha", "plan_file_hash", *_ARTIFACT_REPORT_COLUMNS}
    if required_columns.issubset(existing_columns) and "base_commit_sha IS NULL" in table_sql:
        return

    columns = [
        "task_id",
        "plan_file_path",
        "plan_file_hash",
        "worktree_path",
        "worktree_id",
        "clone_path",
        "clone_id",
        "base_commit_sha",
        "target_branch",
        "expansion_run_id",
        "expansion_attempts",
        "max_expansion_attempts",
        "max_qa_rounds",
        "max_merge_attempts",
        "max_holistic_rounds",
        "max_review_rounds",
        "pr_url",
        "merge_commit_sha",
        "pr_review_report",
        "structured_pr_verdict",
        "merge_campaign_report",
        "updated_at",
    ]
    select_columns = [
        column if column in existing_columns else _default_task_artifact_column(column)
        for column in columns
    ]

    db.execute("ALTER TABLE task_artifacts RENAME TO task_artifacts_old")
    db.execute(_task_artifacts_create_sql("task_artifacts"))
    db.execute(
        f"""
        INSERT INTO task_artifacts ({", ".join(columns)})
        SELECT {", ".join(select_columns)}
        FROM task_artifacts_old
        """,  # nosec B608 - columns are fixed allowlist values.
    )
    db.execute("DROP TABLE task_artifacts_old")


def _add_task_artifact_retry_cap_columns(db: LocalDatabase) -> None:
    existing_columns = _table_columns(db, "task_artifacts")
    for column in (
        "max_expansion_attempts",
        "max_qa_rounds",
        "max_merge_attempts",
        "max_holistic_rounds",
        "max_review_rounds",
    ):
        if column not in existing_columns:
            db.execute(  # nosec B608 - column is from the fixed allowlist above.
                f"ALTER TABLE task_artifacts ADD COLUMN {column} INTEGER"
            )


def _default_task_artifact_column(column: str) -> str:
    if column == "expansion_attempts":
        return "0 AS expansion_attempts"
    if column == "updated_at":
        return "datetime('now') AS updated_at"
    return f"NULL AS {column}"


def _decode_task_labels(labels_json: str | None) -> list[str]:
    if not labels_json:
        return []
    try:
        labels = json.loads(labels_json)
    except json.JSONDecodeError:
        return []
    if not isinstance(labels, list):
        return []
    return [label for label in labels if isinstance(label, str)]


def _numeric_label_value(labels: list[str], prefix: str) -> int:
    values: list[int] = []
    for label in labels:
        if not label.startswith(prefix):
            continue
        suffix = label.removeprefix(prefix).strip()
        if suffix.isdigit():
            values.append(int(suffix))
    return max(values, default=0)


def _normalize_status_escalated(status: str) -> tuple[str, bool]:
    """Normalize legacy escalation status before lifecycle/status mapping."""
    if status == "escalated":
        return "in_progress", True
    return status, False


def _normalize_status_closed_non_merged(lifecycle: str, status: str) -> tuple[str, bool]:
    """Normalize legacy non-merged closures before lifecycle/status mapping."""
    if status == "closed" and lifecycle != "merged":
        return "in_progress", True
    return status, False


def _conductor_override_stage(labels: list[str], task_id: str) -> str | None:
    overrides: list[str] = []
    for label in labels:
        if not label.startswith("conductor-stage:"):
            continue
        conductor_stage = label.removeprefix("conductor-stage:").strip()
        stage_name = _CONDUCTOR_STAGE_OVERRIDES.get(conductor_stage)
        if stage_name is None:
            raise RuntimeError(
                f"Migration 234 cannot map conductor-stage label {label!r} for task {task_id}"
            )
        overrides.append(stage_name)
    unique = set(overrides)
    if len(unique) > 1:
        raise RuntimeError(
            f"Migration 234 found multiple conductor-stage labels for task {task_id}: "
            f"{sorted(unique)}"
        )
    return overrides[0] if overrides else None


def _stage_skip_labels(labels: list[str]) -> set[str]:
    skipped: set[str] = set()
    for label in labels:
        if not label.startswith("stage-:"):
            continue
        stage_name = label.removeprefix("stage-:").strip()
        if stage_name:
            skipped.add(stage_name)
    return skipped


def _filtered_labels_after_backfill(labels: list[str], *, conductor_override: bool) -> list[str]:
    filtered: list[str] = []
    for label in labels:
        if label.startswith("stage-:"):
            continue
        if conductor_override and label.startswith("conductor-stage:"):
            continue
        filtered.append(label)
    return filtered


def _drop_legacy_review_round_labels(db: LocalDatabase) -> None:
    rows = db.fetchall("SELECT id, labels FROM tasks WHERE labels IS NOT NULL")
    for row in rows:
        labels = _decode_task_labels(row["labels"])
        filtered = [
            label for label in labels if not label.startswith(("planning-round:", "qa-attempts:"))
        ]
        if filtered == labels:
            continue
        db.execute(
            "UPDATE tasks SET labels = ? WHERE id = ?",
            (json.dumps(filtered), row["id"]),
        )


def _load_default_manifest(
    db: LocalDatabase,
    task_type: str,
    skipped_stages: set[str],
) -> list[str]:
    rows = db.fetchall(
        """
        SELECT stage_name
          FROM task_type_default_stages
         WHERE task_type = ?
         ORDER BY position, stage_name
        """,
        (task_type,),
    )
    if not rows and task_type != "task":
        rows = db.fetchall(
            """
            SELECT stage_name
              FROM task_type_default_stages
             WHERE task_type = 'task'
             ORDER BY position, stage_name
            """
        )
    return [row["stage_name"] for row in rows if row["stage_name"] not in skipped_stages]


def _is_mapped_tuple(lifecycle: str, status: str) -> bool:
    if status not in _NORMALIZED_STATUSES:
        return False
    if lifecycle == "open":
        return status != "closed"
    if lifecycle == "merged":
        return True
    if lifecycle == "merging":
        return status != "closed"
    if lifecycle in _LIFECYCLE_STAGE_TARGETS:
        return status in {"open", "in_progress", "needs_review", "review_approved"}
    return False


def _target_stage_rank(stage_name: str | None) -> int | None:
    if stage_name is None:
        return None
    return _STAGE_ORDER.get(stage_name)


def _state_through_target(
    manifest: list[str],
    target_stage: str | None,
    target_state: str,
) -> list[str]:
    target_rank = _target_stage_rank(target_stage)
    if target_rank is None:
        return ["ready" for _stage in manifest]

    states: list[str] = []
    for stage_name in manifest:
        stage_rank = _STAGE_ORDER.get(stage_name)
        if stage_name == target_stage:
            states.append(target_state)
        elif stage_rank is not None and stage_rank < target_rank:
            states.append("done")
        else:
            states.append("ready")
    return states


def _state_done_through_lifecycle(manifest: list[str], lifecycle: str) -> list[str]:
    target_stage = _LIFECYCLE_STAGE_TARGETS.get(lifecycle)
    return _state_through_target(manifest, target_stage, "done")


def _resolve_state_from_lifecycle_status(
    lifecycle: str,
    status: str,
    manifest: list[str],
    *,
    closed_non_merged: bool,
) -> list[str]:
    if closed_non_merged:
        return _state_done_through_lifecycle(manifest, lifecycle)
    if lifecycle == "merged":
        return ["done" for _stage in manifest]
    if lifecycle == "open":
        return ["ready" for _stage in manifest]
    if lifecycle == "merging":
        return _state_through_target(manifest, "merge", "in_progress")

    target_stage = _LIFECYCLE_STAGE_TARGETS.get(lifecycle)
    if lifecycle in {"plan_review", "expanding", "in_development", "holistic_review", "pr"}:
        target_state = {
            "open": "in_progress",
            "in_progress": "in_progress",
            "needs_review": "needs_review",
            "review_approved": "review_approved",
        }[status]
        return _state_through_target(manifest, target_stage, target_state)
    if lifecycle == "test_arch":
        target_state = {
            "open": "in_progress",
            "in_progress": "in_progress",
            "needs_review": "in_progress",
            "review_approved": "done",
        }[status]
        return _state_through_target(manifest, target_stage, target_state)

    raise RuntimeError(f"Migration 234 cannot map lifecycle/status tuple {(lifecycle, status)!r}")


def _resolve_conductor_stage_state(
    stage_name: str,
    status: str,
    *,
    closed_non_merged: bool,
) -> str:
    if closed_non_merged or status == "closed":
        return "done"
    if status in {"open", "in_progress"}:
        return "in_progress"
    if status == "needs_review":
        return "needs_review" if stage_name in _REQUIRED_REVIEW_STAGES else "in_progress"
    if status == "review_approved":
        return "review_approved" if stage_name in _REQUIRED_REVIEW_STAGES else "done"
    raise RuntimeError(
        f"Migration 234 cannot map conductor-stage status {status!r} for stage {stage_name!r}"
    )


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int | str):
        return int(value)
    raise TypeError(f"Expected int-compatible migration value, got {type(value).__name__}")


def _stage_caps_for_row(row: dict[str, object], stage_name: str) -> tuple[int | None, int | None]:
    max_work_attempts = None
    max_review_rounds = None
    if stage_name == "expansion":
        max_work_attempts = row.get("max_expansion_attempts")
    elif stage_name == "merge":
        max_work_attempts = row.get("max_merge_attempts")

    if stage_name == "development":
        max_review_rounds = row.get("max_qa_rounds")
    elif stage_name == "holistic_qa":
        max_review_rounds = row.get("max_holistic_rounds")
    elif stage_name == "pr":
        max_review_rounds = row.get("max_review_rounds")

    return (
        _optional_int(max_work_attempts),
        _optional_int(max_review_rounds),
    )


def _stage_timing_fields(
    row: dict[str, object], state: str
) -> tuple[object, object, object, object]:
    actor = row.get("claimed_by_session_id") or row.get("closed_in_session_id")
    if state == "ready":
        return None, None, None, None
    if state == "done":
        return row.get("created_at"), actor, row.get("closed_at") or row.get("updated_at"), actor
    return row.get("updated_at") or row.get("created_at"), actor, None, None


def _registry_metadata(db: LocalDatabase) -> dict[str, dict[str, object]]:
    rows = db.fetchall(
        """
        SELECT name, review_policy, reviewer_agent
          FROM task_stages_registry
        """
    )
    return {row["name"]: dict(row) for row in rows}


def _ensure_migration_234_session(db: LocalDatabase) -> None:
    row = db.fetchone("SELECT id FROM sessions WHERE id = ?", (_MIGRATION_234_SESSION_ID,))
    if row is not None:
        return
    project = db.fetchone(
        "SELECT id FROM projects WHERE id = ?",
        (_PERSONAL_PROJECT_ID,),
    )
    project_id = _PERSONAL_PROJECT_ID
    if project is None:
        fallback = db.fetchone("SELECT id FROM projects ORDER BY created_at LIMIT 1")
        if fallback is None:
            raise RuntimeError("Migration 234 cannot create synthetic close session: no project")
        project_id = fallback["id"]
    db.execute(
        """
        INSERT OR IGNORE INTO sessions (
            id, external_id, machine_id, source, project_id, title,
            status, agent_depth, created_at, updated_at
        )
        VALUES (?, ?, 'migration', 'migration', ?, 'Migration 234',
                'active', 0, datetime('now'), datetime('now'))
        """,
        (_MIGRATION_234_SESSION_ID, _MIGRATION_234_SESSION_ID, project_id),
    )


def _close_all_done_stage_manifests(db: LocalDatabase) -> None:
    if (
        db.fetchone(
            """
            SELECT 1
              FROM tasks
             WHERE closed_at IS NULL
               AND EXISTS (
                   SELECT 1 FROM task_stage_states tss WHERE tss.task_id = tasks.id
               )
               AND NOT EXISTS (
                   SELECT 1
                     FROM task_stage_states tss
                    WHERE tss.task_id = tasks.id AND tss.state != 'done'
               )
             LIMIT 1
            """
        )
        is None
    ):
        return
    _ensure_migration_234_session(db)
    db.execute(
        """
        UPDATE tasks
           SET closed_at = datetime('now'),
               closed_in_session_id = ?,
               updated_at = datetime('now')
         WHERE closed_at IS NULL
           AND EXISTS (
               SELECT 1 FROM task_stage_states tss WHERE tss.task_id = tasks.id
           )
           AND NOT EXISTS (
               SELECT 1
                 FROM task_stage_states tss
                WHERE tss.task_id = tasks.id AND tss.state != 'done'
           )
        """,
        (_MIGRATION_234_SESSION_ID,),
    )


def _backfill_task_stage_states(db: LocalDatabase) -> None:
    """Backfill task stage-state manifests from legacy lifecycle/status fields."""

    registry = _registry_metadata(db)
    census: Counter[tuple[str, str]] = Counter()
    tasks = db.fetchall(
        """
        SELECT tasks.id, tasks.task_type, tasks.lifecycle, tasks.status, tasks.labels,
               tasks.created_at, tasks.updated_at, tasks.claimed_by_session_id,
               tasks.closed_in_session_id, tasks.closed_at, tasks.closed_commit_sha,
               tasks.escalated_at, task_artifacts.pr_url,
               task_artifacts.max_expansion_attempts, task_artifacts.max_qa_rounds,
               task_artifacts.max_merge_attempts, task_artifacts.max_holistic_rounds,
               task_artifacts.max_review_rounds
          FROM tasks
          LEFT JOIN task_artifacts ON task_artifacts.task_id = tasks.id
         ORDER BY tasks.created_at, tasks.id
        """
    )

    for task_row in tasks:
        row = dict(task_row)
        task_id = str(row["id"])
        labels = _decode_task_labels(row.get("labels"))
        conductor_stage = _conductor_override_stage(labels, task_id)
        skipped_stages = _stage_skip_labels(labels)
        manifest = (
            [conductor_stage]
            if conductor_stage is not None
            else _load_default_manifest(db, str(row.get("task_type") or "task"), skipped_stages)
        )

        lifecycle = str(row.get("lifecycle") or "open")
        raw_status = str(row.get("status") or "open")
        normalized_status, was_escalated_status = _normalize_status_escalated(raw_status)
        if was_escalated_status:
            db.execute("UPDATE tasks SET is_escalated = 1 WHERE id = ?", (task_id,))
        normalized_status, closed_non_merged = _normalize_status_closed_non_merged(
            lifecycle, normalized_status
        )

        if conductor_stage is not None:
            if normalized_status not in _NORMALIZED_STATUSES:
                raise RuntimeError(
                    "Migration 234 cannot map task lifecycle/status tuple "
                    f"{(lifecycle, normalized_status)!r} for task {task_id}"
                )
            stage_states = [
                _resolve_conductor_stage_state(
                    conductor_stage,
                    normalized_status,
                    closed_non_merged=closed_non_merged,
                )
            ]
            census[(f"conductor:{conductor_stage}", normalized_status)] += 1
        else:
            if not _is_mapped_tuple(lifecycle, normalized_status):
                raise RuntimeError(
                    "Migration 234 cannot map task lifecycle/status tuple "
                    f"{(lifecycle, normalized_status)!r} for task {task_id}"
                )
            stage_states = _resolve_state_from_lifecycle_status(
                lifecycle,
                normalized_status,
                manifest,
                closed_non_merged=closed_non_merged,
            )
            census[(lifecycle, normalized_status)] += 1

        db.execute("DELETE FROM task_stage_states WHERE task_id = ?", (task_id,))
        planning_rounds = _numeric_label_value(labels, "planning-round:")
        qa_rounds = _numeric_label_value(labels, "qa-attempts:")
        for position, (stage_name, state) in enumerate(
            zip(manifest, stage_states, strict=True),
            start=1,
        ):
            metadata = registry.get(stage_name)
            if metadata is None:
                raise RuntimeError(
                    f"Migration 234 cannot backfill task {task_id}: unknown stage {stage_name!r}"
                )
            entered_at, entered_by, completed_at, completed_by = _stage_timing_fields(row, state)
            max_work_attempts, max_review_rounds = _stage_caps_for_row(row, stage_name)
            artifact_refs = (
                json.dumps({"pr_url": row["pr_url"]}, sort_keys=True)
                if stage_name == "pr" and row.get("pr_url")
                else None
            )
            db.execute(
                """
                INSERT INTO task_stage_states (
                    task_id, stage_name, position, state, review_policy, reviewer_agent,
                    entered_at, entered_by_session_id, completed_at, completed_by_session_id,
                    completed_commit_sha, work_attempt_count, review_round_count,
                    max_work_attempts, max_review_rounds, artifact_refs, notes, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    task_id,
                    stage_name,
                    position,
                    state,
                    metadata.get("review_policy") or "none",
                    metadata.get("reviewer_agent"),
                    entered_at,
                    entered_by,
                    completed_at,
                    completed_by,
                    row.get("closed_commit_sha") if state == "done" else None,
                    planning_rounds
                    if stage_name == "planning"
                    else qa_rounds
                    if stage_name == "development"
                    else 0,
                    max_work_attempts,
                    max_review_rounds,
                    artifact_refs,
                    row.get("updated_at"),
                ),
            )

        filtered_labels = _filtered_labels_after_backfill(
            labels,
            conductor_override=conductor_stage is not None,
        )
        if filtered_labels != labels:
            db.execute(
                "UPDATE tasks SET labels = ?, updated_at = updated_at WHERE id = ?",
                (json.dumps(filtered_labels) if filtered_labels else None, task_id),
            )

    logger.info("Migration 234 task-stage backfill census: %s", sorted(census.items()))
    db.execute("UPDATE tasks SET is_escalated = 1 WHERE escalated_at IS NOT NULL")
    _close_all_done_stage_manifests(db)


def _backfill_task_stage_states_from_legacy(db: LocalDatabase) -> None:
    _backfill_task_stage_states(db)


def _add_task_stage_registry_schema(db: LocalDatabase) -> None:
    with db.transaction():
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS task_stages_registry (
                name TEXT PRIMARY KEY,
                display_label TEXT NOT NULL,
                description TEXT NOT NULL,
                category TEXT NOT NULL
                    CHECK (category IN ('discovery','design','verification','implementation','delivery')),
                default_agent TEXT,
                reviewer_agent TEXT,
                review_policy TEXT NOT NULL DEFAULT 'none'
                    CHECK (review_policy IN ('none','required','optional')),
                position_hint INTEGER NOT NULL,
                requires_human INTEGER NOT NULL DEFAULT 0,
                is_terminal INTEGER NOT NULL DEFAULT 0,
                default_max_work_attempts INTEGER NOT NULL DEFAULT 3,
                default_max_review_rounds INTEGER NOT NULL DEFAULT 5,
                bundled_hash TEXT,
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS task_type_default_stages (
                task_type TEXT NOT NULL,
                stage_name TEXT NOT NULL
                    REFERENCES task_stages_registry(name) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                PRIMARY KEY (task_type, stage_name)
            )
            """
        )
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_task_type_default_stages_position
                ON task_type_default_stages (task_type, position)
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS task_stage_states (
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                stage_name TEXT NOT NULL
                    REFERENCES task_stages_registry(name) ON DELETE RESTRICT,
                position INTEGER NOT NULL,
                state TEXT NOT NULL DEFAULT 'ready'
                    CHECK (
                        state IN ('ready','in_progress','done')
                        OR state IN ('needs_review','review_approved')
                    ),
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
        db.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_task_stage_states_position
                ON task_stage_states (task_id, position)
            """
        )
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_task_stage_states_state
                ON task_stage_states (stage_name, state)
            """
        )
        db.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_task_stage_states_open
                ON task_stage_states (task_id, position) WHERE state != 'done'
            """
        )

        _add_task_artifact_evidence_columns(db)
        if "is_escalated" not in _table_columns(db, "tasks"):
            db.execute(
                """
                ALTER TABLE tasks
                ADD COLUMN is_escalated INTEGER NOT NULL DEFAULT 0
                    CHECK(is_escalated IN (0, 1))
                """
            )

        from gobby.storage.tasks._stage_registry_loader import StageRegistryLoader

        stages, bundled_hash = StageRegistryLoader(path=_STAGES_REGISTRY_PATH).load_with_hash()
        for stage in stages:
            db.execute(
                """
                INSERT INTO task_stages_registry (
                    name, display_label, description, category, default_agent,
                    position_hint, requires_human, is_terminal, bundled_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(name) DO UPDATE SET
                    display_label = excluded.display_label,
                    description = excluded.description,
                    category = excluded.category,
                    default_agent = excluded.default_agent,
                    position_hint = excluded.position_hint,
                    requires_human = excluded.requires_human,
                    is_terminal = excluded.is_terminal,
                    bundled_hash = excluded.bundled_hash,
                    updated_at = datetime('now')
                """,
                (
                    stage.name,
                    stage.display_label,
                    stage.description,
                    stage.category,
                    stage.default_agent,
                    stage.position_hint,
                    1 if stage.requires_human else 0,
                    1 if stage.is_terminal else 0,
                    bundled_hash,
                ),
            )
        for stage_name, reviewer_agent in _REQUIRED_REVIEW_STAGES.items():
            db.execute(
                """
                UPDATE task_stages_registry
                   SET review_policy = 'required',
                       reviewer_agent = ?,
                       updated_at = datetime('now')
                 WHERE name = ?
                """,
                (reviewer_agent, stage_name),
            )

        for task_type, stages_for_type in _DEFAULT_STAGE_MANIFESTS.items():
            db.execute("DELETE FROM task_type_default_stages WHERE task_type = ?", (task_type,))
            for position, stage_name in enumerate(stages_for_type, start=1):
                db.execute(
                    """
                    INSERT INTO task_type_default_stages (task_type, stage_name, position)
                    VALUES (?, ?, ?)
                    """,
                    (task_type, stage_name, position),
                )

        _backfill_task_stage_states(db)


def _seed_new_task_type_defaults(db: LocalDatabase) -> None:
    for task_type, stages_for_type in NEW_TASK_TYPE_DEFAULTS.items():
        db.execute("DELETE FROM task_type_default_stages WHERE task_type = ?", (task_type,))
        for position, stage_name in enumerate(stages_for_type, start=1):
            db.execute(
                """
                INSERT INTO task_type_default_stages (task_type, stage_name, position)
                VALUES (?, ?, ?)
                """,
                (task_type, stage_name, position),
            )


def _drop_legacy_task_state_columns(db: LocalDatabase) -> None:
    task_columns = {"lifecycle", "lifecycle_stage", "status"}
    artifact_columns = {
        "max_expansion_attempts",
        "max_qa_rounds",
        "max_merge_attempts",
        "max_holistic_rounds",
        "max_review_rounds",
    }

    with db.transaction() as conn:
        conn.execute("DROP INDEX IF EXISTS idx_tasks_status")
        conn.execute("DROP INDEX IF EXISTS idx_tasks_lifecycle_stage")
        conn.execute("DROP INDEX IF EXISTS idx_tasks_dispatch_scan")

        existing_task_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        for column in sorted(task_columns & existing_task_columns):
            conn.execute(f"ALTER TABLE tasks DROP COLUMN {column}")  # nosec B608

        existing_artifact_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(task_artifacts)").fetchall()
        }
        for column in sorted(artifact_columns & existing_artifact_columns):
            conn.execute(f"ALTER TABLE task_artifacts DROP COLUMN {column}")  # nosec B608

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tasks_dispatch_scan
                ON tasks(allow_automation, closed_at, is_escalated)
            """
        )


MIGRATIONS: list[tuple[int, str, MigrationAction]] = [
    *_REGISTRY_MIGRATIONS,
    (
        224,
        "Add evidence metadata to task_artifacts",
        _add_task_artifact_evidence_columns,
    ),
    (
        225,
        "Index pipeline_executions(created_at DESC) for paginated listing",
        """
        CREATE INDEX IF NOT EXISTS idx_pipeline_executions_created_at
            ON pipeline_executions (created_at DESC)
        """,
    ),
    (
        226,
        "Add DB-backed plan registry",
        """
        CREATE TABLE IF NOT EXISTS plans (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            plan_id TEXT NOT NULL,
            plan_path TEXT NOT NULL,
            plan_hash TEXT,
            plan_kind TEXT NOT NULL CHECK(plan_kind IN ('implementation', 'strategy')),
            state TEXT NOT NULL CHECK(state IN ('active', 'archived')),
            root_task_ref TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            archived_at TEXT,
            UNIQUE (project_id, plan_id)
        );
        CREATE INDEX IF NOT EXISTS idx_plans_root_task ON plans(root_task_ref);
        CREATE INDEX IF NOT EXISTS idx_plans_state ON plans(state);
        """,
    ),
    (
        227,
        "Add task artifact retry cap overrides",
        _add_task_artifact_retry_cap_columns,
    ),
    (
        228,
        "Index plans by project and state",
        """
        CREATE INDEX IF NOT EXISTS idx_plans_project_state
            ON plans(project_id, state)
        """,
    ),
    (
        229,
        "Add last reviewed plan hash artifact fields",
        add_last_reviewed_plan_hash,
    ),
    (
        231,
        "Add system-managed marker to cron jobs",
        add_cron_is_system,
    ),
    (
        233,
        "Clear failed provider output from session summaries",
        clear_session_summary_sentinels,
    ),
    (
        234,
        "Add task stage registry, manifests, and lifecycle report fields",
        _add_task_stage_registry_schema,
    ),
    (
        235,
        "Seed Phase 5 task type default stage manifests",
        _seed_new_task_type_defaults,
    ),
    (
        236,
        "Drop legacy task lifecycle and artifact cap columns",
        _drop_legacy_task_state_columns,
    ),
    (
        237,
        "Drop legacy review-round labels",
        _drop_legacy_review_round_labels,
    ),
]


def get_current_version(db: LocalDatabase) -> int:
    """Get current schema version from database."""
    try:
        row = db.fetchone("SELECT MAX(version) as version FROM schema_version")
        return row["version"] if row and row["version"] else 0
    except Exception:
        return 0


def latest_known_version() -> int:
    """Return the newest schema version known to this build."""
    return max(
        BASELINE_VERSION,
        max((version for version, _description, _action in MIGRATIONS), default=BASELINE_VERSION),
    )


def migrations_needed(db: LocalDatabase) -> bool:
    """Return whether schema migrations should run for this database.

    This is intentionally a schema-version check only. Startup repair work that
    lives in run_migrations should still be executed by normal daemon startup.
    """
    current_version = get_current_version(db)
    if current_version == 0 or current_version < _MIN_MIGRATION_VERSION:
        return True
    return current_version < latest_known_version()


def _apply_baseline(db: LocalDatabase) -> None:
    """Apply baseline schema for new databases (flattened at v220)."""
    logger.info("Applying baseline schema (v220)")

    with db.transaction() as conn:
        # Execute baseline schema
        for statement in BASELINE_SCHEMA.strip().split(";"):
            statement = statement.strip()
            if statement:
                conn.execute(statement)

        # Record baseline version
        conn.execute(
            "INSERT INTO schema_version (version) VALUES (?)",
            (BASELINE_VERSION,),
        )

    # FTS5 triggers use semicolons in BEGIN...END — can't go through the split
    _setup_code_symbols_fts(db, include_summary=True)
    _setup_code_content_fts(db)
    _setup_tasks_fts(db)
    _setup_skills_fts(db)
    _setup_memories_fts(db)

    logger.info(f"Baseline schema applied, now at version {BASELINE_VERSION}")


def _run_migration_list(
    db: LocalDatabase,
    current_version: int,
    migrations: list[tuple[int, str, MigrationAction]],
) -> int:
    """
    Run migrations from a list.

    Args:
        db: LocalDatabase instance
        current_version: Current schema version
        migrations: List of (version, description, action) tuples

    Returns:
        Number of migrations applied
    """
    applied = 0
    last_version = current_version

    for version, description, action in migrations:
        if version > current_version:
            logger.debug(f"Applying migration {version}: {description}")
            try:
                if callable(action):
                    # Python data migration
                    with db.transaction():
                        action(db)
                        db.execute(
                            "INSERT INTO schema_version (version) VALUES (?)",
                            (version,),
                        )
                else:
                    # SQL migration (may contain multiple statements)
                    with db.transaction():
                        for statement in action.strip().split(";"):
                            statement = statement.strip()
                            if statement:
                                db.execute(statement)
                        db.execute(
                            "INSERT INTO schema_version (version) VALUES (?)",
                            (version,),
                        )
                applied += 1
                last_version = version
            except Exception as e:
                logger.error(f"Migration {version} failed: {e}")
                raise

    if applied > 0:
        logger.debug(f"Applied {applied} migration(s), now at version {last_version}")

    return applied


def run_migrations(db: LocalDatabase) -> int:
    """
    Run pending migrations.

    For new databases (version == 0):
        - Applies the current baseline schema directly.

    For existing databases:
        - Versions 219+ run any future SQLite migrations and repair the system session.
        - Versions below 219 raise MigrationUnsupportedError.
        - Versions above the latest known migration are left untouched.

    Args:
        db: LocalDatabase instance

    Returns:
        Number of migrations applied
    """
    current_version = get_current_version(db)
    total_applied = 0

    if current_version == 0:
        # New database with flattened baseline: apply schema directly
        logger.info("Using flattened baseline for new database")
        _apply_baseline(db)
        total_applied = 1
        current_version = BASELINE_VERSION
    elif current_version < _MIN_MIGRATION_VERSION:
        # Unsupported: pre-launch SQLite database without legacy migrations.
        msg = (
            f"Database version {current_version} predates the SQLite launch "
            f"baseline {_MIN_MIGRATION_VERSION}. Direct upgrade is unsupported. "
            f"To recover: stop the daemon, remove ~/.gobby/gobby-hub.db or "
            f"restore your pre-cutover backup, then restart Gobby to initialize "
            f"schema {BASELINE_VERSION}."
        )
        logger.error(msg)
        raise MigrationUnsupportedError(msg)

    latest_version = latest_known_version()
    if current_version > latest_version:
        logger.info(
            "Database version %s is newer than this build's latest known SQLite "
            "schema %s; leaving it untouched.",
            current_version,
            latest_version,
        )
        return 0

    # Run any new migrations after the flattened launch baseline.
    if MIGRATIONS:
        applied = _run_migration_list(db, current_version, MIGRATIONS)
        total_applied += applied

    # Existing databases may be missing the bootstrapped root session due to
    # prior drift or partial upgrades; restore it idempotently on every startup.
    from gobby.storage.sessions import ensure_system_session
    from gobby.storage.tasks import TaskDispatchMutexManager

    ensure_system_session(db)
    TaskDispatchMutexManager(db).sweep_expired()

    return total_applied
