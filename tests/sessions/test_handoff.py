"""Structured handoff and feedback persistence contracts."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch
from uuid import uuid4

import pytest

from gobby.mcp_proxy.tools.sessions import create_session_messages_registry
from gobby.sessions.handoff import (
    HANDOFF_PULL_PENDING_VARIABLE,
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
    clear_successor_title,
    recompute_automatic_title,
    update_title_for_claim,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.projects import LocalProjectManager
from gobby.storage.session_models import Session
from gobby.storage.sessions import SessionManager
from gobby.storage.tasks import LocalTaskManager, Task
from gobby.utils.session_context import session_context_for_test
from gobby.workflows.state_manager import SessionVariableManager
from tests.fixtures.isolated_checkout import write_project_marker

pytestmark = pytest.mark.unit

MACHINE_ID = "20000000-0000-4000-8000-000000000002"


@pytest.fixture(autouse=True)
def _local_machine_identity() -> Iterator[None]:
    with patch("gobby.utils.machine_id._cached_machine_id", MACHINE_ID):
        yield


@pytest.fixture
def session_manager(temp_db: HubDatabase, tmp_path: Path) -> SessionManager:
    checkout = tmp_path / "handoff-test"
    checkout.mkdir()
    project_id = str(uuid4())
    write_project_marker(checkout, project_id=project_id, name="handoff-test")
    project = LocalProjectManager(temp_db).create(
        name="handoff-test", repo_path=str(checkout), project_id=project_id
    )
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


def _observation(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "source": "agent",
        "kind": "friction",
        "evidence": "evidence",
        "impact": "impact",
        "frequency": "once",
    }
    base.update(overrides)
    return base


def _feedback_task(**overrides: object) -> Task:
    values: dict[str, object] = {
        "created_in_session_id": "session-current",
        "claimed_by_session_id": None,
        "closed_in_session_id": None,
        "closed_at": None,
        "labels": [],
    }
    values.update(overrides)
    return cast(Task, SimpleNamespace(**values))


def test_feedback_enums_reject_unlisted_values() -> None:
    with pytest.raises(ValueError, match=r"kind must be one of"):
        normalize_feedback_observations([_observation(kind="tool-defect")])
    with pytest.raises(ValueError, match=r"frequency must be one of"):
        normalize_feedback_observations([_observation(frequency="sometimes")])
    with pytest.raises(ValueError, match=r"disposition must be one of"):
        normalize_feedback_observations([_observation(disposition="observed")])


def test_filed_task_requires_a_task_ref_and_labeled_current_session_task() -> None:
    with pytest.raises(ValueError, match=r"observations\[0\]\.disposition: Found-work ladder"):
        normalize_feedback_observations([_observation(disposition="filed-task")])

    unlabeled = _feedback_task()
    with pytest.raises(ValueError, match=r"needs-decision or clean-window"):
        normalize_feedback_observations(
            [_observation(disposition="filed-task", evidence="Filed #21484")],
            resolve_task=lambda _ref: unlabeled,
            session_id="session-current",
        )

    labeled = _feedback_task(labels=["needs-decision"])
    [accepted] = normalize_feedback_observations(
        [_observation(disposition="filed-task", evidence="Filed #21484")],
        resolve_task=lambda _ref: labeled,
        session_id="session-current",
    )

    assert accepted.disposition == "filed-task"


def test_task_refs_accept_short_seq_numbers() -> None:
    labeled = _feedback_task(labels=["needs-decision"])
    seen: list[str] = []

    def resolve(ref: str) -> Task:
        seen.append(ref)
        return labeled

    [accepted] = normalize_feedback_observations(
        [_observation(disposition="filed-task", evidence="Filed #42")],
        resolve_task=resolve,
        session_id="session-current",
    )

    assert accepted.disposition == "filed-task"
    assert seen == ["#42"]


def test_fixed_requires_a_task_owned_by_current_session() -> None:
    foreign = _feedback_task(claimed_by_session_id="session-other")

    with pytest.raises(ValueError, match=r"claimed or closed by this session"):
        normalize_feedback_observations(
            [_observation(disposition="fixed", evidence="Tracked in #21484")],
            resolve_task=lambda _ref: foreign,
            session_id="session-current",
        )


def test_escalated_requires_an_owner_session_ref() -> None:
    with pytest.raises(ValueError, match=r"active owner session ref"):
        normalize_feedback_observations(
            [_observation(disposition="escalated", evidence="Sent the failure to its owner")]
        )


def test_feedback_other_kind_requires_a_novel_label() -> None:
    with pytest.raises(ValueError, match=r"kind_other_label \(required"):
        normalize_feedback_observations([_observation(kind="other")])
    with pytest.raises(ValueError, match=r"restates the 'friction' kind"):
        normalize_feedback_observations([_observation(kind="other", kind_other_label="Friction")])
    with pytest.raises(ValueError, match=r"restates the 'missing-affordance' kind"):
        normalize_feedback_observations(
            [_observation(kind="other", kind_other_label="missing_affordance")]
        )
    with pytest.raises(ValueError, match=r"only allowed when kind is 'other'"):
        normalize_feedback_observations([_observation(kind="bug", kind_other_label="latency")])
    [labeled] = normalize_feedback_observations(
        [_observation(kind="other", kind_other_label="doc-drift")]
    )
    assert labeled.kind == "other"
    assert labeled.kind_other_label == "doc-drift"


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
                "disposition": "noted",
            },
            {
                "source": "agent",
                "kind": "other",
                "kind_other_label": "doc-drift",
                "evidence": "The guide contradicted the tool schema.",
                "impact": "Cost one wrong call.",
                "frequency": "repeated",
            },
        ]
    )

    assert write_feedback_batch(temp_db, session.id, []) == []
    ids = write_feedback_batch(temp_db, session.id, observations)
    rows = temp_db.fetchall(
        "SELECT * FROM session_feedback WHERE session_id = %s ORDER BY created_at, id",
        (session.id,),
    )
    assert len(ids) == len(rows) == 3
    assert all(row["reviewed"] is False for row in rows)
    assert all(row["created_at"].utcoffset().total_seconds() == 0 for row in rows)
    labels = {row["kind"]: row["kind_other_label"] for row in rows}
    assert labels == {"friction": None, "useful": None, "other": "doc-drift"}


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
    sv_mgr = SessionVariableManager(temp_db)
    assert sv_mgr.get_variables(predecessor.id).get(HANDOFF_PULL_PENDING_VARIABLE) is True
    compact = consume_pending_handoff(temp_db, predecessor.id)
    assert compact is not None and compact.markdown == markdown
    assert HANDOFF_PULL_PENDING_VARIABLE not in sv_mgr.get_variables(predecessor.id)
    assert consume_pending_handoff(temp_db, predecessor.id) is None

    stage_handoff_attempt(
        temp_db,
        predecessor.id,
        attempt_id="clear-attempt",
        markdown=markdown,
        observations=[],
        clear_session=True,
    )
    assert HANDOFF_PULL_PENDING_VARIABLE not in sv_mgr.get_variables(predecessor.id)
    successor_id = session_manager.register_session(
        external_id="clear-successor",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=predecessor.project_id,
        parent_session_id=predecessor.id,
    )
    sv_mgr.merge_variables(successor_id, {HANDOFF_PULL_PENDING_VARIABLE: True})
    cleared = consume_pending_handoff(temp_db, successor_id)
    assert cleared is not None and cleared.session_id == predecessor.id
    assert HANDOFF_PULL_PENDING_VARIABLE not in sv_mgr.get_variables(successor_id)
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
    sv_mgr = SessionVariableManager(temp_db)
    assert sv_mgr.get_variables(session.id).get(HANDOFF_PULL_PENDING_VARIABLE) is True

    assert restore_handoff_attempt(temp_db, state) is True
    assert HANDOFF_PULL_PENDING_VARIABLE not in sv_mgr.get_variables(session.id)
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
    item_properties = schema.input_schema["properties"]["gobby_feedback"]["items"]["properties"]
    assert item_properties["kind"]["enum"] == [
        "friction",
        "bug",
        "noise",
        "surprise",
        "missing-affordance",
        "useful",
        "other",
    ]
    assert item_properties["frequency"]["enum"] == ["once", "repeated", "always"]
    assert "kind_other_label" in item_properties

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
    assert session.title == f"(handoff-test-S#{session.seq_num}): Codex"

    update_title_for_claim(
        session_manager,
        session.id,
        SimpleNamespace(seq_num=42, title="Implement handoffs"),
    )
    assert _title(session_manager, session.id) == (
        f"(handoff-test-S#{session.seq_num}): Task #42 - Implement handoffs"
    )

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


def test_clear_successor_task_title_uses_successor_session_ref(
    session_manager: SessionManager,
) -> None:
    predecessor = _registered_session(session_manager)
    task = LocalTaskManager(session_manager.db).create_task(
        project_id=predecessor.project_id,
        title="Continue claimed work",
        claimed_by_session_id=predecessor.id,
        category="code",
        validation_criteria="Title lifecycle test task.",
        implementation_domain="backend",
    )

    title, title_source = clear_successor_title(
        session_manager.db,
        predecessor,
        successor_seq_num=99,
    )

    assert title == (f"(handoff-test-S#99): Task #{task.seq_num} - Continue claimed work")
    assert title_source == "task"
