"""CLI commands for token event auditing and inspection."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import click

from gobby.cli.runtime import require_cli_database
from gobby.cli.utils import resolve_project_ref, resolve_session_id
from gobby.sessions.model_family import normalize_model
from gobby.sessions.reported_usage import ReportedRun, collect_runs
from gobby.sessions.subagent_usage import read_all_subagent_events
from gobby.sessions.transcripts import get_parser
from gobby.sessions.usage_calls import api_call_count
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.reported_usage import ReportedUsageStore
from gobby.storage.sessions import SessionManager
from gobby.storage.token_events import (
    TokenEvent,
    TokenEventStore,
    canonicalize_event_timestamp,
    token_event_retention_cutoff,
    token_event_retention_transaction,
)


@click.group()
def tokens() -> None:
    """Inspect and repair token usage ledgers."""


def _load_session_messages(session_id: str, session: Any) -> list[Any]:
    transcript_path = getattr(session, "transcript_path", None)
    if not transcript_path:
        raise click.ClickException(f"Session {session_id} has no transcript path")

    path = Path(transcript_path)
    if not path.exists():
        raise click.ClickException(f"Transcript not found: {transcript_path}")

    raw = path.read_text(encoding="utf-8")
    source = getattr(session, "source", None)

    try:
        parser = get_parser(source, session_id=session_id, transcript_path=path)
        if path.suffix == ".json" and hasattr(parser, "parse_session_json"):
            data = json.loads(raw)
            return cast(list[Any], parser.parse_session_json(data))

        # Codex's parser may yield ParsedToolEvent alongside ParsedMessage;
        # the token-event consumer below only reads .model/.usage from
        # ParsedMessage, so filter the others out here.
        from gobby.sessions.transcripts.base import ParsedMessage

        parsed = parser.parse_lines(raw.splitlines(keepends=True), start_index=0)
        return [r for r in parsed if isinstance(r, ParsedMessage)]
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(
            f"Failed to parse transcript {transcript_path} for session {session_id}: {exc}"
        ) from exc


def _messages_to_events(
    session_id: str, session: Any, messages: list[Any]
) -> tuple[list[TokenEvent], str | None]:
    project_id = session.project_id if isinstance(session.project_id, str) else None
    source = session.source if isinstance(session.source, str) else "unknown"
    context_window = session.context_window if isinstance(session.context_window, int) else None
    last_model: str | None = None
    events: list[TokenEvent] = []

    seen_keys: set[str] = set()
    for message in messages:
        if message.model:
            last_model = message.model
        usage = message.usage
        if usage is None:
            continue
        if (
            usage.input_tokens == 0
            and usage.output_tokens == 0
            and usage.cache_creation_tokens == 0
            and usage.cache_read_tokens == 0
        ):
            continue
        if message.message_id is None:
            raise click.ClickException(
                f"Session {session_id} has token usage without a message id; "
                "the audit cannot key it"
            )
        # The first occurrence wins, as the ledger's (session_id, message_id) conflict does.
        if message.message_id in seen_keys:
            continue
        seen_keys.add(message.message_id)
        events.append(
            TokenEvent(
                session_id=session_id,
                project_id=project_id,
                message_id=message.message_id,
                source=source,
                origin="transcript",
                model=message.model,
                model_family=normalize_model(message.model),
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cache_creation_tokens=usage.cache_creation_tokens,
                cache_read_tokens=usage.cache_read_tokens,
                context_window=context_window,
                event_at=message.timestamp,
                metadata={"content_type": message.content_type},
                api_calls=api_call_count(source, message),
            )
        )

    return events, last_model


def _format_totals(label: str, totals: dict[str, int]) -> str:
    return (
        f"{label}: in={totals['input_tokens']} out={totals['output_tokens']} "
        f"cache_write={totals['cache_creation_tokens']} cache_read={totals['cache_read_tokens']}"
    )


def _session_totals(session: Any) -> dict[str, int]:
    return {
        "input_tokens": int(getattr(session, "usage_input_tokens", 0) or 0),
        "output_tokens": int(getattr(session, "usage_output_tokens", 0) or 0),
        "cache_creation_tokens": int(getattr(session, "usage_cache_creation_tokens", 0) or 0),
        "cache_read_tokens": int(getattr(session, "usage_cache_read_tokens", 0) or 0),
    }


@dataclass(frozen=True)
class SessionAudit:
    """One session's audit: stored rows against the transcript and sessions.usage_*."""

    session_id: str
    stale_rows: int
    missing_rows: int
    usage_mismatch: bool
    repaired: bool = False
    stale_runs: int = 0
    missing_runs: int = 0
    run_count: int = 0
    run_totals: dict[str, Decimal] = field(default_factory=dict)

    @property
    def missing(self) -> bool:
        return self.missing_rows > 0 or self.usage_mismatch


_COMPARED_COLUMNS = (
    "api_calls",
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
)


def _read_stored_rows(db: HubDatabase, session_id: str) -> list[Mapping[str, Any]]:
    return db.fetchall(
        """
        SELECT id, message_id, api_calls, event_at, input_tokens, output_tokens,
               cache_creation_tokens, cache_read_tokens
        FROM token_events
        WHERE session_id = %s
        """,
        (session_id,),
    )


