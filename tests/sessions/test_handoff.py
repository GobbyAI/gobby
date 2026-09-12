"""Structured handoff and feedback persistence contracts."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

import pytest
from psycopg.errors import CheckViolation

from gobby.mcp_proxy.tools.sessions import create_session_messages_registry
from gobby.mcp_proxy.tools.sessions._handoff import FEEDBACK_OBSERVATION_INPUT_SCHEMA
from gobby.sessions.clear_continuation import (
    CLEAR_ATTEMPT_VARIABLE,
    clear_failed_attempt,
    stage_clear_attempt,
    take_clear_handoff_marker,
)
from gobby.sessions.handoff import (
    HANDOFF_DISPATCH_GATE_VARIABLE,
    HANDOFF_PULL_PENDING_VARIABLE,
    PENDING_HANDOFF_VARIABLE,
    claim_staged_handoff_delivery,
    consume_pending_handoff,
    normalize_feedback_observations,
    render_handoff_markdown,
    restore_handoff_attempt,
    restore_staged_handoff,
    stage_handoff_attempt,
    staged_handoff_rejection,
    write_feedback_batch,
)
from gobby.sessions.handoff_records import (
    build_handoff_payload,
    get_agent_end_handoff,
    record_handoff_delivery,
    stage_agent_end_handoff,
)
from gobby.sessions.title_lifecycle import (
    apply_clear_successor_title,
    clear_successor_title,
    recompute_automatic_title,
    update_title_for_claim,
)
from gobby.storage.agents import LocalAgentRunManager
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


def _agent_boundary(
    manager: SessionManager,
    *,
    suffix: str,
    parent: Session | None = None,
) -> tuple[Session, Session, str]:
    parent_session = parent or _registered_session(manager)
    child_id = manager.register_session(
        external_id=f"agent-child-{suffix}",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=parent_session.project_id,
        parent_session_id=parent_session.id,
    )
    child = manager.get(child_id)
    assert child is not None
    run = LocalAgentRunManager(manager.db).create(
        parent_session_id=parent_session.id,
        child_session_id=child.id,
        provider="codex",
        prompt=f"agent handoff {suffix}",
    )
    return parent_session, child, run.id


def _title(manager: SessionManager, session_id: str) -> str | None:
    session = manager.get(session_id)
    assert session is not None
    return session.title


def test_render_handoff_is_structured_deterministic_and_excludes_feedback() -> None:
    markdown = render_handoff_markdown(
        current_state="Implementation is staged.",
        next_steps=["Run focused tests", "Commit the change"],
        what_was_accomplished=["Stored authored content"],
        key_decisions=["Use pull-only recovery"],
        problems_encountered=["The old row mixed content and delivery state"],
        what_didnt_work=["Inferring delivery from mutable Markdown"],
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

## What Was Accomplished

- Stored authored content

## Key Decisions

- Use pull-only recovery

## Problems Encountered

- The old row mixed content and delivery state

## What Didn’t Work

- Inferring delivery from mutable Markdown

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
                    "source": "agent:test-agent",
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
        "source": "agent:test-agent",
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


@pytest.mark.parametrize(
    "source",
    [
        "gobby-tasks:close_task",
        "rule:x",
        "hook:PostToolUse",
        "cli:gobby restart",
        "binary:gcode",
        "src/gobby/hooks/x.py",
        "docs/guides/sessions.md",
    ],
)
def test_feedback_source_accepts_gobby_surfaces(source: str) -> None:
    assert normalize_feedback_observations([_observation(source=source)])[0].source == source


@pytest.mark.parametrize(
    "source",
    ["agent", "close_task", "session #11210", "my-app:api", "app/models.py", "tool:x"],
)
def test_feedback_source_rejects_ambiguous_or_non_gobby_sources(source: str) -> None:
    with pytest.raises(ValueError, match=r"observations\[0\]\.source must name a Gobby surface"):
        normalize_feedback_observations([_observation(source=source)])


def _register_spawn_chain(
    manager: SessionManager,
    reporter: Session,
    *,
    depth: int,
) -> list[str]:
    run_manager = LocalAgentRunManager(manager.db)
    parent_session_id = reporter.id
    descendant_session_ids: list[str] = []
    for index in range(depth):
        child_session_id = manager.register_session(
            external_id=f"feedback-child-{index}",
            machine_id=MACHINE_ID,
            source="codex",
            project_id=reporter.project_id,
            parent_session_id=parent_session_id,
            agent_depth=index + 1,
        )
        assert child_session_id
        run_manager.create(
            parent_session_id=parent_session_id,
            provider="codex",
            prompt="Test feedback ownership",
            child_session_id=child_session_id,
        )
        descendant_session_ids.append(child_session_id)
        parent_session_id = child_session_id
    return descendant_session_ids


async def _submit_fixed_feedback(
    manager: SessionManager,
    reporter: Session,
    owner_session_id: str,
) -> dict[str, Any]:
    task_manager = LocalTaskManager(manager.db)
    task = task_manager.create_task(
        project_id=reporter.project_id,
        title="Fix feedback ownership test defect",
        claimed_by_session_id=owner_session_id,
        validation_criteria="Task records the completing session.",
    )
    task = task_manager.close_task(
        task.id,
        force=True,
        closed_in_session_id=owner_session_id,
    )
    assert task.seq_num is not None
    registry = create_session_messages_registry(
        session_manager=manager,
        db=manager.db,
        task_manager=task_manager,
    )
    with session_context_for_test(reporter.id):
        result = await registry.call(
            "feedback",
            {
                "observations": [
                    _observation(
                        disposition="fixed",
                        evidence=f"Tracked in #{task.seq_num}",
                    )
                ]
            },
        )
    assert isinstance(result, dict)
    return cast(dict[str, Any], result)


def test_feedback_enums_reject_unlisted_values() -> None:
    with pytest.raises(ValueError, match=r"kind must be one of"):
        normalize_feedback_observations([_observation(kind="tool-defect")])
    with pytest.raises(ValueError, match=r"frequency must be one of"):
        normalize_feedback_observations([_observation(frequency="sometimes")])
    with pytest.raises(ValueError, match=r"disposition must be one of"):
        normalize_feedback_observations([_observation(disposition="observed")])


@pytest.mark.parametrize("label", ["needs-decision", "needs-planning", "clean-window"])
def test_filed_task_requires_a_task_ref_and_labeled_current_session_task(label: str) -> None:
    with pytest.raises(ValueError, match=r"observations\[0\]\.disposition: Found-work ladder"):
        normalize_feedback_observations([_observation(disposition="filed-task")])

    unlabeled = _feedback_task()
    with pytest.raises(ValueError, match=r"needs-decision, needs-planning, or clean-window"):
        normalize_feedback_observations(
            [_observation(disposition="filed-task", evidence="Filed #21484")],
            resolve_task=lambda _ref: unlabeled,
            session_id="session-current",
        )

    labeled = _feedback_task(labels=[label])
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


@pytest.mark.parametrize("owner_field", ("claimed_by_session_id", "closed_in_session_id"))
def test_fixed_accepts_task_owned_by_current_session(owner_field: str) -> None:
    task = _feedback_task(**{owner_field: "session-current"})

    [accepted] = normalize_feedback_observations(
        [_observation(disposition="fixed", evidence="Tracked in #21484")],
        resolve_task=lambda _ref: task,
        session_id="session-current",
    )

    assert accepted.disposition == "fixed"


def test_fixed_rejects_task_owned_by_unrelated_session() -> None:
    foreign = _feedback_task(claimed_by_session_id="session-other")

    with pytest.raises(
        ValueError,
        match=r"claimed or closed by this session or by a spawned descendant session",
    ):
        normalize_feedback_observations(
            [_observation(disposition="fixed", evidence="Tracked in #21484")],
            resolve_task=lambda _ref: foreign,
            session_id="session-current",
        )


@pytest.mark.asyncio
async def test_fixed_accepts_task_closed_by_depth_one_descendant(
    session_manager: SessionManager,
) -> None:
    reporter = _registered_session(session_manager)
    [descendant_session_id] = _register_spawn_chain(session_manager, reporter, depth=1)

    result = await _submit_fixed_feedback(session_manager, reporter, descendant_session_id)

    assert result["success"] is True
    assert result["created"] == 1


@pytest.mark.asyncio
async def test_fixed_accepts_task_closed_by_depth_three_descendant(
    session_manager: SessionManager,
) -> None:
    reporter = _registered_session(session_manager)
    descendant_session_id = _register_spawn_chain(session_manager, reporter, depth=3)[-1]

    result = await _submit_fixed_feedback(session_manager, reporter, descendant_session_id)

    assert result["success"] is True
    assert result["created"] == 1


@pytest.mark.asyncio
async def test_fixed_rejects_task_closed_by_depth_six_descendant(
    session_manager: SessionManager,
) -> None:
    reporter = _registered_session(session_manager)
    descendant_session_id = _register_spawn_chain(session_manager, reporter, depth=6)[-1]

    result = await _submit_fixed_feedback(session_manager, reporter, descendant_session_id)

    assert result["success"] is False
    assert result["error_code"] == "invalid_feedback"
    assert (
        "'fixed' requires a task claimed or closed by this session or by a spawned "
        "descendant session" in result["error"]
    )


@pytest.mark.asyncio
async def test_fixed_rejects_task_closed_by_unrelated_session(
    session_manager: SessionManager,
) -> None:
    reporter = _registered_session(session_manager)
    unrelated_session_id = session_manager.register_session(
        external_id="feedback-unrelated",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=reporter.project_id,
    )
    assert unrelated_session_id

    result = await _submit_fixed_feedback(session_manager, reporter, unrelated_session_id)

    assert result["success"] is False
    assert "spawned descendant session" in result["error"]


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
                "source": "agent:test-agent",
                "kind": "friction",
                "evidence": "The tool required a duplicate retry.",
                "impact": "Added one round trip.",
                "frequency": "once",
            },
            {
                "source": "agent:test-agent",
                "kind": "useful",
                "evidence": "The schema gate returned an exact repair.",
                "impact": "Prevented a malformed call.",
                "frequency": "once",
                "suggestion": "Keep the repair hint.",
                "disposition": "noted",
            },
            {
                "source": "agent:test-agent",
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
    handoff = build_handoff_payload(current_state="Ready.", next_steps=["Continue."])
    compact_state = stage_handoff_attempt(
        temp_db,
        predecessor.id,
        attempt_id="a" * 32,
        handoff=handoff,
        clear_session=False,
    )
    sv_mgr = SessionVariableManager(temp_db)
    assert sv_mgr.get_variables(predecessor.id).get(HANDOFF_PULL_PENDING_VARIABLE) is True
    compact = consume_pending_handoff(temp_db, predecessor.id)
    assert compact is not None and compact.markdown == handoff.rendered_markdown
    compact_receipt = temp_db.fetchone(
        "SELECT * FROM session_handoff_deliveries WHERE handoff_id = %s",
        (compact_state.handoff_record_id,),
    )
    assert compact_receipt is not None
    assert compact_receipt["boundary_kind"] == "compact"
    assert HANDOFF_PULL_PENDING_VARIABLE not in sv_mgr.get_variables(predecessor.id)
    assert consume_pending_handoff(temp_db, predecessor.id) is None

    clear_state = stage_handoff_attempt(
        temp_db,
        predecessor.id,
        attempt_id="b" * 32,
        handoff=handoff,
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
    record_handoff_delivery(
        temp_db,
        handoff_id=clear_state.handoff_record_id,
        attempt_id="b" * 32,
        boundary_kind="clear",
        continuation_session_id=successor_id,
    )
    sv_mgr.merge_variables(successor_id, {HANDOFF_PULL_PENDING_VARIABLE: True})
    cleared = consume_pending_handoff(temp_db, successor_id)
    assert cleared is not None and cleared.session_id == predecessor.id
    assert HANDOFF_PULL_PENDING_VARIABLE not in sv_mgr.get_variables(successor_id)
    assert consume_pending_handoff(temp_db, successor_id) is None


@pytest.mark.asyncio
async def test_plan_draft_round_trips_through_compaction_and_argumentless_get_handoff(
    temp_db: HubDatabase,
    session_manager: SessionManager,
) -> None:
    """A substantial staged plan survives authored storage and one compact delivery."""
    session = _registered_session(session_manager)
    draft = dedent(
        """\
        # Session-Recoverable Planning Draft

        Plan artifact: staged conversational draft; materialize at
        `.gobby/plans/session-recovery.md` when project writes become available.

        **Plan ID:** session-recovery

        ## Overview
        `kind: framing`

        Preserve one complete planning authority across a write-restricted provider
        boundary. The draft must retain implementation details, exact commands,
        acceptance criteria, and fenced examples without a scratch store. The
        continuation restores the authored payload before doing more planning.

        ## Constraints
        `kind: framing`

        - No project or home-directory writes while provider permissions are absent.
        - No plan registration before a real expansion root exists.
        - Base validation runs only after canonical materialization.
        - Expansion remains blocked until manifest application and explicit approval.

        ## P1: Preserve the staged authority
        `kind: framing`

        ### 1.1 Store the complete narrative
        `kind: deliverable`

        Targets:
        - `src/gobby/sessions/handoff_records.py::build_handoff_payload`
        - `tests/sessions/test_handoff.py::*` — scope-reason: verify persisted handoff behavior

        Keep the entire Markdown draft in `current_state`. Store decision and stage
        state in `key_decisions`, unresolved material questions in `notes`, and the
        exact continuation point in `next_steps`. The authored record and rendered
        Markdown must represent the same payload.

        ```python
        def restore_plan(handoff: HandoffPayload) -> str:
            assert handoff.next_steps
            return handoff.current_state
        ```

        **Acceptance:**

        - 1.1.1 - The authored record preserves the exact plan body. behavior: "current_state roundtrip" in `tests/sessions/test_handoff.py`.
        - 1.1.2 - Structured decisions and unresolved questions survive rendering. behavior: "structured metadata roundtrip" in `tests/sessions/test_handoff.py`.
        - 1.1.3 - The first argumentless retrieval consumes the compact handoff exactly once. test: `tests/sessions/test_handoff.py::test_plan_draft_round_trips_through_compaction_and_argumentless_get_handoff`.

        ### 1.2 Materialize and validate
        `kind: deliverable`

        Targets:
        - `src/gobby/install/shared/skills/plan/references/drafting-and-staging.md`

        When writes become available, materialize this whole draft at its canonical
        path, replace staging-only provenance, and run project-aware base validation.
        Review is optional; user approval and post-manifest expansion validation are
        mandatory.

        ```yaml
        approvals:
          drafting: approved
          enhancement: declined
          adversarial_review: pending
          expansion: blocked
        ```

        **Acceptance:**

        - 1.2.1 - Materialization establishes one canonical file authority. file: `.gobby/plans/session-recovery.md`.
        - 1.2.2 - No staged draft is represented as already validated. behavior: "validation boundary" in `.gobby/plans/session-recovery.md`.

        ## Verification
        `kind: verification`

        Query the isolated authored row, consume it through the session MCP registry,
        inspect the compact delivery receipt, and prove a second argumentless
        retrieval returns no handoff.

        ## Open Questions
        `kind: framing`

        - Must a resumed provider preserve the original slug after permissions change?
        - Which review stages has the user explicitly approved?
        """
    ).strip()
    assert len(draft) > 2_000
    assert "```python" in draft
    assert "```yaml" in draft
    assert "## Open Questions" in draft

    key_decisions = (
        "Decision Record: the canonical path is .gobby/plans/session-recovery.md.",
        "Stage approvals: drafting approved; enhancement declined; adversarial review pending.",
    )
    notes = ("Unresolved material questions: preserve the slug after provider permissions change.",)
    next_steps = (
        "Materialize the complete draft when project writes are allowed.",
        "Run project-aware base validation before any optional review.",
    )
    payload = build_handoff_payload(
        current_state=draft,
        next_steps=next_steps,
        key_decisions=key_decisions,
        notes=notes,
    )
    attempt_id = "7" * 32
    attempt = stage_handoff_attempt(
        temp_db,
        session.id,
        attempt_id=attempt_id,
        handoff=payload,
        clear_session=False,
    )

    authored = temp_db.fetchone(
        """
        SELECT current_state, next_steps_json, key_decisions_json, notes_json,
               rendered_markdown
        FROM session_handoffs
        WHERE id = %s
        """,
        (attempt.handoff_record_id,),
    )
    assert authored is not None
    assert authored["current_state"] == draft
    assert json.loads(str(authored["next_steps_json"])) == list(next_steps)
    assert json.loads(str(authored["key_decisions_json"])) == list(key_decisions)
    assert json.loads(str(authored["notes_json"])) == list(notes)
    assert authored["rendered_markdown"] == payload.rendered_markdown

    registry = create_session_messages_registry(session_manager=session_manager, db=temp_db)
    with session_context_for_test(session.id):
        delivered = await registry.call("get_handoff", {})
        consumed = await registry.call("get_handoff", {})

    assert delivered == {
        "success": True,
        "found": True,
        "session_id": session.id,
        "handoff": payload.rendered_markdown,
    }
    assert draft in delivered["handoff"]
    for item in (*key_decisions, *notes, *next_steps):
        assert item in delivered["handoff"]
    assert consumed == {
        "success": True,
        "found": False,
        "session_id": None,
        "handoff": "",
    }

    receipt = temp_db.fetchone(
        """
        SELECT attempt_id, boundary_kind, continuation_session_id
        FROM session_handoff_deliveries
        WHERE handoff_id = %s
        """,
        (attempt.handoff_record_id,),
    )
    assert receipt is not None
    assert receipt["attempt_id"] == attempt_id
    assert receipt["boundary_kind"] == "compact"
    assert receipt["continuation_session_id"] == session.id


def test_failed_attempt_restores_handoff_and_deletes_only_staged_content(
    temp_db: HubDatabase,
    session_manager: SessionManager,
) -> None:
    session = _registered_session(session_manager)
    temp_db.execute(
        "UPDATE sessions SET handoff_markdown = %s WHERE id = %s",
        ("previous", session.id),
    )
    feedback_ids = write_feedback_batch(
        temp_db,
        session.id,
        normalize_feedback_observations([_observation()]),
    )
    handoff = build_handoff_payload(current_state="Replacement", next_steps=["Continue"])
    state = stage_handoff_attempt(
        temp_db,
        session.id,
        attempt_id="c" * 32,
        handoff=handoff,
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
    handoff_count = temp_db.fetchone(
        "SELECT COUNT(*) AS count FROM session_handoffs WHERE session_id = %s",
        (session.id,),
    )
    assert handoff_count is not None and handoff_count["count"] == 0
    feedback_count = temp_db.fetchone(
        "SELECT COUNT(*) AS count FROM session_feedback WHERE session_id = %s",
        (session.id,),
    )
    assert feedback_count is not None and feedback_count["count"] == len(feedback_ids) == 1


def test_delivery_receipt_is_idempotent_and_prevents_compensation(
    temp_db: HubDatabase,
    session_manager: SessionManager,
) -> None:
    session = _registered_session(session_manager)
    state = stage_handoff_attempt(
        temp_db,
        session.id,
        attempt_id="4" * 32,
        handoff=build_handoff_payload(current_state="Delivered.", next_steps=["Continue."]),
        clear_session=False,
    )

    assert (
        record_handoff_delivery(
            temp_db,
            handoff_id=state.handoff_record_id,
            attempt_id=state.attempt_id,
            boundary_kind="compact",
            continuation_session_id=session.id,
        )
        is True
    )
    assert (
        record_handoff_delivery(
            temp_db,
            handoff_id=state.handoff_record_id,
            attempt_id=state.attempt_id,
            boundary_kind="compact",
            continuation_session_id=session.id,
        )
        is False
    )
    assert restore_handoff_attempt(temp_db, state) is False
    row = temp_db.fetchone(
        "SELECT rendered_markdown FROM session_handoffs WHERE id = %s",
        (state.handoff_record_id,),
    )
    assert row is not None


def test_agent_end_staging_is_first_successful_payload_wins(
    temp_db: HubDatabase,
    session_manager: SessionManager,
) -> None:
    parent, child, run_id = _agent_boundary(session_manager, suffix="retry")
    first_payload = build_handoff_payload(
        current_state="Implementation complete.",
        next_steps=["Review the result."],
    )
    retry_payload = build_handoff_payload(
        current_state="A retry tried to replace the result.",
        next_steps=["Ignore the first payload."],
    )

    first = stage_agent_end_handoff(
        temp_db,
        agent_run_id=run_id,
        child_session_id=child.id,
        parent_session_id=parent.id,
        payload=first_payload,
    )
    retried = stage_agent_end_handoff(
        temp_db,
        agent_run_id=run_id,
        child_session_id=child.id,
        parent_session_id=parent.id,
        payload=retry_payload,
    )

    assert retried.id == first.id
    assert retried.payload == first_payload
    persisted = get_agent_end_handoff(temp_db, run_id)
    assert persisted is not None and persisted.id == first.id
    handoff_count = temp_db.fetchone(
        "SELECT COUNT(*) AS count FROM session_handoffs WHERE session_id = %s",
        (child.id,),
    )
    receipt_count = temp_db.fetchone(
        """
        SELECT COUNT(*) AS count
        FROM session_handoff_deliveries
        WHERE attempt_id = %s AND boundary_kind = 'agent_end'
        """,
        (run_id.replace("-", ""),),
    )
    assert handoff_count is not None and handoff_count["count"] == 1
    assert receipt_count is not None and receipt_count["count"] == 1


@pytest.mark.asyncio
async def test_targeted_agent_end_handoff_is_parent_only_and_idempotent(
    temp_db: HubDatabase,
    session_manager: SessionManager,
) -> None:
    parent, child, run_id = _agent_boundary(session_manager, suffix="targeted")
    payload = build_handoff_payload(
        current_state="Review completed.",
        next_steps=["Apply the review verdict."],
    )
    staged = stage_agent_end_handoff(
        temp_db,
        agent_run_id=run_id,
        child_session_id=child.id,
        parent_session_id=parent.id,
        payload=payload,
    )
    registry = create_session_messages_registry(session_manager=session_manager, db=temp_db)

    with session_context_for_test(parent.id):
        first = await registry.call("get_handoff", {"agent_run_id": run_id})
        second = await registry.call("get_handoff", {"agent_run_id": run_id})

    expected = {
        "success": True,
        "found": True,
        "handoff_id": staged.id,
        "agent_run_id": run_id,
        "session_id": child.id,
        "boundary_kind": "agent_end",
        "handoff": payload.rendered_markdown,
    }
    assert first == expected
    assert second == expected

    unrelated_id = session_manager.register_session(
        external_id="agent-handoff-unrelated",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=parent.project_id,
    )
    with session_context_for_test(unrelated_id):
        denied = await registry.call("get_handoff", {"agent_run_id": run_id})
    assert denied["success"] is False
    assert denied["error_code"] == "access_denied"


@pytest.mark.asyncio
async def test_targeted_agent_end_handoff_reports_unknown_and_not_authored(
    temp_db: HubDatabase,
    session_manager: SessionManager,
) -> None:
    parent, _child, run_id = _agent_boundary(session_manager, suffix="missing")
    registry = create_session_messages_registry(session_manager=session_manager, db=temp_db)

    with session_context_for_test(parent.id):
        missing = await registry.call("get_handoff", {"agent_run_id": run_id})
        unknown = await registry.call("get_handoff", {"agent_run_id": str(uuid4())})

    assert missing == {
        "success": True,
        "found": False,
        "handoff_id": None,
        "agent_run_id": run_id,
        "session_id": None,
        "boundary_kind": "agent_end",
        "handoff": "",
    }
    assert unknown["success"] is False
    assert unknown["error_code"] == "run_not_found"


@pytest.mark.asyncio
async def test_targeted_agent_end_handoffs_survive_parent_clear_and_stay_independent(
    temp_db: HubDatabase,
    session_manager: SessionManager,
) -> None:
    parent, first_child, first_run_id = _agent_boundary(session_manager, suffix="first")
    _, second_child, second_run_id = _agent_boundary(
        session_manager,
        suffix="second",
        parent=parent,
    )
    first = stage_agent_end_handoff(
        temp_db,
        agent_run_id=first_run_id,
        child_session_id=first_child.id,
        parent_session_id=parent.id,
        payload=build_handoff_payload(current_state="First done.", next_steps=["Read second."]),
    )
    second = stage_agent_end_handoff(
        temp_db,
        agent_run_id=second_run_id,
        child_session_id=second_child.id,
        parent_session_id=parent.id,
        payload=build_handoff_payload(current_state="Second done.", next_steps=["Continue."]),
    )
    successor_id = session_manager.register_session(
        external_id="agent-parent-successor",
        machine_id=MACHINE_ID,
        source="codex",
        project_id=parent.project_id,
        parent_session_id=parent.id,
    )
    clear_attempt_id = uuid4().hex
    stage_clear_attempt(
        temp_db,
        parent.id,
        attempt_id=clear_attempt_id,
        handoff=build_handoff_payload(current_state="Clearing.", next_steps=["Continue."]),
        terminal_context=None,
        chat_context=None,
    )
    assert take_clear_handoff_marker(
        temp_db,
        parent.id,
        attempt_id=clear_attempt_id,
        successor_id=successor_id,
    )
    registry = create_session_messages_registry(session_manager=session_manager, db=temp_db)

    with session_context_for_test(successor_id):
        first_result = await registry.call("get_handoff", {"agent_run_id": first_run_id})
        second_result = await registry.call("get_handoff", {"agent_run_id": second_run_id})

    assert first_result["handoff_id"] == first.id
    assert first_result["session_id"] == first_child.id
    assert second_result["handoff_id"] == second.id
    assert second_result["session_id"] == second_child.id


def test_staged_terminal_delivery_claim_requires_gate_and_is_deduplicated(
    temp_db: HubDatabase,
    session_manager: SessionManager,
) -> None:
    session = _registered_session(session_manager)
    attempt_id = "d" * 32
    handoff = build_handoff_payload(current_state="Ready.", next_steps=["Continue."])
    state = stage_handoff_attempt(
        temp_db,
        session.id,
        attempt_id=attempt_id,
        handoff=handoff,
        clear_session=False,
    )

    assert claim_staged_handoff_delivery(temp_db, session.id, attempt_id) is None
    SessionVariableManager(temp_db).merge_variables(
        session.id,
        {
            HANDOFF_DISPATCH_GATE_VARIABLE: {
                "handoff_staged": True,
                "delivery_pending": True,
                "attempt_id": attempt_id,
                "clear_session": False,
            }
        },
    )

    claimed = claim_staged_handoff_delivery(temp_db, session.id, attempt_id)

    assert claimed is not None
    assert claimed.handoff_record_id == state.handoff_record_id
    assert claimed.clear_session is False
    assert claim_staged_handoff_delivery(temp_db, session.id, attempt_id) is None
    marker = SessionVariableManager(temp_db).get_variables(session.id)[PENDING_HANDOFF_VARIABLE]
    assert marker["dispatch_started_at"]
    failure = {"delivery_failed": True, "retry_guidance": "Retry set_handoff."}
    assert restore_staged_handoff(
        temp_db,
        session.id,
        attempt_id,
        failure_result=failure,
    )
    variables = SessionVariableManager(temp_db).get_variables(session.id)
    assert PENDING_HANDOFF_VARIABLE not in variables
    assert HANDOFF_PULL_PENDING_VARIABLE not in variables
    assert variables[HANDOFF_DISPATCH_GATE_VARIABLE] == failure


def test_clear_delivery_compensation_restores_status_and_clears_markers(
    temp_db: HubDatabase,
    session_manager: SessionManager,
) -> None:
    session = _registered_session(session_manager)
    attempt_id = "e" * 32
    state = stage_clear_attempt(
        temp_db,
        session.id,
        attempt_id=attempt_id,
        handoff=build_handoff_payload(current_state="Ready.", next_steps=["Continue."]),
        terminal_context={"tmux_pane": "%1"},
        chat_context=None,
    )
    failure = {"delivery_failed": True, "retry_guidance": "Retry set_handoff."}

    assert clear_failed_attempt(
        temp_db,
        session.id,
        attempt_id=attempt_id,
        attempt_state=state,
        marker_updates={HANDOFF_DISPATCH_GATE_VARIABLE: failure},
    )

    restored = session_manager.get(session.id)
    assert restored is not None
    assert restored.status == "active"
    variables = SessionVariableManager(temp_db).get_variables(session.id)
    assert PENDING_HANDOFF_VARIABLE not in variables
    assert CLEAR_ATTEMPT_VARIABLE not in variables
    assert variables[HANDOFF_DISPATCH_GATE_VARIABLE] == failure


@pytest.mark.parametrize(
    ("assignment", "value", "constraint"),
    [
        ("payload_version = %s", 2, "session_handoffs_payload_version_valid"),
        ("current_state = %s", " ", "session_handoffs_current_state_nonblank"),
        (
            "next_steps_json = %s::jsonb",
            "[]",
            "session_handoffs_next_steps_array",
        ),
        ("notes_json = %s::jsonb", "{}", "session_handoffs_notes_array"),
        (
            "rendered_markdown = %s",
            " ",
            "session_handoffs_rendered_markdown_nonblank",
        ),
        ("content_sha256 = %s", "nope", "session_handoffs_content_sha256_valid"),
    ],
)
def test_handoff_schema_constraints_reject_malformed_rows(
    temp_db: HubDatabase,
    session_manager: SessionManager,
    assignment: str,
    value: object,
    constraint: str,
) -> None:
    session = _registered_session(session_manager)
    state = stage_handoff_attempt(
        temp_db,
        session.id,
        attempt_id="5" * 32,
        handoff=build_handoff_payload(current_state="Valid.", next_steps=["Continue."]),
        clear_session=False,
    )

    with pytest.raises(CheckViolation) as exc_info:
        temp_db.execute(
            f"UPDATE session_handoffs SET {assignment} WHERE id = %s",
            (value, state.handoff_record_id),
        )

    assert exc_info.value.diag.constraint_name == constraint


def test_delivery_schema_constraints_boundary_and_attempt_id(
    temp_db: HubDatabase,
    session_manager: SessionManager,
) -> None:
    session = _registered_session(session_manager)
    state = stage_handoff_attempt(
        temp_db,
        session.id,
        attempt_id="6" * 32,
        handoff=build_handoff_payload(current_state="Valid.", next_steps=["Continue."]),
        clear_session=False,
    )
    assert record_handoff_delivery(
        temp_db,
        handoff_id=state.handoff_record_id,
        attempt_id=state.attempt_id,
        boundary_kind="compact",
        continuation_session_id=str(uuid4()),
    )

    with pytest.raises(CheckViolation) as boundary_error:
        temp_db.execute(
            "UPDATE session_handoff_deliveries SET boundary_kind = 'other' WHERE handoff_id = %s",
            (state.handoff_record_id,),
        )
    assert (
        boundary_error.value.diag.constraint_name
        == "session_handoff_deliveries_boundary_kind_valid"
    )

    with pytest.raises(CheckViolation) as attempt_error:
        temp_db.execute(
            "UPDATE session_handoff_deliveries SET attempt_id = 'invalid' WHERE handoff_id = %s",
            (state.handoff_record_id,),
        )
    assert attempt_error.value.diag.constraint_name == "session_handoff_deliveries_attempt_id_valid"


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
    properties = schema.input_schema["properties"]
    assert "gobby_feedback" not in properties
    assert tuple(properties) == (
        "current_state",
        "next_steps",
        "what_was_accomplished",
        "key_decisions",
        "problems_encountered",
        "what_didnt_work",
        "blockers",
        "notes",
        "references",
        "clear_session",
    )
    assert properties["current_state"]["minLength"] == 1
    assert properties["next_steps"]["minItems"] == 1
    assert properties["next_steps"]["items"]["minLength"] == 1
    get_schema = registry.get_tool_metadata("get_handoff")
    assert get_schema is not None
    assert get_schema.input_schema == {
        "type": "object",
        "properties": {"agent_run_id": {"type": "string"}},
        "additionalProperties": False,
    }
    feedback_schema = registry.get_tool_metadata("feedback")
    assert feedback_schema is not None
    disposition_description = feedback_schema.input_schema["properties"]["observations"]["items"][
        "properties"
    ]["disposition"]["description"]
    assert (
        "claimed or closed by this session or by a spawned descendant session"
        in disposition_description
    )
    assert (
        "gobby-<server>:<tool>"
        in FEEDBACK_OBSERVATION_INPUT_SCHEMA["properties"]["source"]["description"]
    )

    with session_context_for_test(session.id):
        assert await registry.call("feedback", {"observations": []}) == {
            "success": True,
            "created": 0,
            "feedback_ids": [],
        }
        empty = await registry.call("get_handoff", {})
        assert empty == {
            "success": True,
            "found": False,
            "session_id": None,
            "handoff": "",
        }
        renamed = await registry.call("set_title", {"title": "Manual title"})
        assert renamed["title"] == "Manual title"


@pytest.mark.asyncio
async def test_feedback_tool_rejects_non_gobby_source(
    temp_db: HubDatabase,
    session_manager: SessionManager,
) -> None:
    session = _registered_session(session_manager)
    registry = create_session_messages_registry(session_manager=session_manager, db=temp_db)

    with session_context_for_test(session.id):
        result = await registry.call(
            "feedback", {"observations": [_observation(source="close_task")]}
        )

    assert result["success"] is False
    assert result["error_code"] == "invalid_feedback"
    assert "must name a Gobby surface" in result["error"]


@pytest.mark.asyncio
async def test_get_handoff_result_stays_below_offload_threshold(
    temp_db: HubDatabase,
    session_manager: SessionManager,
) -> None:
    session = _registered_session(session_manager)
    handoff = build_handoff_payload(current_state="H" * 8_000, next_steps=["Continue"])
    stage_handoff_attempt(
        temp_db,
        session.id,
        attempt_id="d" * 32,
        handoff=handoff,
        clear_session=False,
    )
    registry = create_session_messages_registry(session_manager=session_manager, db=temp_db)

    with session_context_for_test(session.id):
        result = await registry.call("get_handoff", {})

    assert result == {
        "success": True,
        "found": True,
        "session_id": session.id,
        "handoff": handoff.rendered_markdown,
    }
    assert len(json.dumps(result)) < 15_000


def test_title_lifecycle_is_provisional_task_manual_and_clear_sticky(
    session_manager: SessionManager,
) -> None:
    session = _registered_session(session_manager)
    assert session.seq_num is not None
    assert session.title == f"handoff-test#{session.seq_num}: Codex"

    update_title_for_claim(
        session_manager,
        session.id,
        SimpleNamespace(seq_num=42, title="Implement handoffs"),
    )
    assert _title(session_manager, session.id) == (
        f"handoff-test#{session.seq_num}: Task #42 - Implement handoffs"
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

    assert title == (f"handoff-test#99: Task #{task.seq_num} - Continue claimed work")
    assert title_source == "task"


def test_staged_handoff_rejection_names_the_blocking_condition(
    temp_db: HubDatabase,
    session_manager: SessionManager,
) -> None:
    session = _registered_session(session_manager)
    attempt_id = "e" * 32
    handoff = build_handoff_payload(current_state="Ready.", next_steps=["Continue."])
    stage_handoff_attempt(
        temp_db,
        session.id,
        attempt_id=attempt_id,
        handoff=handoff,
        clear_session=False,
    )
    manager = SessionVariableManager(temp_db)

    assert staged_handoff_rejection({}, attempt_id) == f"no {PENDING_HANDOFF_VARIABLE} marker"
    assert staged_handoff_rejection(manager.get_variables(session.id), attempt_id) == (
        f"{HANDOFF_DISPATCH_GATE_VARIABLE} gate is not armed"
    )

    manager.merge_variables(
        session.id,
        {
            HANDOFF_DISPATCH_GATE_VARIABLE: {
                "handoff_staged": True,
                "delivery_pending": True,
                "attempt_id": attempt_id,
                "clear_session": False,
            }
        },
    )

    assert staged_handoff_rejection(manager.get_variables(session.id), attempt_id) is None
    assert staged_handoff_rejection(manager.get_variables(session.id), "f" * 32) == (
        f"{PENDING_HANDOFF_VARIABLE} holds attempt {attempt_id!r}"
    )
    assert claim_staged_handoff_delivery(temp_db, session.id, attempt_id) is not None
    rejection = staged_handoff_rejection(manager.get_variables(session.id), attempt_id)
    assert rejection is not None
    assert rejection.startswith("dispatch already started at ")
