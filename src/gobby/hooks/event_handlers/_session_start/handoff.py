"""In-place compact identity resolution for session-start handling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from gobby.hooks.context_limits import handoff_summary_inject_budget_for
from gobby.llm.sdk_utils import (
    MANDATORY_HANDOFF_SECTION_TITLES,
    allocate_section_budget,
    head_with_breadcrumb,
    split_markdown_sections,
)
from gobby.sessions.compact_continuation import (
    COMPACT_HANDOFF_MARKER_VARIABLE,
    consume_compact_handoff_marker,
)
from gobby.sessions.compact_identity import resolve_compact_continuation
from gobby.sessions.compact_markers import COMPACT_NOTIFICATION_STARTED_AT_VARIABLE
from gobby.sessions.handoff_identity import terminal_contexts_match
from gobby.sessions.tmux_context import parse_terminal_context_value
from gobby.utils.injected_context import strip_injected_context

_SECTION_PRIORITIES = {
    "next steps": 10,
    "current state": 20,
    "unresolved errors": 30,
    "key technical decisions": 40,
    "problems encountered": 50,
    "what didn't work": 55,
    "files changed": 70,
    "what was accomplished": 80,
}
_OMISSION_TITLE_LIMIT = 10
_OMISSION_TITLE_CHARS = 40
_HANDOFF_SUMMARY_VARIABLES = (
    "session_summary",
    "full_session_summary",
    "handoff_summary_injectable",
)
_TERMINAL_IDENTITY_FIELDS = ("tmux_pane", "tmux_session", "tty", "parent_pid")


def _format_omission_titles(titles: tuple[str, ...]) -> str:
    displayed = [title[:_OMISSION_TITLE_CHARS] for title in titles[:_OMISSION_TITLE_LIMIT]]
    if len(titles) > _OMISSION_TITLE_LIMIT:
        displayed.append(f"+{len(titles) - _OMISSION_TITLE_LIMIT} more")
    return ", ".join(displayed)


def _omission_line(titles: tuple[str, ...]) -> str:
    return (
        f"Omitted sections: {_format_omission_titles(titles)} "
        "— full summary via get_handoff_context."
    )


@dataclass(frozen=True)
class SessionStartResolution:
    """Outcome of resolving a session start against its persisted identity."""

    session: Any | None
    session_source: str
    blocked_reason: str | None = None

    @property
    def is_compact(self) -> bool:
        return self.blocked_reason is None and self.session_source == "compact"


def resolve_session_start_identity(
    handler: Any,
    input_data: dict[str, Any],
    session_source: str,
    *,
    external_id: str,
    machine_id: str,
    project_id: str,
    cli_source: str,
) -> SessionStartResolution:
    """Resolve a terminal session start against its persisted session row.

    Compaction is an in-place handoff. A marked row with exact terminal process
    identity is canonical even when ingress carries a differing provider ID.
    Compact classification is one-shot: an explicit compact source, a
    handoff_ready row, or an expired row with an unconsumed compact marker.
    """
    if session_source == "clear" or not handler._session_manager:
        return SessionStartResolution(session=None, session_source=session_source)

    session = None
    drifted_project = False
    try:
        session = handler._session_manager.find_by_external_id(
            external_id,
            project_id,
            cli_source,
            session_type="terminal",
        )
        if session is None:
            session = handler._session_manager.find_by_external_id_any_project(
                external_id,
                cli_source,
                session_type="terminal",
            )
            drifted_project = session is not None
    except Exception as e:
        handler.logger.warning("Session identity lookup failed for %s: %s", external_id, e)
        return SessionStartResolution(session=None, session_source=session_source)

    if drifted_project and session is not None:
        handler.logger.warning(
            "Session %s for external_id=%s found under project %s instead of %s; "
            "registration will rebind it (cwd/project drift)",
            session.id,
            external_id,
            getattr(session, "project_id", None),
            project_id,
        )

    compact_candidate_resolved = False
    try:
        candidate_resolution = resolve_compact_continuation(
            handler._session_manager.db,
            source=cli_source,
            terminal_context=input_data.get("terminal_context"),
        )
    except Exception as e:
        handler.logger.warning(
            "Compact continuation lookup failed for %s: %s",
            external_id,
            e,
        )
        return SessionStartResolution(session=session, session_source=session_source)
    if candidate_resolution.ambiguous:
        handler.logger.warning(
            "Blocking compact restart: terminal process matches multiple compact rows",
            extra={
                "event": "compact_identity_ambiguous",
                "observed_external_id": external_id,
                "conflicting_session_ids": list(candidate_resolution.conflicting_session_ids),
            },
        )
        return SessionStartResolution(
            session=session,
            session_source=session_source,
            blocked_reason=(
                "Compact restart terminal identity matches multiple persisted sessions."
            ),
        )
    if candidate_resolution.session is not None:
        compact_candidate_resolved = True
        candidate = candidate_resolution.session
        if session is None or session.id != candidate.id:
            handler.logger.info(
                "Canonicalized compact provider identity for session %s",
                candidate.id,
                extra={
                    "event": "compact_identity_canonicalized",
                    "session_id": candidate.id,
                    "canonical_external_id": candidate.external_id,
                    "observed_external_id": external_id,
                    "superseded_session_id": getattr(session, "id", None),
                },
            )
        session = candidate

    marker_present = False
    if session is not None:
        try:
            from gobby.workflows.state_manager import SessionVariableManager

            row_vars = SessionVariableManager(handler._session_manager.db).get_variables(session.id)
            marker_present = row_vars.get(COMPACT_HANDOFF_MARKER_VARIABLE) == "compact"
        except Exception as e:
            handler.logger.debug("Could not read compact marker for %s: %s", session.id, e)

    status = getattr(session, "status", None)
    is_compact = (
        session_source == "compact"
        or compact_candidate_resolved
        or (
            session is not None
            and (status == "handoff_ready" or (status == "expired" and marker_present))
        )
    )
    if not is_compact:
        return SessionStartResolution(session=session, session_source=session_source)

    if session is None:
        _log_missing_compact_row(
            handler,
            external_id=external_id,
            machine_id=machine_id,
            project_id=project_id,
            cli_source=cli_source,
        )
        input_data["source"] = "startup"
        return SessionStartResolution(session=None, session_source="startup")

    identity = _classify_terminal_identity(session, input_data.get("terminal_context"))
    if identity == "conflict":
        handler.logger.warning(
            "Blocking compact restart for session %s (external_id=%s): terminal "
            "identity contradicts the persisted session",
            session.id,
            external_id,
        )
        return SessionStartResolution(
            session=session,
            session_source=session_source,
            blocked_reason=(
                "Compact restart terminal identity does not match the persisted session."
            ),
        )

    input_data["source"] = "compact"
    return SessionStartResolution(session=session, session_source="compact")


def rebind_resumed_session_start(
    handler: Any,
    input_data: dict[str, Any],
    session: Any,
    *,
    machine_id: str,
    project_id: str,
    cli_source: str,
    terminal_context: dict[str, Any] | None,
    transcript_path: str | None,
) -> tuple[Any, str | None]:
    """Bind an explicit resume to its persisted row and fresh runtime context."""
    if not transcript_path:
        transcript_path = handler._derive_transcript_path(
            cli_source,
            input_data,
            session.external_id,
            owner_machine_id=machine_id,
            local_machine_id=machine_id,
        )

    raw_depth = input_data.get("agent_depth")
    try:
        agent_depth = int(raw_depth) if raw_depth is not None else 0
    except (TypeError, ValueError):
        agent_depth = 0
    raw_sandbox = input_data.get("sandbox_enabled")
    sandbox_enabled = raw_sandbox if isinstance(raw_sandbox, bool) else None

    rebound = handler._session_manager.rebind_resumed_terminal_session(
        session.id,
        machine_id=machine_id,
        project_id=project_id,
        source=cli_source,
        transcript_path=transcript_path,
        terminal_context=terminal_context,
        workflow_name=input_data.get("workflow_name"),
        agent_depth=agent_depth,
        sandbox_enabled=sandbox_enabled,
    )
    if rebound is None:
        raise RuntimeError(f"Session {session.id} is ineligible for explicit resume")
    handler._session_manager.cache_session_mapping(
        external_id=rebound.external_id,
        source=cli_source,
        session_id=rebound.id,
        project_id=rebound.project_id,
        session_type=rebound.session_type,
    )
    return rebound, transcript_path


def _classify_terminal_identity(
    session: Any,
    child_context: Any,
) -> str:
    """Classify terminal identity as ``match``, ``conflict``, or ``unknown``.

    Insufficient identity on either side is ``unknown``, never a conflict —
    only comparable, non-empty identity fields that contradict each other
    prove the start belongs to a different terminal.
    """
    child = parse_terminal_context_value(child_context)
    if not child:
        return "unknown"
    session_id = getattr(session, "id", None)
    if isinstance(session_id, str) and child.get("gobby_session_id") == session_id:
        return "match"
    stored = parse_terminal_context_value(getattr(session, "terminal_context", None))
    if not stored:
        return "unknown"

    def _has(context: dict[str, Any], field: str) -> bool:
        return bool(str(context.get(field) or "").strip())

    comparable = [
        field for field in _TERMINAL_IDENTITY_FIELDS if _has(child, field) and _has(stored, field)
    ]
    if not comparable:
        return "unknown"
    return "match" if terminal_contexts_match(stored, child) else "conflict"


def _log_missing_compact_row(
    handler: Any,
    *,
    external_id: str,
    machine_id: str,
    project_id: str,
    cli_source: str,
) -> None:
    """Warn loudly when a compact restart has no persisted row to reactivate."""
    handler.logger.warning(
        "Compact restart found no persisted session for external_id=%s "
        "machine_id=%s project_id=%s source=%s; degrading to startup "
        "registration without compact context",
        external_id,
        machine_id,
        project_id,
        cli_source,
    )


def prepare_compact_continuation_variables(
    handler: Any,
    session_id: str | None,
    session_source: str,
) -> None:
    """Refresh same-row summary and skill variables for an in-place compact restart.

    Replaces the bounded summary variables from the row's current summary,
    clears stale injection values when no usable summary exists (or injection
    is disabled), normalizes the required-skill reload list, and consumes the
    one-shot compact marker.
    """
    if session_source != "compact" or not session_id or not handler._session_manager:
        return

    from gobby.workflows.state_manager import SessionVariableManager

    db = handler._session_manager.db
    sv_mgr = SessionVariableManager(db)
    current_vars = sv_mgr.get_variables(session_id)
    sv_mgr.set_variable(
        session_id,
        COMPACT_NOTIFICATION_STARTED_AT_VARIABLE,
        datetime.now(UTC).isoformat(),
    )
    auto_inject = _variable_enabled(current_vars.get("auto_inject_handoff"), default=True)

    session = handler._session_manager.get(session_id)
    summary_markdown = ""
    if session is not None and session.summary_markdown:
        summary_markdown = strip_injected_context(session.summary_markdown)

    if auto_inject and summary_markdown:
        sv_mgr.merge_variables(
            session_id,
            {
                "session_summary": summary_markdown,
                "full_session_summary": summary_markdown,
                "handoff_summary_injectable": _bound_handoff_summary(summary_markdown, session),
            },
        )
    else:
        stale = {name: "" for name in _HANDOFF_SUMMARY_VARIABLES if current_vars.get(name)}
        if stale:
            sv_mgr.merge_variables(session_id, stale)

    _normalize_compact_resume_required_skills(sv_mgr, session_id, current_vars)
    consume_compact_handoff_marker(db, session_id)


def _variable_enabled(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"false", "0", "no", "off", ""}:
            return False
        if normalized in {"true", "1", "yes", "on"}:
            return True
    return default


def _bound_handoff_summary(summary: str, session: Any) -> str:
    """Bound a pre-compaction summary for inline injection, with a retrieval breadcrumb.

    The full summary stays available via the get_handoff_context MCP tool; this
    only caps the copy injected into provider context. Returns the summary
    unchanged when it already fits within the budget.
    """
    budget = handoff_summary_inject_budget_for(getattr(session, "source", None))
    if len(summary) <= budget:
        return summary

    seq_num = getattr(session, "seq_num", None)
    ref = f"#{seq_num}" if seq_num else (getattr(session, "id", "") or "")
    ref_clause = f' with your own session ref "{ref}"' if ref else ""
    breadcrumb = (
        "> ⚠️ This is a truncated head of this session's pre-compaction summary "
        f"({len(summary)} chars total), shortened to fit the inline handoff "
        "budget. Call get_handoff_context (gobby-sessions)"
        f"{ref_clause} to load the full summary."
    )
    sections = split_markdown_sections(summary)
    real_sections = [section for section in sections if section.heading]
    has_mandatory_section = any(
        section.title in MANDATORY_HANDOFF_SECTION_TITLES for section in real_sections
    )
    if not real_sections or not has_mandatory_section:
        return head_with_breadcrumb(
            summary,
            budget=budget,
            breadcrumb=breadcrumb,
        )

    worst_case_titles = tuple(
        section.display_title[:_OMISSION_TITLE_CHARS].ljust(_OMISSION_TITLE_CHARS, "x")
        for section in sections
    )
    worst_case_suffix = f"\n\n{breadcrumb}\n{_omission_line(worst_case_titles)}"
    available = max(0, budget - len(worst_case_suffix))
    allocation = allocate_section_budget(sections, _SECTION_PRIORITIES, available)

    suffix = f"\n\n{breadcrumb}"
    if allocation.omitted_titles:
        suffix = f"{suffix}\n{_omission_line(allocation.omitted_titles)}"
    return f"{allocation.text.rstrip()}{suffix}"


def _normalize_compact_resume_required_skills(
    sv_mgr: Any,
    session_id: str,
    current_vars: dict[str, Any],
) -> None:
    raw_skills = current_vars.get("compact_resume_required_skills")
    if not isinstance(raw_skills, list):
        return

    skills: list[str] = []
    seen: set[str] = set()
    for value in raw_skills:
        if not isinstance(value, str):
            continue
        skill = value.strip()
        if not skill or skill in seen:
            continue
        seen.add(skill)
        skills.append(skill)

    if skills and skills != raw_skills:
        sv_mgr.merge_variables(session_id, {"compact_resume_required_skills": skills})
