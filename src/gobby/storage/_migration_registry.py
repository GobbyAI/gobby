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
]
