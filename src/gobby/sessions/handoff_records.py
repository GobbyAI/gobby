"""Immutable structured handoff content and delivery receipts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from gobby.storage.hub.protocol import HubDatabase
from gobby.utils.datetime import utc_now

HANDOFF_PAYLOAD_VERSION = 1
HandoffBoundaryKind = Literal["compact", "clear", "agent_end"]


@dataclass(frozen=True, slots=True)
class HandoffPayload:
    """Normalized v1 handoff fields plus their canonical Markdown."""

    current_state: str
    next_steps: tuple[str, ...]
    what_was_accomplished: tuple[str, ...]
    key_decisions: tuple[str, ...]
    problems_encountered: tuple[str, ...]
    what_didnt_work: tuple[str, ...]
    blockers: tuple[str, ...]
    notes: tuple[str, ...]
    references: tuple[str, ...]
    rendered_markdown: str
    content_sha256: str
    payload_version: int = HANDOFF_PAYLOAD_VERSION


@dataclass(frozen=True, slots=True)
class DeliveredHandoff:
    """A validated handoff joined to its successful delivery fact."""

    id: str
    session_id: str
    payload: HandoffPayload
    authored_at: datetime
    attempt_id: str
    continuation_session_id: str
    delivered_at: datetime


def build_handoff_payload(
    *,
    current_state: str,
    next_steps: Sequence[str],
    what_was_accomplished: Sequence[str] = (),
    key_decisions: Sequence[str] = (),
    problems_encountered: Sequence[str] = (),
    what_didnt_work: Sequence[str] = (),
    blockers: Sequence[str] = (),
    notes: Sequence[str] = (),
    references: Sequence[str] = (),
) -> HandoffPayload:
    """Normalize and render one canonical v1 handoff payload."""
    state = _nonblank(current_state, "current_state")
    normalized_next_steps = tuple(_nonblank_list(next_steps, "next_steps", required=True))
    normalized_accomplished = tuple(_nonblank_list(what_was_accomplished, "what_was_accomplished"))
    normalized_decisions = tuple(_nonblank_list(key_decisions, "key_decisions"))
    normalized_problems = tuple(_nonblank_list(problems_encountered, "problems_encountered"))
    normalized_failed = tuple(_nonblank_list(what_didnt_work, "what_didnt_work"))
    normalized_blockers = tuple(_nonblank_list(blockers, "blockers"))
    normalized_notes = tuple(_nonblank_list(notes, "notes"))
    normalized_references = tuple(dict.fromkeys(_nonblank_list(references, "references")))

    sections: list[str] = ["## Current State", "", state, "", "## Next Steps", ""]
    sections.extend(f"{index}. {step}" for index, step in enumerate(normalized_next_steps, 1))
    optional_sections = (
        ("What Was Accomplished", normalized_accomplished),
        ("Key Decisions", normalized_decisions),
        ("Problems Encountered", normalized_problems),
        ("What Didn’t Work", normalized_failed),
        ("Blockers", normalized_blockers),
        ("Notes", normalized_notes),
        ("References", normalized_references),
    )
    for heading, entries in optional_sections:
        if entries:
            sections.extend(("", f"## {heading}", "", *(f"- {entry}" for entry in entries)))
    markdown = "\n".join(sections).strip()
    return HandoffPayload(
        current_state=state,
        next_steps=normalized_next_steps,
        what_was_accomplished=normalized_accomplished,
        key_decisions=normalized_decisions,
        problems_encountered=normalized_problems,
        what_didnt_work=normalized_failed,
        blockers=normalized_blockers,
        notes=normalized_notes,
        references=normalized_references,
        rendered_markdown=markdown,
        content_sha256=hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
    )


def insert_handoff_record(
    conn: Any,
    session_id: str,
    payload: HandoffPayload,
    *,
    authored_at: datetime | None = None,
) -> tuple[str, datetime]:
    """Insert one authored-content row inside the caller's transaction."""
    handoff_id = str(uuid4())
    authored = authored_at or utc_now()
    conn.execute(
        """
        INSERT INTO session_handoffs (
            id, session_id, payload_version, current_state, next_steps_json,
            what_was_accomplished_json, key_decisions_json, problems_encountered_json,
            what_didnt_work_json, blockers_json, notes_json, references_json,
            rendered_markdown, content_sha256, authored_at
        )
        VALUES (
            %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb,
            %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s
        )
        """,
        (
            handoff_id,
            session_id,
            payload.payload_version,
            payload.current_state,
            json.dumps(payload.next_steps),
            json.dumps(payload.what_was_accomplished),
            json.dumps(payload.key_decisions),
            json.dumps(payload.problems_encountered),
            json.dumps(payload.what_didnt_work),
            json.dumps(payload.blockers),
            json.dumps(payload.notes),
            json.dumps(payload.references),
            payload.rendered_markdown,
            payload.content_sha256,
            authored,
        ),
    )
    return handoff_id, authored


