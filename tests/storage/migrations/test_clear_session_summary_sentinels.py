"""Migration coverage for clearing failed session summaries."""

from __future__ import annotations

import importlib

import pytest

from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit


def test_sentinel_summaries_are_cleared(
    session_manager: SessionManager,
    sample_project: dict,
) -> None:
    migration = importlib.import_module("gobby.storage.migrations.clear_session_summary_sentinels")

    failed = session_manager.register(
        external_id="failed-summary",
        machine_id="machine",
        source="claude",
        project_id=sample_project["id"],
    )
    unavailable = session_manager.register(
        external_id="unavailable-summary",
        machine_id="machine",
        source="claude",
        project_id=sample_project["id"],
    )
    normal = session_manager.register(
        external_id="normal-summary",
        machine_id="machine",
        source="claude",
        project_id=sample_project["id"],
    )

    session_manager.db.execute(
        "UPDATE sessions SET summary_markdown = ? WHERE id = ?",
        ("Session summary generation failed: timeout", failed.id),
    )
    session_manager.db.execute(
        "UPDATE sessions SET summary_markdown = ? WHERE id = ?",
        ("Session summary unavailable (Claude CLI not found)", unavailable.id),
    )
    session_manager.db.execute(
        "UPDATE sessions SET summary_markdown = ? WHERE id = ?",
        ("# Normal Summary\n\nWork completed.", normal.id),
    )

    migration.up(session_manager.db)

    rows = {
        row["id"]: row["summary_markdown"]
        for row in session_manager.db.fetchall(
            "SELECT id, summary_markdown FROM sessions WHERE id IN (?, ?, ?)",
            (failed.id, unavailable.id, normal.id),
        )
    }
    assert rows[failed.id] is None
    assert rows[unavailable.id] is None
    assert rows[normal.id] == "# Normal Summary\n\nWork completed."
