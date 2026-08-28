"""Structured handoff and feedback persistence contracts."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gobby.sessions.handoff import (
    FeedbackObservation,
    consume_pending_handoff,
    normalize_feedback_observations,
    render_handoff_markdown,
    restore_handoff_attempt,
    stage_handoff_attempt,
    write_feedback_batch,
)
from gobby.sessions.title_lifecycle import (
    apply_clear_successor_title,
    recompute_automatic_title,
    update_title_for_claim,
)
from gobby.mcp_proxy.tools.sessions import create_session_messages_registry
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.utils.session_context import session_context_for_test

pytestmark = pytest.mark.unit

MACHINE_ID = "20000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", MACHINE_ID):
        yield


@pytest.fixture
def session_manager(temp_db: HubDatabase) -> SessionManager:
    project = LocalProjectManager(temp_db).create(name="handoff-test", repo_path="/tmp/test")
    manager = SessionManager(temp_db)
    manager.register_session(
        external_id="handoff-session",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=project.id,
    )
    return manager


def _registered_session(manager: SessionManager) -> Session:
    row = manager.db.fetchone(
        "SELECT id FROM sessions WHERE external_id = %s",
        ("handoff-session",),
    )
    assert row is not None
    session = manager.get(str(row["id"]))
    assert session is not None
    return session


def _title(manager: SessionManager, session_id: str) -> str | None:
    session = manager.get(session_id)
    assert session is not None
    return session.title


def test_render_handoff_is_structured_deterministic_and_excludes_feedback() -> None:
    markdown = render_handoff_markdown(
        current_state="Implementation is staged.",
        next_steps=["Run focused tests", "Commit the change"],
        key_decisions=["Use pull-only recovery"],
        blockers=["Await isolated database"],
        notes=["Preserve archival summaries"],
        references=["#21140", "src/gobby/sessions/handoff.py", "#21140"],
    )

    assert (
        markdown
        == """## Current State

Implementation is staged.

## Next Steps

1. Run focused tests
2. Commit the change

## Key Decisions

- Use pull-only recovery

## Blockers

- Await isolated database

## Notes

- Preserve archival summaries

## References

