"""Transcript and delivered-handoff archival summary behavior."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gobby.sessions.handoff_records import (
    build_handoff_payload,
    insert_handoff_record,
    record_handoff_delivery,
)
from gobby.sessions.summarize import generate_session_summaries
from gobby.sessions.summary_validity import is_summary_markdown_valid
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.session_tasks import SessionTaskManager
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import IsolatedCheckoutFactory

pytestmark = pytest.mark.unit

MACHINE_ID = "20000000-0000-4000-8000-000000000002"


def _record_handoff(
    db: HubDatabase,
    manager: SessionManager,
    session_id: str,
    *,
    boundary_kind: Literal["compact", "clear"],
) -> tuple[str, str]:
    payload = build_handoff_payload(
        current_state="The implementation is ready for archival.",
        next_steps=["Resume in the bound successor."],
        key_decisions=["Use the delivered handoff without an LLM."],
    )
    with db.transaction() as conn:
        handoff_id, _authored_at = insert_handoff_record(conn, session_id, payload)
    attempt_id = "1" * 32
    record_handoff_delivery(
        db,
        handoff_id=handoff_id,
        attempt_id=attempt_id,
        boundary_kind=boundary_kind,
        continuation_session_id=session_id,
    )
    manager.update_status(session_id, "expired")
    return handoff_id, payload.rendered_markdown


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", MACHINE_ID):
        yield


@pytest.mark.asyncio
async def test_delivered_handoff_cannot_persist_after_session_revival(
    isolated_checkout_factory: IsolatedCheckoutFactory, temp_db: HubDatabase
) -> None:
    checkout = isolated_checkout_factory(temp_db, "clear-summary-race")
    manager = SessionManager(temp_db)
    session_id = manager.register_session(
        external_id="clear-race",
        machine_id=MACHINE_ID,
        source="claude",
        project_id=checkout.project.id,
    )
    _record_handoff(temp_db, manager, session_id, boundary_kind="clear")

    def revive_before_persist(*args: object) -> None:
        with temp_db.transaction() as conn:
            conn.execute("UPDATE sessions SET status = 'active' WHERE id = %s", (session_id,))

    with patch(
        "gobby.sessions.summarize.find_current_handoff_summary",
        side_effect=revive_before_persist,
    ):
        result = await generate_session_summaries(session_id, manager, db=temp_db)

    saved = manager.get(session_id)
    assert saved is not None
    assert saved.status == "active"
    assert saved.summary_markdown is None
    assert saved.summary_source_context_hash is None
    assert not result["success"]
    assert result["generation_error"] == "Session changed during summary generation"


@pytest.mark.asyncio
async def test_missing_transcript_leaves_archival_summary_empty(
    isolated_checkout_factory: IsolatedCheckoutFactory, temp_db: HubDatabase
) -> None:
    project = isolated_checkout_factory(temp_db, "summary-test").project
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
    assert session.summary_source_context_hash is None
    assert session.summary_generation_mode is None
    assert session.summary_generated_at is None


@pytest.mark.asyncio
async def test_transcript_fallback_persists_current_summary(
    isolated_checkout_factory: IsolatedCheckoutFactory, temp_db: HubDatabase
) -> None:
    root = Path(__file__).resolve().parents[2]
    transcript = root / "tests/sessions/transcripts/fixtures/golden_path/claude.jsonl"
    project = isolated_checkout_factory(temp_db, "summary-test").project
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
    assert session.summary_generation_mode == "full"
    assert session.summary_source_context_hash
    assert session.summary_generated_at is not None


@pytest.mark.asyncio
async def test_compact_delivery_does_not_bypass_missing_transcript_fallback(
    isolated_checkout_factory: IsolatedCheckoutFactory, temp_db: HubDatabase
) -> None:
    project = isolated_checkout_factory(temp_db, "compact-summary").project
    manager = SessionManager(temp_db)
    session_id = manager.register_session(
        external_id="compact-only",
        machine_id=MACHINE_ID,
        source="claude",
        project_id=project.id,
        transcript_path="/tmp/does-not-exist.jsonl",
    )
    _record_handoff(temp_db, manager, session_id, boundary_kind="compact")

    result = await generate_session_summaries(
        session_id=session_id,
        session_manager=manager,
        db=temp_db,
    )

    assert result["success"] is False
    session = manager.get(session_id)
    assert session is not None and session.summary_markdown is None


@pytest.mark.asyncio
async def test_delivered_clear_handoff_builds_evidence_summary_without_llm(
    isolated_checkout_factory: IsolatedCheckoutFactory, temp_db: HubDatabase
) -> None:
    checkout = isolated_checkout_factory(temp_db, "clear-summary")
    manager = SessionManager(temp_db)
    session_id = manager.register_session(
        external_id="clear-delivered",
        machine_id=MACHINE_ID,
        source="claude",
        project_id=checkout.project.id,
        transcript_path="/tmp/does-not-exist.jsonl",
    )
    task = LocalTaskManager(temp_db).create_task(
        checkout.project.id,
        title="Archive delivered handoff",
        validation_criteria="Summary contains bounded evidence.",
    )
    SessionTaskManager(temp_db).link_task(session_id, task.id, "claimed")
    commit_sha = "abc1234def567890"
    temp_db.execute(
        "UPDATE tasks SET commits = %s::jsonb WHERE id = %s",
        (json.dumps([commit_sha]), task.id),
    )
    evidence_file = Path(checkout.root_path) / "evidence.txt"
    evidence_file.write_text("changed\n", encoding="utf-8")
    SessionVariableManager(temp_db).merge_variables(
        session_id,
        {
            "session_edited_files": ["evidence.txt", "../outside.txt"],
            "open_tool_errors": [
                {
                    "tool": "Bash",
                    "target_key": "focused-test",
                    "error": "exact unresolved failure " + ("x" * 500),
                    "first_at": "2026-09-03T10:00:00+00:00",
                    "last_at": "2026-09-03T10:00:01+00:00",
                    "count": 1,
                }
            ],
        },
    )
    _handoff_id, handoff_markdown = _record_handoff(
        temp_db,
        manager,
        session_id,
        boundary_kind="clear",
    )
    llm = MagicMock()
    llm.call_feature = AsyncMock(side_effect=AssertionError("LLM must not be called"))

    first = await generate_session_summaries(
        session_id=session_id,
        session_manager=manager,
        llm_service=llm,
        db=temp_db,
    )
    second = await generate_session_summaries(
        session_id=session_id,
        session_manager=manager,
        llm_service=llm,
        db=temp_db,
    )

    session = manager.get(session_id)
    assert session is not None and session.summary_markdown is not None
    assert first["generation_mode"] == "agent_authored"
    assert second["generation_mode"] == "noop"
    assert session.summary_markdown.startswith(handoff_markdown)
    headings = [
        "## Active Task",
        "## Commits",
        "## Files Changed",
        "## Unresolved Errors",
    ]
    assert [session.summary_markdown.index(heading) for heading in headings] == sorted(
        session.summary_markdown.index(heading) for heading in headings
    )
    assert f"#{task.seq_num} [in_progress] {task.title}" in session.summary_markdown
    assert commit_sha in session.summary_markdown
    assert "evidence.txt" in session.summary_markdown
    assert "../outside.txt" not in session.summary_markdown
    assert "error preview: exact unresolved failure" in session.summary_markdown
    assert "full error: get_variable" in session.summary_markdown
    assert not llm.call_feature.called
    assert session.summary_generation_mode == "agent_authored"
    assert session.summary_source_context_hash is not None
    assert len(session.summary_source_context_hash) == 64
    assert session.summary_generated_at is not None

    expected_markdown = session.summary_markdown
    reused = await generate_session_summaries(
        session_id=session_id,
        session_manager=manager,
        llm_service=llm,
        db=temp_db,
    )
    unchanged = manager.get(session_id)
    assert reused["generation_mode"] == "noop"
    assert unchanged is not None
    assert unchanged.summary_markdown == expected_markdown

    altered_markdown = expected_markdown + "\n\nAdditional valid material."
    assert is_summary_markdown_valid(altered_markdown)
    temp_db.execute(
        "UPDATE sessions SET summary_markdown = %s WHERE id = %s",
        (altered_markdown, session_id),
    )

    regenerated = await generate_session_summaries(
        session_id=session_id,
        session_manager=manager,
        llm_service=llm,
        db=temp_db,
    )

    refreshed = manager.get(session_id)
    assert regenerated["generation_mode"] == "agent_authored"
    assert refreshed is not None
    assert refreshed.summary_markdown == expected_markdown


@pytest.mark.asyncio
async def test_malformed_clear_handoff_retains_transcript_fallback(
    isolated_checkout_factory: IsolatedCheckoutFactory, temp_db: HubDatabase
) -> None:
    project = isolated_checkout_factory(temp_db, "malformed-summary").project
    manager = SessionManager(temp_db)
    session_id = manager.register_session(
        external_id="malformed-clear",
        machine_id=MACHINE_ID,
        source="claude",
        project_id=project.id,
        transcript_path="/tmp/does-not-exist.jsonl",
    )
    handoff_id, _markdown = _record_handoff(
        temp_db,
        manager,
        session_id,
        boundary_kind="clear",
    )
    temp_db.execute(
        "UPDATE session_handoffs SET content_sha256 = %s WHERE id = %s",
        ("0" * 64, handoff_id),
    )

    result = await generate_session_summaries(
        session_id=session_id,
        session_manager=manager,
        db=temp_db,
    )

    assert result["success"] is False
    session = manager.get(session_id)
    assert session is not None and session.summary_markdown is None