def _differs(row: Mapping[str, Any], event: TokenEvent) -> bool:
    return any(row[column] != getattr(event, column) for column in _COMPARED_COLUMNS)


def _expired(value: datetime | str | None, cutoff: datetime) -> bool:
    return canonicalize_event_timestamp(value) < cutoff


def _reinsertable(event: TokenEvent, archived: frozenset[str]) -> bool:
    # An expired event may already be counted under its own key or under the unkeyed key.
    # Only an event with a key that neither key archives is safe to re-insert.
    return event.message_id is not None and event.message_id not in archived and "" not in archived


def _run_missing(stored: ReportedRun | None, derived: ReportedRun) -> bool:
    """A transcript run is missing when storage lacks it or holds an older reading."""
    return stored is None or stored.observed_at < derived.observed_at


def _transcript_runs(session: Any, messages: Sequence[Any]) -> list[ReportedRun] | None:
    """The transcript's provider-reported runs, or None when the transcript cannot be read."""
    try:
        with open(session.transcript_path, encoding="utf-8") as handle:
            return collect_runs(
                session.source,
                lines=handle,
                messages=messages,
                transcript_path=session.transcript_path,
                session_started_at=session.created_at,
            )
    except OSError:
        return None


def _run_totals(runs: Sequence[ReportedRun]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for run in runs:
        if run.cost_unit is not None and run.cost_amount is not None:
            totals[run.cost_unit] = totals.get(run.cost_unit, Decimal(0)) + run.cost_amount
    return totals


def _format_runs(audit: SessionAudit) -> str:
    amounts = " ".join(f"{unit}={amount}" for unit, amount in sorted(audit.run_totals.items()))
    return f"runs={audit.run_count}" + (f" {amounts}" if amounts else "")


def _audit_session(db: HubDatabase, session_id: str, *, fix: bool) -> SessionAudit:
    # With --fix, the shared retention lock spans the read and the writes, so a prune cannot
    # archive a row between them. Stored rows are read before the transcript: a line the
    # processor ingests in between is then absent from `stored`, so --fix never deletes it.
    with token_event_retention_transaction(db) if fix else nullcontext():
        stored = _read_stored_rows(db, session_id)
        runs_store = ReportedUsageStore(db)
        stored_runs = runs_store.list_runs(session_id)
        session_manager = SessionManager(db)
        session = session_manager.get(session_id)
        if session is None:
            raise click.ClickException(f"Session {session_id} not found")
        messages = _load_session_messages(session_id, session)
        derived, last_model = _messages_to_events(session_id, session, messages)
        derived_runs: list[ReportedRun] | None = None
        # Subagent calls carry agent-scoped ids, so they cannot collide with parent keys.
        if isinstance(session.transcript_path, str):
            subagent_events = read_all_subagent_events(
                session_id,
                session.project_id if isinstance(session.project_id, str) else None,
                session.transcript_path,
            )
            derived.extend(subagent_events)
            derived_runs = _transcript_runs(session, messages)

        # Retention archives events older than the cutoff into token_event_retention_totals
        # and deletes their rows. An expired event with no stored row was pruned, so it is
        # re-inserted only when its key is unarchived. A stored row whose key the transcript no
        # longer produces is drift, expired or not, and --fix deletes it. An unkeyed ('') archive
        # row may already count any expired row, so while one exists, expired drift is kept.
        store = TokenEventStore(db)
        cutoff = token_event_retention_cutoff()
        archived = store.archived_message_keys(session_id)
        keep_expired = "" in archived
        derived_by_key = {
            event.message_id: event for event in derived if event.message_id is not None
        }
        stored_keys = {row["message_id"] for row in stored if row["message_id"] is not None}
        # A row with no message_id matches no transcript key, so it is drift that --fix replaces.
        absent = [row for row in stored if row["message_id"] not in derived_by_key]
        differing = [
            row
            for row in stored
            if row["message_id"] in derived_by_key
            and _differs(row, derived_by_key[row["message_id"]])
        ]
        repairable = [
            event
            for event in derived
            if event.message_id in stored_keys
            or not _expired(event.event_at, cutoff)
            or _reinsertable(event, archived)
        ]
        missing = [event for event in repairable if event.message_id not in stored_keys]
        # Lifetime totals include the retention archive, which the stored rows do not.
        usage_mismatch = store.get_session_totals(session_id) != _session_totals(session)
        stale_rows = len(absent) + len(differing)
        # An unreadable transcript leaves the run totals unknown, so no run is missing or stale.
        runs_now = derived_runs or []
        stored_run_by_key = {run.run_key: run for run in stored_runs}
        missing_runs: list[ReportedRun] = []
        stale_run_keys: list[str] = []
        if derived_runs is not None:
            missing_runs = [
                run for run in derived_runs if _run_missing(stored_run_by_key.get(run.run_key), run)
            ]
            derived_run_keys = {run.run_key for run in derived_runs}
            stale_run_keys = [key for key in stored_run_by_key if key not in derived_run_keys]
        run_drift = bool(missing_runs or stale_run_keys)

        repaired = False
        if fix and (stale_rows or missing or usage_mismatch or run_drift):
            doomed = [
                row["id"]
                for row in absent
                if not (keep_expired and _expired(row["event_at"], cutoff))
            ]
            if doomed:
                db.execute("DELETE FROM token_events WHERE id = ANY(%s)", (doomed,))
            store.repair_keyed_events(repairable)
            runs_store.delete_runs(session_id, stale_run_keys)
            runs_store.upsert_runs(session_id, runs_now)
            repaired_totals = store.get_session_totals(session_id)
            session_manager.update_usage(
                session_id=session_id,
                input_tokens=repaired_totals["input_tokens"],
                output_tokens=repaired_totals["output_tokens"],
                cache_creation_tokens=repaired_totals["cache_creation_tokens"],
                cache_read_tokens=repaired_totals["cache_read_tokens"],
                context_window=(
                    session.context_window if isinstance(session.context_window, int) else None
                ),
                model=last_model,
            )
            repaired = True

    return SessionAudit(
        session_id=session_id,
        stale_rows=stale_rows,
        missing_rows=len(missing),
        usage_mismatch=usage_mismatch,
        repaired=repaired,
        stale_runs=len(stale_run_keys),
        missing_runs=len(missing_runs),
        run_count=len(runs_now),
        run_totals=_run_totals(runs_now),
    )


@tokens.command("audit")
@click.option("--session", "session_ref", help="Session UUID/#ref/prefix to audit.")
@click.option("--all", "audit_all", is_flag=True, help="Audit all sessions with transcripts.")
@click.option("--fix", is_flag=True, help="Rebuild token_events from the transcript.")
@click.option("--project", "project_ref", help="Project name or UUID for #ref resolution.")
def audit_tokens(
    session_ref: str | None,
    audit_all: bool,
    fix: bool,
    project_ref: str | None,
) -> None:
    """Audit transcript-derived token usage against token_events and sessions."""
    if not session_ref and not audit_all:
        raise click.UsageError("Provide --session or --all")

    project_id = resolve_project_ref(project_ref) if project_ref else None

    with nullcontext(require_cli_database()) as db:
        if audit_all:
            query = """
                SELECT id
                FROM sessions
                WHERE transcript_path IS NOT NULL
                  AND source != 'system'
            """
            params: tuple[Any, ...] = ()
            if project_id is not None:
                query += "\n  AND project_id = %s"
                params = (project_id,)
            query += "\n                ORDER BY updated_at DESC"
            rows = db.fetchall(query, params)
            session_ids = [str(row["id"]) for row in rows]
        else:
            assert session_ref is not None
            session_ids = [resolve_session_id(session_ref, project_id=project_id)]

        audited = 0
        stale_sessions = 0
        missing_sessions = 0
        repaired_sessions = 0

        for session_id in session_ids:
            try:
                audit = _audit_session(db, session_id, fix=fix)
            except click.ClickException as exc:
                click.echo(f"{session_id}: {exc.message}", err=True)
                continue

            audited += 1
            if audit.stale_rows or audit.stale_runs:
                stale_sessions += 1
            if audit.missing or audit.missing_runs:
                missing_sessions += 1
            if audit.repaired:
                repaired_sessions += 1
            drift = audit.stale_rows or audit.missing or audit.stale_runs or audit.missing_runs
            if not drift:
                if not fix:
                    click.echo(f"{session_id}: ok {_format_runs(audit)}")
                continue
            click.echo(
                f"{session_id}: stale={audit.stale_rows} missing={audit.missing_rows} "
                f"usage_mismatch={audit.usage_mismatch} stale_runs={audit.stale_runs} "
                f"missing_runs={audit.missing_runs} {_format_runs(audit)}"
            )
            if audit.repaired:
                click.echo("  repaired")

        summary = f"audited={audited} stale={stale_sessions} missing={missing_sessions}"
        click.echo(f"{summary} repaired={repaired_sessions}" if fix else summary)


@tokens.command("stats")
@click.option("--project", "project_ref", help="Filter to a project.")
def token_stats(project_ref: str | None) -> None:
    """Show token event ledger statistics."""
    project_id = resolve_project_ref(project_ref) if project_ref else None
    with nullcontext(require_cli_database()) as db:
        store = TokenEventStore(db)
        breakdown = store.get_breakdown(project_id=project_id)
        total_rows = db.fetchone(
            "SELECT COUNT(*) AS count FROM token_events"
            + (" WHERE project_id = %s" if project_id else ""),
            (project_id,) if project_id else (),
        )
        click.echo(f"rows={int(total_rows['count'] or 0) if total_rows else 0}")
        click.echo(
            _format_totals(
                "totals",
                {
                    "input_tokens": breakdown["totals"]["input_tokens"],
                    "output_tokens": breakdown["totals"]["output_tokens"],
                    "cache_creation_tokens": breakdown["totals"]["cache_creation_tokens"],
                    "cache_read_tokens": breakdown["totals"]["cache_read_tokens"],
                },
            )
        )
        click.echo(f"sessions={breakdown['totals']['session_count']}")