- #21140
- src/gobby/sessions/handoff.py"""
    )
    assert "feedback" not in markdown.casefold()


@pytest.mark.parametrize(
    ("current_state", "next_steps"),
    [("", ["next"]), ("  ", ["next"]), ("state", []), ("state", [" "])],
)
def test_render_handoff_rejects_blank_required_fields(
    current_state: str,
    next_steps: list[str],
) -> None:
    with pytest.raises(ValueError):
        render_handoff_markdown(current_state=current_state, next_steps=next_steps)


def test_optional_handoff_and_feedback_entries_reject_blanks() -> None:
    with pytest.raises(ValueError, match=r"notes\[0\]"):
        render_handoff_markdown(
            current_state="Ready",
            next_steps=["Continue"],
            notes=[" "],
        )
    with pytest.raises(ValueError, match="suggestion"):
        normalize_feedback_observations(
            [
                {
                    "source": "agent",
                    "kind": "friction",
                    "evidence": "evidence",
                    "impact": "impact",
                    "frequency": "once",
                    "suggestion": " ",
                }
            ]
        )


def test_feedback_batch_writes_one_row_per_observation_and_empty_is_noop(
    temp_db: HubDatabase,
    session_manager: SessionManager,
) -> None:
    session = _registered_session(session_manager)
    observations = normalize_feedback_observations(
        [
            {
                "source": "agent",
                "kind": "friction",
                "evidence": "The tool required a duplicate retry.",
                "impact": "Added one round trip.",
                "frequency": "once",
            },
            {
                "source": "agent",
                "kind": "useful",
                "evidence": "The schema gate returned an exact repair.",
                "impact": "Prevented a malformed call.",
                "frequency": "once",
                "suggestion": "Keep the repair hint.",
                "disposition": "observed",
            },
        ]
    )

    assert write_feedback_batch(temp_db, session.id, []) == []
    ids = write_feedback_batch(temp_db, session.id, observations)
    rows = temp_db.fetchall(
        "SELECT * FROM session_feedback WHERE session_id = %s ORDER BY created_at, id",
        (session.id,),
    )
    assert len(ids) == len(rows) == 2
    assert all(row["reviewed"] is False for row in rows)
    assert all(row["created_at"].utcoffset().total_seconds() == 0 for row in rows)


def test_handoff_consumes_once_for_compact_and_clear_successor(
    temp_db: HubDatabase,
    session_manager: SessionManager,
) -> None:
    predecessor = _registered_session(session_manager)
    markdown = render_handoff_markdown(current_state="Ready.", next_steps=["Continue."])
    stage_handoff_attempt(
        temp_db,
        predecessor.id,
        attempt_id="compact-attempt",
        markdown=markdown,
        observations=[],
        clear_session=False,
    )
    compact = consume_pending_handoff(temp_db, predecessor.id)
    assert compact is not None and compact.markdown == markdown
    assert consume_pending_handoff(temp_db, predecessor.id) is None

    stage_handoff_attempt(
        temp_db,
        predecessor.id,
        attempt_id="clear-attempt",
        markdown=markdown,
        observations=[],
        clear_session=True,
    )
    successor_id = session_manager.register_session(
        external_id="clear-successor",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=predecessor.project_id,
        parent_session_id=predecessor.id,
    )
    cleared = consume_pending_handoff(temp_db, successor_id)
    assert cleared is not None and cleared.session_id == predecessor.id
    assert consume_pending_handoff(temp_db, successor_id) is None


def test_failed_attempt_restores_handoff_and_deletes_attempt_feedback(
    temp_db: HubDatabase,
    session_manager: SessionManager,
) -> None:
    session = _registered_session(session_manager)
    temp_db.execute(
        "UPDATE sessions SET handoff_markdown = %s WHERE id = %s",
        ("previous", session.id),
    )
    state = stage_handoff_attempt(
        temp_db,
        session.id,
        attempt_id="failed-attempt",
        markdown="replacement",
        observations=[FeedbackObservation("agent", "friction", "evidence", "impact", "once")],
        clear_session=False,
    )

    assert restore_handoff_attempt(temp_db, state) is True
    row = temp_db.fetchone(
        "SELECT handoff_markdown FROM sessions WHERE id = %s",
        (session.id,),
    )
    assert row is not None and row["handoff_markdown"] == "previous"
    count = temp_db.fetchone(
        "SELECT COUNT(*) AS count FROM session_feedback WHERE session_id = %s",
        (session.id,),
    )
    assert count is not None and count["count"] == 0


@pytest.mark.asyncio
async def test_tool_schemas_expose_new_surface_and_legacy_names_are_absent(
    temp_db: HubDatabase,
    session_manager: SessionManager,
) -> None:
    session = _registered_session(session_manager)
    registry = create_session_messages_registry(session_manager=session_manager, db=temp_db)
    names = {tool["name"] for tool in registry.list_tools()}
    assert {"set_handoff", "get_handoff", "feedback", "set_title"} <= names
    assert not {
        "compact_self",
        "clear_self",
        "set_handoff_context",
        "get_handoff_context",
    }.intersection(names)
    schema = registry.get_tool_metadata("set_handoff")
    assert schema is not None
    assert schema.input_schema["required"] == ["current_state", "next_steps"]
    assert schema.input_schema["properties"]["gobby_feedback"]["items"]["required"] == [
        "source",
        "kind",
        "evidence",
        "impact",
        "frequency",
    ]

    with session_context_for_test(session.id):
        assert await registry.call("feedback", {"observations": []}) == {
            "success": True,
            "created": 0,
            "feedback_ids": [],
        }
        empty = await registry.call("get_handoff", {})
        assert empty["success"] is True and empty["found"] is False
        renamed = await registry.call("set_title", {"title": "Manual title"})
        assert renamed["title"] == "Manual title"


def test_title_lifecycle_is_provisional_task_manual_and_clear_sticky(
    session_manager: SessionManager,
) -> None:
    session = _registered_session(session_manager)
    assert session.seq_num is not None
    assert session.title == f"(gobby): S#{session.seq_num}"

    update_title_for_claim(
        session_manager,
        session.id,
        SimpleNamespace(seq_num=42, title="Implement handoffs"),
    )
    assert _title(session_manager, session.id) == "(gobby): Task #42 - Implement handoffs"

    session_manager.update_title(session.id, "Sticky", title_source="manual")
    update_title_for_claim(
        session_manager,
        session.id,
        SimpleNamespace(seq_num=43, title="Another task"),
    )
    assert _title(session_manager, session.id) == "Sticky"

    successor_id = session_manager.register_session(
        external_id="title-successor",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=session.project_id,
    )
    predecessor = session_manager.get(session.id)
    assert predecessor is not None
    apply_clear_successor_title(session_manager, successor_id, predecessor)
    assert _title(session_manager, successor_id) == "Sticky"

    session_manager.update_title(successor_id, "temporary", title_source="task")
    assert _title(session_manager, successor_id) == "Sticky"
    recomputed = recompute_automatic_title(session_manager, successor_id)
    assert recomputed is not None and recomputed.title == "Sticky"
