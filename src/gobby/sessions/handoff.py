"""Structured handoff rendering, feedback persistence, and one-shot delivery."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Never
from uuid import uuid4

from gobby.storage.hub.protocol import HubDatabase
from gobby.tasks.state_semantics import get_claimed_session_id
from gobby.utils.datetime import utc_now

if TYPE_CHECKING:
    from gobby.storage.tasks import Task

PENDING_HANDOFF_VARIABLE = "set_handoff_pending"
HANDOFF_PULL_PENDING_VARIABLE = "handoff_pull_pending"
REQUIRED_SKILLS_VARIABLE = "compact_resume_required_skills"
ADVISORY_SKILLS_VARIABLE = "compact_resume_advisory_skills"

_OPTIONAL_FEEDBACK_FIELDS = ("suggestion", "disposition")

FEEDBACK_KINDS = ("friction", "bug", "noise", "surprise", "missing-affordance", "useful", "other")
FEEDBACK_FREQUENCIES = ("once", "repeated", "always")
FEEDBACK_DISPOSITIONS = ("worked-around", "filed-task", "fixed", "escalated", "noted")
FEEDBACK_TASK_REF_RE = re.compile(r"#(\d{3,6})")
_FEEDBACK_SESSION_REF_RE = re.compile(
    r"(?:\b[\w.-]+-S#\d+\b|(?<![\w#])#\d+\b|"
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b)"
)


def build_handoff_continue_prompt() -> str:
    """Return the pull-only continuation directive used after compact and clear."""
    return (
        "Call `get_handoff()` on `gobby-sessions`, follow the returned handoff and "
        "skill reload tiers, then continue."
    )


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
    prior_handoff_markdown: str | None
    prior_markers: dict[str, Any]
    missing_markers: frozenset[str]
    feedback_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConsumedHandoff:
    session_id: str
    markdown: str
    required_skills: tuple[str, ...]
    advisory_skills: tuple[str, ...]


def render_handoff_markdown(
    *,
    current_state: str,
    next_steps: Sequence[str],
    key_decisions: Sequence[str] = (),
    blockers: Sequence[str] = (),
    notes: Sequence[str] = (),
    references: Sequence[str] = (),
) -> str:
    """Validate and deterministically render the public handoff fields."""
    state = _nonblank(current_state, "current_state")
    normalized_next_steps = _nonblank_list(next_steps, "next_steps", required=True)
    sections: list[str] = ["## Current State", "", state, "", "## Next Steps", ""]
    sections.extend(f"{index}. {step}" for index, step in enumerate(normalized_next_steps, start=1))

    optional_sections = (
        ("Key Decisions", _nonblank_list(key_decisions, "key_decisions")),
        ("Blockers", _nonblank_list(blockers, "blockers")),
        ("Notes", _nonblank_list(notes, "notes")),
        ("References", _deduplicate(_nonblank_list(references, "references"))),
    )
    for heading, entries in optional_sections:
        if not entries:
            continue
        sections.extend(("", f"## {heading}", ""))
        sections.extend(f"- {entry}" for entry in entries)
    return "\n".join(sections).strip()


def normalize_feedback_observations(
    observations: Sequence[Mapping[str, Any]] | None,
    *,
    resolve_task: Callable[[str], Task | None] | None = None,
    session_id: str | None = None,
) -> list[FeedbackObservation]:
    """Validate feedback input without mutating storage."""
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
                get_claimed_session_id(task) != session_id
                and task.closed_in_session_id != session_id
            ):
                _raise_disposition_error(
                    index,
                    "'fixed' requires a task claimed or closed by this session",
                )
        normalized.append(
            FeedbackObservation(
                source=_nonblank(raw.get("source"), f"observations[{index}].source"),
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
    markdown: str,
    observations: Sequence[FeedbackObservation],
    clear_session: bool,
    additional_markers: Mapping[str, Any] | None = None,
) -> HandoffAttemptState:
    """Atomically stage handoff Markdown, feedback, and delivery markers."""
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
            "SELECT handoff_markdown FROM sessions WHERE id = %s FOR UPDATE",
            (session_id,),
        ).fetchone()
        if session_row is None:
            raise ValueError(f"Session {session_id} not found")
        variable_row = conn.execute(
            "SELECT variables FROM session_variables WHERE session_id = %s FOR UPDATE",
            (session_id,),
        ).fetchone()
        variables = _load_variables(variable_row["variables"] if variable_row else None)
        prior_markers = {name: variables[name] for name in marker_updates if name in variables}
        missing_markers = frozenset(name for name in marker_updates if name not in variables)
        feedback_ids = [str(uuid4()) for _ in observations]
        for name, value in marker_updates.items():
            if isinstance(value, dict) and value.get("attempt_id") == attempt_id:
                marker_updates[name] = {
                    **value,
                    "prior_handoff_markdown": session_row["handoff_markdown"],
                    "feedback_ids": feedback_ids,
                }
        variables.update(marker_updates)

        conn.execute(
            "UPDATE sessions SET handoff_markdown = %s, updated_at = %s WHERE id = %s",
            (markdown, utc_now(), session_id),
        )
        _store_variables(conn, session_id, variables, exists=variable_row is not None)
        _insert_feedback_rows(conn, session_id, observations, ids=feedback_ids)

    return HandoffAttemptState(
        session_id=session_id,
        attempt_id=attempt_id,
        prior_handoff_markdown=session_row["handoff_markdown"],
        prior_markers=prior_markers,
        missing_markers=missing_markers,
        feedback_ids=tuple(feedback_ids),
    )


def restore_handoff_attempt(db: HubDatabase, state: HandoffAttemptState) -> bool:
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
        if not isinstance(pending, Mapping) or pending.get("attempt_id") != state.attempt_id:
            return False

        for name in state.missing_markers:
            current = variables.get(name)
            if not isinstance(current, Mapping) or current.get("attempt_id") == state.attempt_id:
                variables.pop(name, None)
        variables.update(state.prior_markers)
        conn.execute(
            "UPDATE sessions SET handoff_markdown = %s, updated_at = %s WHERE id = %s",
            (state.prior_handoff_markdown, utc_now(), state.session_id),
        )
        _store_variables(conn, state.session_id, variables, exists=True)
        if state.feedback_ids:
            conn.executemany(
                "DELETE FROM session_feedback WHERE id = %s AND session_id = %s",
                [(feedback_id, state.session_id) for feedback_id in state.feedback_ids],
            )
    return True


def restore_staged_handoff(db: HubDatabase, session_id: str, attempt_id: str) -> bool:
    """Restore an attempt later using compensation data stored in its pending marker."""
    row = db.fetchone(
        "SELECT variables FROM session_variables WHERE session_id = %s",
        (session_id,),
    )
    variables = _load_variables(row["variables"] if row else None)
    marker = variables.get(PENDING_HANDOFF_VARIABLE)
    if not isinstance(marker, Mapping) or marker.get("attempt_id") != attempt_id:
        return False
    feedback_ids = marker.get("feedback_ids")
    state = HandoffAttemptState(
        session_id=session_id,
        attempt_id=attempt_id,
        prior_handoff_markdown=marker.get("prior_handoff_markdown"),
        prior_markers={},
        missing_markers=frozenset({PENDING_HANDOFF_VARIABLE}),
        feedback_ids=tuple(item for item in feedback_ids or () if isinstance(item, str)),
    )
    return restore_handoff_attempt(db, state)


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
        consumed = _consume_candidate(db, candidate_id, expects_clear=expects_clear)
        if consumed is not None:
            if expects_clear:
                _clear_handoff_pull_pending(db, caller_session_id)
            return consumed
    return None


def _consume_candidate(
    db: HubDatabase,
    session_id: str,
    *,
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
        variables.pop(PENDING_HANDOFF_VARIABLE, None)
        variables.pop(HANDOFF_PULL_PENDING_VARIABLE, None)
        _store_variables(conn, session_id, variables, exists=True)
        markdown = str(session_row["handoff_markdown"] or "")
        required = tuple(_string_list(variables.get(REQUIRED_SKILLS_VARIABLE)))
        advisory = tuple(
            item
            for item in _string_list(variables.get(ADVISORY_SKILLS_VARIABLE))
            if item not in required
        )
        return ConsumedHandoff(session_id, markdown, required, advisory)


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


def _deduplicate(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]
