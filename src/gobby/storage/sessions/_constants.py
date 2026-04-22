"""Shared constants and bootstrap helpers for session storage."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import cast

from gobby.storage.database import DatabaseProtocol
from gobby.storage.projects import PERSONAL_PROJECT_ID

logger = logging.getLogger("gobby.storage.sessions")


def get_logger() -> logging.Logger:
    """Resolve package logger through the public import path for patch compatibility."""
    import gobby.storage.sessions as sessions_module

    return cast(logging.Logger, sessions_module.logger)


# Well-known system session ID — bootstrapped at DB init.
# Used as the default parent for pipelines and cron jobs that have no caller session.
SYSTEM_SESSION_ID = "00000000-0000-0000-0000-000000000001"
SYSTEM_SESSION_PROJECT_ID = PERSONAL_PROJECT_ID
SYSTEM_SESSION_EXTERNAL_ID = "system"
SYSTEM_SESSION_MACHINE_ID = "system"
SYSTEM_SESSION_SOURCE = "system"
SYSTEM_SESSION_TITLE = "_system"


def ensure_system_session(db: DatabaseProtocol) -> None:
    """Ensure the bootstrapped root session for cron/pipeline work exists."""
    with db.transaction_immediate():
        existing = db.fetchone("SELECT id FROM sessions WHERE id = ?", (SYSTEM_SESSION_ID,))
        if existing is not None:
            return

        project = db.fetchone("SELECT id FROM projects WHERE id = ?", (SYSTEM_SESSION_PROJECT_ID,))
        if project is None:
            raise RuntimeError(
                "Missing required _personal project for system session bootstrap "
                f"({SYSTEM_SESSION_PROJECT_ID})"
            )

        now = datetime.now(UTC).isoformat()
        db.execute(
            """
            INSERT OR IGNORE INTO sessions (
                id, external_id, machine_id, source, project_id, title,
                status, agent_depth, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'active', 0, ?, ?)
            """,
            (
                SYSTEM_SESSION_ID,
                SYSTEM_SESSION_EXTERNAL_ID,
                SYSTEM_SESSION_MACHINE_ID,
                SYSTEM_SESSION_SOURCE,
                SYSTEM_SESSION_PROJECT_ID,
                SYSTEM_SESSION_TITLE,
                now,
                now,
            ),
        )

        recreated = db.fetchone("SELECT id FROM sessions WHERE id = ?", (SYSTEM_SESSION_ID,))
        if recreated is None:
            raise RuntimeError(f"Failed to recreate missing system session {SYSTEM_SESSION_ID}")

    get_logger().warning("Recreated missing system session %s", SYSTEM_SESSION_ID)
