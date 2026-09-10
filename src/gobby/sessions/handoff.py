"""Structured handoff rendering, feedback persistence, and one-shot delivery."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Never
from uuid import uuid4

from gobby.sessions.handoff_records import (
    HandoffPayload,
    build_handoff_payload,
    delete_undelivered_handoff,
    insert_delivery_receipt,
    insert_handoff_record,
)
from gobby.storage.hub.protocol import HubDatabase
from gobby.tasks.state_semantics import get_claimed_session_id
from gobby.utils.datetime import utc_now

if TYPE_CHECKING:
    from gobby.storage.tasks import Task

PENDING_HANDOFF_VARIABLE = "set_handoff_pending"
HANDOFF_PULL_PENDING_VARIABLE = "handoff_pull_pending"
HANDOFF_DISPATCH_GATE_VARIABLE = "context_compact_handoff_result"

_OPTIONAL_FEEDBACK_FIELDS = ("suggestion", "disposition")

FEEDBACK_KINDS = ("friction", "bug", "noise", "surprise", "missing-affordance", "useful", "other")
FEEDBACK_FREQUENCIES = ("once", "repeated", "always")
FEEDBACK_DISPOSITIONS = ("worked-around", "filed-task", "fixed", "escalated", "noted")
FEEDBACK_SOURCE_SURFACES = (
    "rule",
    "hook",
    "skill",
    "workflow",
    "agent",
    "pipeline",
    "prompt",
    "cli",
    "binary",
    "daemon",
    "ui",
    "docs",
    "config",
)
FEEDBACK_TASK_REF_RE = re.compile(r"#(\d+)")
_FEEDBACK_MCP_SOURCE_RE = re.compile(r"^gobby-[a-z0-9-]+:[a-z_][a-z0-9_]*$")
_FEEDBACK_REPOSITORY_PREFIXES = ("src/gobby/", "crates/", "web/src/", "docs/")
_FEEDBACK_SESSION_REF_RE = re.compile(
    r"(?:\b[\w.-]+-S#\d+\b|(?<![\w#])#\d+\b|"
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b)"
)


def build_handoff_continue_prompt() -> str:
    """Return the pull-only continuation directive used after compact and clear."""
    return "Call `get_handoff()` on `gobby-sessions`, follow the returned handoff, then continue."


@dataclass(frozen=True, slots=True)
class FeedbackObservation:
    source: str
    kind: str
    evidence: str
    impact: str
    frequency: str
    suggestion: str | None = None
    disposition: str | None = None
    kind_other_label: str | None = None


@dataclass(frozen=True, slots=True)
class HandoffAttemptState:
    session_id: str
    attempt_id: str
    handoff_record_id: str
    prior_handoff_markdown: str | None
    prior_markers: dict[str, Any]
    missing_markers: frozenset[str]
    # Status the row held before a clear attempt moved it to awaiting_handoff;
    # None when staging did not transition the row.
    prior_status: str | None = None


@dataclass(frozen=True, slots=True)
class ClaimedHandoffDelivery:
    """Canonical staged handoff claimed for one terminal dispatch."""

    session_id: str
    attempt_id: str
    handoff_record_id: str
    clear_session: bool


@dataclass(frozen=True, slots=True)
class ConsumedHandoff:
    session_id: str
    handoff_id: str
    attempt_id: str
    markdown: str


def render_handoff_markdown(
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
) -> str:
    """Validate and deterministically render the public handoff fields."""
    return build_handoff_payload(
        current_state=current_state,
        next_steps=next_steps,
        what_was_accomplished=what_was_accomplished,
        key_decisions=key_decisions,
        problems_encountered=problems_encountered,
        what_didnt_work=what_didnt_work,
        blockers=blockers,
        notes=notes,
        references=references,
    ).rendered_markdown


def normalize_feedback_observations(
    observations: Sequence[Mapping[str, Any]] | None,
    *,
    resolve_task: Callable[[str], Task | None] | None = None,
    session_id: str | None = None,
    descendant_session_ids: Collection[str] = (),
) -> list[FeedbackObservation]:
    """Validate feedback input without mutating storage."""
    fixed_owner_session_ids = set(descendant_session_ids)
    if session_id is not None:
        fixed_owner_session_ids.add(session_id)
    normalized: list[FeedbackObservation] = []
    for index, raw in enumerate(observations or ()):
        if not isinstance(raw, Mapping):
            raise ValueError(f"observations[{index}] must be an object")
        optional: dict[str, str | None] = {}
        for field in _OPTIONAL_FEEDBACK_FIELDS:
            value = raw.get(field)
            optional[field] = (
                None if value is None else _nonblank(value, f"observations[{index}].{field}")
            )
        kind = _nonblank(raw.get("kind"), f"observations[{index}].kind")
        if kind not in FEEDBACK_KINDS:
            raise ValueError(
                f"observations[{index}].kind must be one of {', '.join(FEEDBACK_KINDS)}"
            )
        frequency = _nonblank(raw.get("frequency"), f"observations[{index}].frequency")
        if frequency not in FEEDBACK_FREQUENCIES:
            raise ValueError(
                f"observations[{index}].frequency must be one of {', '.join(FEEDBACK_FREQUENCIES)}"
            )
        if optional["disposition"] is not None and optional["disposition"] not in (
            FEEDBACK_DISPOSITIONS
        ):
            raise ValueError(
                f"observations[{index}].disposition must be one of "
                f"{', '.join(FEEDBACK_DISPOSITIONS)}"
            )
        evidence = _nonblank(raw.get("evidence"), f"observations[{index}].evidence")
        disposition = optional["disposition"]
        task_match = FEEDBACK_TASK_REF_RE.search(evidence)
        if disposition in {"filed-task", "fixed"} and task_match is None:
            _raise_disposition_error(
                index,
                f"'{disposition}' requires a #N task ref in evidence",
            )
        if disposition == "escalated" and _FEEDBACK_SESSION_REF_RE.search(evidence) is None:
            _raise_disposition_error(
                index,
                "'escalated' requires the active owner session ref "
                "(#N, UUID, or <project>-S#N) in evidence",
            )
        if disposition in {"filed-task", "fixed"} and resolve_task is not None:
            assert task_match is not None
            task = resolve_task(task_match.group(0))
            if task is None or session_id is None:
                _raise_disposition_error(index, f"{task_match.group(0)} could not be resolved")
            if disposition == "filed-task":
                labels = set(task.labels or ())
                if task.created_in_session_id != session_id or not labels.intersection(
                    {"needs-decision", "clean-window"}
                ):
                    _raise_disposition_error(
                        index,
                        "'filed-task' is rung 3 only: the referenced task must be created "
                        "by this session and labeled needs-decision or clean-window",
                    )
            elif (
                get_claimed_session_id(task) not in fixed_owner_session_ids
                and task.closed_in_session_id not in fixed_owner_session_ids
            ):
                _raise_disposition_error(
                    index,
                    "'fixed' requires a task claimed or closed by this session or by a "
                    "spawned descendant session",
                )
        source = validate_feedback_source(
            _nonblank(raw.get("source"), f"observations[{index}].source"),
            field=f"observations[{index}].source",
        )
        normalized.append(
            FeedbackObservation(
                source=source,
                kind=kind,
                evidence=evidence,
                impact=_nonblank(raw.get("impact"), f"observations[{index}].impact"),
                frequency=frequency,
                suggestion=optional["suggestion"],
                disposition=disposition,
                kind_other_label=_normalize_other_label(raw.get("kind_other_label"), kind, index),
            )
        )
    return normalized


def validate_feedback_source(source: str, *, field: str = "source") -> str:
    """Validate that feedback names an unambiguous Gobby-owned surface."""
    if _FEEDBACK_MCP_SOURCE_RE.fullmatch(source):
        return source
    surface, separator, name = source.partition(":")
    if separator and surface in FEEDBACK_SOURCE_SURFACES and bool(name.strip()):
        return source
    if any(
        source.startswith(prefix) and len(source) > len(prefix)
        for prefix in _FEEDBACK_REPOSITORY_PREFIXES
    ):
        return source
    surfaces = ", ".join(FEEDBACK_SOURCE_SURFACES)
    raise ValueError(
        f"{field} must name a Gobby surface: gobby-<server>:<tool>; "
        f"<surface>:<name> where surface is one of {surfaces}; or a repository path "
        "starting with src/gobby/, crates/, web/src/, or docs/"
    )


def _raise_disposition_error(index: int, detail: str) -> Never:
    raise ValueError(
        f"observations[{index}].disposition: Found-work ladder: {detail}. "
        "Use 'fixed' for a referenced task this session claimed or closed; "
        "use 'escalated' after send_message to a referenced active owner session; "
        "use 'filed-task' only for a referenced rung-3 task carrying "
        "needs-decision or clean-window."
    )


def _normalize_other_label(value: Any, kind: str, index: int) -> str | None:
    """Enforce the strict `other` gate: label present iff kind is `other`."""
    if kind != "other":
        if value is not None:
            raise ValueError(
                f"observations[{index}].kind_other_label is only allowed when kind is 'other'"
            )
        return None
    label = _nonblank(value, f"observations[{index}].kind_other_label (required for kind 'other')")
    slug = label.lower().replace("_", "-").replace(" ", "-")
    if slug in FEEDBACK_KINDS:
        raise ValueError(
            f"observations[{index}].kind_other_label restates the '{slug}' kind; use it directly"
        )
    return label


def write_feedback_batch(
    db: HubDatabase,
    session_id: str,
    observations: Sequence[FeedbackObservation],
) -> list[str]:
    """Write one feedback row per observation in one transaction."""
    if not observations:
        return []
    with db.transaction() as conn:
        return _insert_feedback_rows(conn, session_id, observations)


def stage_handoff_attempt(
    db: HubDatabase,
    session_id: str,
    *,
    attempt_id: str,
    handoff: HandoffPayload,
    clear_session: bool,
    additional_markers: Mapping[str, Any] | None = None,
    transition_status: str | None = None,
) -> HandoffAttemptState:
    """Atomically stage authored content, handoff Markdown, and delivery markers.

    ``transition_status`` moves an ``active``/``paused`` row to that status inside
    the staging transaction (clear attempts use ``awaiting_handoff`` so startup
    expiry and SessionEnd leave the row alone until its successor binds); the
    prior status is recorded on the attempt markers and in the returned state.
    """
    marker_updates = dict(additional_markers or {})
    marker_updates[PENDING_HANDOFF_VARIABLE] = {
        "attempt_id": attempt_id,
        "clear_session": clear_session,
        "created_at": utc_now().isoformat(),
    }
    if not clear_session:
        marker_updates[HANDOFF_PULL_PENDING_VARIABLE] = True
    with db.transaction() as conn:
        session_row = conn.execute(
            "SELECT handoff_markdown, status, machine_id FROM sessions WHERE id = %s FOR UPDATE",
            (session_id,),
        ).fetchone()
        if session_row is None:
            raise ValueError(f"Session {session_id} not found")
        from gobby.sessions.handoff_shutdown import lock_handoff_staging

        lock_handoff_staging(conn, str(session_row["machine_id"]))
        prior_status: str | None = None
        if transition_status is not None and session_row["status"] in ("active", "paused"):
            prior_status = str(session_row["status"])
        variable_row = conn.execute(
            "SELECT variables FROM session_variables WHERE session_id = %s FOR UPDATE",
            (session_id,),
        ).fetchone()
        variables = _load_variables(variable_row["variables"] if variable_row else None)
        prior_markers = {name: variables[name] for name in marker_updates if name in variables}
        missing_markers = frozenset(name for name in marker_updates if name not in variables)
        handoff_record_id, _authored_at = insert_handoff_record(conn, session_id, handoff)
        for name, value in marker_updates.items():
            if isinstance(value, dict) and value.get("attempt_id") == attempt_id:
                marker_updates[name] = {
                    **value,
                    "handoff_record_id": handoff_record_id,
                    "prior_handoff_markdown": session_row["handoff_markdown"],
                    "prior_status": prior_status,
                }
        variables.update(marker_updates)

        if prior_status is not None:
            conn.execute(
                "UPDATE sessions SET handoff_markdown = %s, status = %s, updated_at = %s "
                "WHERE id = %s",
                (handoff.rendered_markdown, transition_status, utc_now(), session_id),
            )
        else:
            conn.execute(
                "UPDATE sessions SET handoff_markdown = %s, updated_at = %s WHERE id = %s",
                (handoff.rendered_markdown, utc_now(), session_id),
            )
        _store_variables(conn, session_id, variables, exists=variable_row is not None)

    return HandoffAttemptState(
        session_id=session_id,
        attempt_id=attempt_id,
        handoff_record_id=handoff_record_id,
        prior_handoff_markdown=session_row["handoff_markdown"],
        prior_markers=prior_markers,
        missing_markers=missing_markers,
        prior_status=prior_status,
    )


def restore_handoff_attempt(
    db: HubDatabase,
    state: HandoffAttemptState,
    *,
    marker_updates: Mapping[str, Any] | None = None,
) -> bool:
    """Compensate a failed provider dispatch without disturbing newer markers."""
    with db.transaction() as conn:
        variable_row = conn.execute(
            "SELECT variables FROM session_variables WHERE session_id = %s FOR UPDATE",
            (state.session_id,),
        ).fetchone()
        if variable_row is None:
            return False
        variables = _load_variables(variable_row["variables"])
        pending = variables.get(PENDING_HANDOFF_VARIABLE)
        if (
            not isinstance(pending, Mapping)
            or pending.get("attempt_id") != state.attempt_id
            or pending.get("handoff_record_id") != state.handoff_record_id
        ):
            return False
        if not delete_undelivered_handoff(
            conn,
            state.handoff_record_id,
            state.session_id,
        ):
            return False

        for name in state.missing_markers:
            current = variables.get(name)
            if not isinstance(current, Mapping) or current.get("attempt_id") == state.attempt_id:
                variables.pop(name, None)
        variables.update(state.prior_markers)
        variables.update(marker_updates or {})
        conn.execute(
            "UPDATE sessions SET handoff_markdown = %s, updated_at = %s WHERE id = %s",
            (state.prior_handoff_markdown, utc_now(), state.session_id),
        )
        if state.prior_status is not None:
            # Only undo the staging transition; a successor bind has already
            # moved the row on and must not be reverted.
            conn.execute(
                "UPDATE sessions SET status = %s, updated_at = %s "
                "WHERE id = %s AND status = 'awaiting_handoff'",
                (state.prior_status, utc_now(), state.session_id),
            )
        _store_variables(conn, state.session_id, variables, exists=True)
    return True


def staged_handoff_tool_result(
    *,
    attempt_id: str,
    session_id: str,
    clear_session: bool,
    command: str,
    cli: str | None,
    via: str,
) -> dict[str, Any]:
    """Build the ``set_handoff`` result for a staged terminal delivery.

    The MCP proxy strips the top-level ``success`` key before the CLI and the
    tool-completion hook see this dict, so every reader keys on
    ``handoff_staged`` and ``delivery_pending`` instead (#21713).
    """
    return {
        "success": True,
        "handoff_staged": True,
        "delivery_pending": True,
        "attempt_id": attempt_id,
        "session_id": session_id,
        "clear_session": clear_session,
        "command": command,
        "cli": cli,
        "via": via,
    }


def staged_handoff_rejection(variables: Mapping[str, Any], attempt_id: str) -> str | None:
    """Explain why ``attempt_id`` cannot be claimed for delivery; ``None`` when it can."""
    marker = variables.get(PENDING_HANDOFF_VARIABLE)
    if not isinstance(marker, Mapping):
        return f"no {PENDING_HANDOFF_VARIABLE} marker"
    if marker.get("attempt_id") != attempt_id:
        return f"{PENDING_HANDOFF_VARIABLE} holds attempt {marker.get('attempt_id')!r}"
    if marker.get("dispatch_started_at") is not None:
        return f"dispatch already started at {marker['dispatch_started_at']}"
    clear_session = marker.get("clear_session")
    if not isinstance(clear_session, bool):
        return f"{PENDING_HANDOFF_VARIABLE} clear_session is not a bool"
    handoff_record_id = marker.get("handoff_record_id")
    if not isinstance(handoff_record_id, str) or not handoff_record_id:
        return f"{PENDING_HANDOFF_VARIABLE} has no handoff_record_id"
    gate = variables.get(HANDOFF_DISPATCH_GATE_VARIABLE)
    if not isinstance(gate, Mapping):
        return f"{HANDOFF_DISPATCH_GATE_VARIABLE} gate is not armed"
    if gate.get("handoff_staged") is not True or gate.get("delivery_pending") is not True:
        return f"{HANDOFF_DISPATCH_GATE_VARIABLE} gate is not delivery_pending"
    if gate.get("attempt_id") != attempt_id:
        return f"{HANDOFF_DISPATCH_GATE_VARIABLE} gate holds attempt {gate.get('attempt_id')!r}"
    if gate.get("clear_session") is not clear_session:
        return f"{HANDOFF_DISPATCH_GATE_VARIABLE} gate clear_session disagrees with the marker"
    return None


def claim_staged_handoff_delivery(
    db: HubDatabase,
    session_id: str,
    attempt_id: str,
) -> ClaimedHandoffDelivery | None:
    """Claim a staged terminal handoff after its successful tool result was persisted."""
    with db.transaction() as conn:
        variable_row = conn.execute(
            "SELECT variables FROM session_variables WHERE session_id = %s FOR UPDATE",
            (session_id,),
        ).fetchone()
        if variable_row is None:
            return None
        variables = _load_variables(variable_row["variables"])
        if staged_handoff_rejection(variables, attempt_id) is not None:
            return None
        marker = variables[PENDING_HANDOFF_VARIABLE]
        variables[PENDING_HANDOFF_VARIABLE] = {
            **marker,
            "dispatch_started_at": utc_now().isoformat(),
        }
        _store_variables(conn, session_id, variables, exists=True)
    return ClaimedHandoffDelivery(
        session_id=session_id,
        attempt_id=attempt_id,
        handoff_record_id=marker["handoff_record_id"],
        clear_session=marker["clear_session"],
    )


def restore_staged_handoff(
    db: HubDatabase,
    session_id: str,
    attempt_id: str,
    *,
    failure_result: Mapping[str, Any] | None = None,
) -> bool:
    """Restore an attempt later using compensation data stored in its pending marker."""
    row = db.fetchone(
        "SELECT variables FROM session_variables WHERE session_id = %s",
        (session_id,),
    )
    variables = _load_variables(row["variables"] if row else None)
    marker = variables.get(PENDING_HANDOFF_VARIABLE)
    if not isinstance(marker, Mapping) or marker.get("attempt_id") != attempt_id:
        return False
    handoff_record_id = marker.get("handoff_record_id")
    if not isinstance(handoff_record_id, str) or not handoff_record_id:
        return False
    state = HandoffAttemptState(
        session_id=session_id,
        attempt_id=attempt_id,
        handoff_record_id=handoff_record_id,
        prior_handoff_markdown=marker.get("prior_handoff_markdown"),
        prior_markers={},
        missing_markers=frozenset({PENDING_HANDOFF_VARIABLE, HANDOFF_PULL_PENDING_VARIABLE}),
        prior_status=(
            marker.get("prior_status") if isinstance(marker.get("prior_status"), str) else None
        ),
    )
    updates = (
        {HANDOFF_DISPATCH_GATE_VARIABLE: dict(failure_result)}
        if failure_result is not None
        else None
    )
    return restore_handoff_attempt(db, state, marker_updates=updates)


def consume_pending_handoff(db: HubDatabase, caller_session_id: str) -> ConsumedHandoff | None:
    """Resolve and consume the caller's same-row or clear-predecessor handoff."""
    caller = db.fetchone(
        "SELECT id, parent_session_id FROM sessions WHERE id = %s",
        (caller_session_id,),
    )
    if caller is None:
        return None
    candidates = ((caller_session_id, False), (caller["parent_session_id"], True))
    for candidate_id, expects_clear in candidates:
        if not isinstance(candidate_id, str) or not candidate_id:
            continue
        consumed = _consume_candidate(
            db,
            candidate_id,
            continuation_session_id=caller_session_id,
            expects_clear=expects_clear,
        )
        if consumed is not None:
            if expects_clear:
                _clear_handoff_pull_pending(db, caller_session_id)
            return consumed
    return None


def _consume_candidate(
    db: HubDatabase,
    session_id: str,
    *,
    continuation_session_id: str,
    expects_clear: bool,
) -> ConsumedHandoff | None:
    with db.transaction() as conn:
        session_row = conn.execute(
            "SELECT handoff_markdown FROM sessions WHERE id = %s FOR UPDATE",
            (session_id,),
        ).fetchone()
        if session_row is None:
            return None
        variable_row = conn.execute(
            "SELECT variables FROM session_variables WHERE session_id = %s FOR UPDATE",
            (session_id,),
        ).fetchone()
        if variable_row is None:
            return None
        variables = _load_variables(variable_row["variables"])
        marker = variables.get(PENDING_HANDOFF_VARIABLE)
        if not isinstance(marker, Mapping) or bool(marker.get("clear_session")) != expects_clear:
            return None
        attempt_id = marker.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            return None
        handoff_id = marker.get("handoff_record_id")
        if not isinstance(handoff_id, str) or not handoff_id:
            return None
        handoff_row = conn.execute(
            "SELECT rendered_markdown FROM session_handoffs WHERE id = %s AND session_id = %s",
            (handoff_id, session_id),
        ).fetchone()
        if handoff_row is None:
            return None
        if expects_clear:
            delivery = conn.execute(
                """
                SELECT 1 FROM session_handoff_deliveries
                WHERE handoff_id = %s
                  AND attempt_id = %s
                  AND boundary_kind = 'clear'
                """,
                (handoff_id, attempt_id),
            ).fetchone()
            if delivery is None:
                return None
        else:
            insert_delivery_receipt(
                conn,
                handoff_id=handoff_id,
                attempt_id=attempt_id,
                boundary_kind="compact",
                continuation_session_id=continuation_session_id,
            )
        variables.pop(PENDING_HANDOFF_VARIABLE, None)
        variables.pop(HANDOFF_PULL_PENDING_VARIABLE, None)
        _store_variables(conn, session_id, variables, exists=True)
        markdown = str(handoff_row["rendered_markdown"])
        return ConsumedHandoff(session_id, handoff_id, attempt_id, markdown)


def _clear_handoff_pull_pending(db: HubDatabase, session_id: str) -> None:
    """Drop the successor's pull-deferral flag after a clear handoff is consumed."""
    with db.transaction() as conn:
        variable_row = conn.execute(
            "SELECT variables FROM session_variables WHERE session_id = %s FOR UPDATE",
            (session_id,),
        ).fetchone()
        if variable_row is None:
            return
        variables = _load_variables(variable_row["variables"])
        if HANDOFF_PULL_PENDING_VARIABLE not in variables:
            return
        variables.pop(HANDOFF_PULL_PENDING_VARIABLE, None)
        _store_variables(conn, session_id, variables, exists=True)


def _insert_feedback_rows(
    conn: Any,
    session_id: str,
    observations: Sequence[FeedbackObservation],
    *,
    ids: list[str] | None = None,
) -> list[str]:
    ids = ids if ids is not None else [str(uuid4()) for _ in observations]
    if not ids:
        return ids
    created_at = utc_now()
    conn.executemany(
        """
        INSERT INTO session_feedback (
            id, session_id, source, kind, kind_other_label, evidence, impact, frequency,
            suggestion, disposition, reviewed, created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s)
        """,
        [
            (
                feedback_id,
                session_id,
                observation.source,
                observation.kind,
                observation.kind_other_label,
                observation.evidence,
                observation.impact,
                observation.frequency,
                observation.suggestion,
                observation.disposition,
                created_at,
            )
            for feedback_id, observation in zip(ids, observations, strict=True)
        ],
    )
    return ids


def _store_variables(
    conn: Any, session_id: str, variables: dict[str, Any], *, exists: bool
) -> None:
    payload = json.dumps(variables)
    now = utc_now()
    if exists:
        conn.execute(
            "UPDATE session_variables SET variables = %s, updated_at = %s WHERE session_id = %s",
            (payload, now, session_id),
        )
        return
    conn.execute(
        "INSERT INTO session_variables (session_id, variables, updated_at) VALUES (%s, %s, %s)",
        (session_id, payload, now),
    )


def _load_variables(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return dict(decoded) if isinstance(decoded, dict) else {}
    return {}


def _nonblank(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonblank string")
    return value.strip()