def insert_delivery_receipt(
    conn: Any,
    *,
    handoff_id: str,
    attempt_id: str,
    boundary_kind: HandoffBoundaryKind,
    continuation_session_id: str,
) -> bool:
    """Insert one receipt idempotently, rejecting conflicting retries."""
    inserted = conn.execute(
        """
        INSERT INTO session_handoff_deliveries (
            handoff_id, attempt_id, boundary_kind, continuation_session_id
        )
        VALUES (%s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        RETURNING handoff_id
        """,
        (handoff_id, attempt_id, boundary_kind, continuation_session_id),
    ).fetchone()
    if inserted is not None:
        return True
    existing = conn.execute(
        """
        SELECT handoff_id, attempt_id, boundary_kind, continuation_session_id
        FROM session_handoff_deliveries
        WHERE handoff_id = %s OR attempt_id = %s
        """,
        (handoff_id, attempt_id),
    ).fetchone()
    expected = (handoff_id, attempt_id, boundary_kind, continuation_session_id)
    actual = (
        (
            str(existing["handoff_id"]),
            str(existing["attempt_id"]),
            str(existing["boundary_kind"]),
            str(existing["continuation_session_id"]),
        )
        if existing is not None
        else None
    )
    if actual != expected:
        raise ValueError("handoff delivery receipt conflicts with the staged attempt")
    return False


def record_handoff_delivery(
    db: HubDatabase,
    *,
    handoff_id: str,
    attempt_id: str,
    boundary_kind: HandoffBoundaryKind,
    continuation_session_id: str,
) -> bool:
    """Record a successful boundary in its own transaction."""
    with db.transaction() as conn:
        return insert_delivery_receipt(
            conn,
            handoff_id=handoff_id,
            attempt_id=attempt_id,
            boundary_kind=boundary_kind,
            continuation_session_id=continuation_session_id,
        )


def agent_run_attempt_id(agent_run_id: str) -> str:
    """Return the stable delivery-attempt identity for one agent run."""
    try:
        return UUID(agent_run_id).hex
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("agent_run_id must be a UUID") from exc


def get_agent_end_handoff(
    db: HubDatabase,
    agent_run_id: str,
) -> DeliveredHandoff | None:
    """Read an agent run's immutable final handoff without consuming it."""
    attempt_id = agent_run_attempt_id(agent_run_id)
    row = db.fetchone(
        """
        SELECT
            h.*,
            d.attempt_id,
            d.continuation_session_id,
            d.delivered_at
        FROM session_handoffs AS h
        JOIN session_handoff_deliveries AS d
            ON d.handoff_id = h.id
        WHERE d.attempt_id = %s
          AND d.boundary_kind = 'agent_end'
        LIMIT 1
        """,
        (attempt_id,),
    )
    return _delivered_handoff_from_row(row)


def stage_agent_end_handoff(
    db: HubDatabase,
    *,
    agent_run_id: str,
    child_session_id: str,
    parent_session_id: str,
    payload: HandoffPayload,
) -> DeliveredHandoff:
    """Persist an agent-end handoff once; retries reuse the winning payload."""
    attempt_id = agent_run_attempt_id(agent_run_id)
    existing = get_agent_end_handoff(db, agent_run_id)
    if existing is not None:
        return _validate_agent_end_target(existing, child_session_id, parent_session_id)

    try:
        with db.transaction() as conn:
            handoff_id, _authored_at = insert_handoff_record(conn, child_session_id, payload)
            insert_delivery_receipt(
                conn,
                handoff_id=handoff_id,
                attempt_id=attempt_id,
                boundary_kind="agent_end",
                continuation_session_id=parent_session_id,
            )
            row = conn.execute(
                """
                SELECT
                    h.*,
                    d.attempt_id,
                    d.continuation_session_id,
                    d.delivered_at
                FROM session_handoffs AS h
                JOIN session_handoff_deliveries AS d
                    ON d.handoff_id = h.id
                WHERE d.attempt_id = %s
                  AND d.boundary_kind = 'agent_end'
                LIMIT 1
                """,
                (attempt_id,),
            ).fetchone()
            staged = _delivered_handoff_from_row(row)
            if staged is None:
                raise RuntimeError("agent-end handoff could not be read after staging")
            return _validate_agent_end_target(staged, child_session_id, parent_session_id)
    except ValueError as exc:
        if "delivery receipt conflicts" not in str(exc):
            raise
        winner = get_agent_end_handoff(db, agent_run_id)
        if winner is None:
            raise
        return _validate_agent_end_target(winner, child_session_id, parent_session_id)


