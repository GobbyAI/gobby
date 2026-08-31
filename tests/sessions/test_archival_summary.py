"""Transcript-only archival summary behavior."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from gobby.sessions.summarize import generate_session_summaries
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager

pytestmark = pytest.mark.unit

MACHINE_ID = "20000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", MACHINE_ID):
        yield


@pytest.mark.asyncio
async def test_missing_transcript_leaves_archival_summary_empty(temp_db: HubDatabase) -> None:
    project = LocalProjectManager(temp_db).create(name="summary-test", repo_path="/tmp/test")
    manager = SessionManager(temp_db)
    session_id = manager.register_session(
        external_id="missing-transcript",
        machine_id=MACHINE_ID,
        source="claude",
        project_id=project.id,
        transcript_path="/tmp/does-not-exist.jsonl",
    )

    result = await generate_session_summaries(
        session_id=session_id,
        session_manager=manager,
        db=temp_db,
    )

    assert result["success"] is False
    session = manager.get(session_id)
    assert session is not None
    assert session.summary_markdown is None
    revision_count = temp_db.fetchone(
        "SELECT COUNT(*) AS count FROM session_summary_revisions WHERE session_id = %s",
        (session_id,),
    )
    assert revision_count is not None
    assert revision_count["count"] == 0


@pytest.mark.asyncio
async def test_transcript_fallback_persists_summary_revision(
    temp_db: HubDatabase, tmp_path: Path
) -> None:
    root = Path(__file__).resolve().parents[2]
    transcript = root / "tests/sessions/transcripts/fixtures/golden_path/claude.jsonl"
    # repo_path must not point at this checkout: running the suite from a linked
    # worktree would trip the isolation-path repo_path guard.
    project = LocalProjectManager(temp_db).create(name="summary-test", repo_path=str(tmp_path))
    manager = SessionManager(temp_db)
    session_id = manager.register_session(
        external_id="archival-summary",
        machine_id=MACHINE_ID,
        source="claude",
        project_id=project.id,
        transcript_path=str(transcript),
    )

    result = await generate_session_summaries(
        session_id=session_id,
        session_manager=manager,
        db=temp_db,
    )

    session = manager.get(session_id)
    assert session is not None
    assert result["success"] is True
    assert result["generation_mode"] == "full"
    assert session.summary_markdown
    revision = temp_db.fetchone(
        "SELECT generation_mode, source_context_hash FROM session_summary_revisions "
        "WHERE session_id = %s",
        (session_id,),
    )
    assert revision is not None
    assert revision["generation_mode"] == "full"
    assert revision["source_context_hash"]