def delete_undelivered_handoff(conn: Any, handoff_id: str, session_id: str) -> bool:
    """Delete staged content only while it has no successful receipt."""
    deleted = conn.execute(
        """
        DELETE FROM session_handoffs AS h
        WHERE h.id = %s
          AND h.session_id = %s
          AND NOT EXISTS (
              SELECT 1 FROM session_handoff_deliveries AS d WHERE d.handoff_id = h.id
          )
        RETURNING h.id
        """,
        (handoff_id, session_id),
    ).fetchone()
    return deleted is not None


def latest_delivered_clear_handoff(
    db: HubDatabase,
    session_id: str,
) -> DeliveredHandoff | None:
    """Return the newest valid clear-delivered handoff for a session."""
    row = db.fetchone(
        """
        SELECT
            h.*,
            d.attempt_id,
            d.continuation_session_id,
            d.delivered_at
        FROM session_handoffs AS h
        JOIN session_handoff_deliveries AS d
            ON d.handoff_id = h.id
        WHERE h.session_id = %s
          AND d.boundary_kind = 'clear'
        ORDER BY d.delivered_at DESC, h.id DESC
        LIMIT 1
        """,
        (session_id,),
    )
    return _delivered_handoff_from_row(row)


def handoff_summary_source_hash(handoff: DeliveredHandoff) -> str:
    payload = f"session-handoff-summary-v1\0{handoff.id}\0{handoff.payload.content_sha256}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _delivered_handoff_from_row(row: Mapping[str, Any] | None) -> DeliveredHandoff | None:
    if row is None:
        return None
    payload = _payload_from_row(row)
    if payload is None:
        return None
    authored_at = row["authored_at"]
    delivered_at = row["delivered_at"]
    if not isinstance(authored_at, datetime) or not isinstance(delivered_at, datetime):
        return None
    return DeliveredHandoff(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        payload=payload,
        authored_at=authored_at,
        attempt_id=str(row["attempt_id"]),
        continuation_session_id=str(row["continuation_session_id"]),
        delivered_at=delivered_at,
    )


def _validate_agent_end_target(
    handoff: DeliveredHandoff,
    child_session_id: str,
    parent_session_id: str,
) -> DeliveredHandoff:
    if (
        handoff.session_id != child_session_id
        or handoff.continuation_session_id != parent_session_id
    ):
        raise ValueError("agent-end handoff conflicts with the agent run boundary")
    return handoff


def _payload_from_row(row: Mapping[str, Any]) -> HandoffPayload | None:
    if row.get("payload_version") != HANDOFF_PAYLOAD_VERSION:
        return None
    try:
        payload = build_handoff_payload(
            current_state=row["current_state"],
            next_steps=_json_list(row["next_steps_json"]),
            what_was_accomplished=_json_list(row["what_was_accomplished_json"]),
            key_decisions=_json_list(row["key_decisions_json"]),
            problems_encountered=_json_list(row["problems_encountered_json"]),
            what_didnt_work=_json_list(row["what_didnt_work_json"]),
            blockers=_json_list(row["blockers_json"]),
            notes=_json_list(row["notes_json"]),
            references=_json_list(row["references_json"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    rendered = row.get("rendered_markdown")
    digest = row.get("content_sha256")
    if not isinstance(rendered, str) or not isinstance(digest, str):
        return None
    recomputed = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    if (
        rendered != payload.rendered_markdown
        or digest != recomputed
        or digest != payload.content_sha256
    ):
        return None
    return payload


def _json_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        raise TypeError("handoff JSON field must be an array")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError("handoff JSON items must be nonblank strings")
    return value


def _nonblank(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonblank string")
    return value.strip()


def _nonblank_list(
    values: Sequence[str] | None,
    field: str,
    *,
    required: bool = False,
) -> list[str]:
    if values is None:
        values = ()
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{field} must be a list of nonblank strings")
    normalized = [_nonblank(value, f"{field}[{index}]") for index, value in enumerate(values)]
    if required and not normalized:
        raise ValueError(f"{field} must contain at least one nonblank string")
    return normalized
