"""CLI commands for token event auditing and inspection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from gobby.cli.utils import resolve_project_ref, resolve_session_id
from gobby.sessions.model_family import normalize_model
from gobby.sessions.transcripts.claude import ClaudeTranscriptParser
from gobby.sessions.transcripts.codex import CodexTranscriptParser
from gobby.sessions.transcripts.gemini import GeminiTranscriptParser
from gobby.sessions.transcripts.qwen import QwenTranscriptParser
from gobby.storage.database import LocalDatabase
from gobby.storage.migrations import run_migrations
from gobby.storage.sessions import LocalSessionManager
from gobby.storage.token_events import TokenEvent, TokenEventStore


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

    parser: Any = ClaudeTranscriptParser(session_id=session_id)
    if source == "gemini":
        parser = GeminiTranscriptParser(session_id=session_id)
    elif source == "qwen":
        parser = QwenTranscriptParser(session_id=session_id)
    elif source == "codex":
        parser = CodexTranscriptParser(session_id=session_id)

    if path.suffix == ".json" and hasattr(parser, "parse_session_json"):
        data = json.loads(raw)
        return parser.parse_session_json(data)

    return parser.parse_lines(raw.splitlines(keepends=True), start_index=0)


def _messages_to_events(session_id: str, session: Any, messages: list[Any]) -> tuple[list[TokenEvent], str | None]:
    project_id = session.project_id if isinstance(session.project_id, str) else None
    source = session.source if isinstance(session.source, str) else "unknown"
    context_window = session.context_window if isinstance(session.context_window, int) else None
    last_model: str | None = None
    events: list[TokenEvent] = []

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

    project_id = resolve_project_ref(project_ref, exit_on_not_found=False) if project_ref else None

    db = LocalDatabase()
    try:
        run_migrations(db)
        session_manager = LocalSessionManager(db)
        store = TokenEventStore(db)

        if audit_all:
            rows = db.fetchall(
                """
                SELECT id
                FROM sessions
                WHERE transcript_path IS NOT NULL
                  AND source != 'system'
                ORDER BY updated_at DESC
                """
            )
            session_ids = [str(row["id"]) for row in rows]
        else:
            assert session_ref is not None
            session_ids = [resolve_session_id(session_ref, project_id=project_id)]

        audited = 0
        drifted = 0
        repaired = 0

        for session_id in session_ids:
            session = session_manager.get(session_id)
            if session is None:
                click.echo(f"{session_id}: session not found", err=True)
                continue

            try:
                messages = _load_session_messages(session_id, session)
            except click.ClickException as exc:
                click.echo(f"{session_id}: {exc.message}", err=True)
                continue

            transcript_events, last_model = _messages_to_events(session_id, session, messages)
            transcript_totals = {
                "input_tokens": sum(event.input_tokens for event in transcript_events),
                "output_tokens": sum(event.output_tokens for event in transcript_events),
                "cache_creation_tokens": sum(
                    event.cache_creation_tokens for event in transcript_events
                ),
                "cache_read_tokens": sum(event.cache_read_tokens for event in transcript_events),
            }
            stored_totals = store.get_session_totals(session_id)
            cached_totals = _session_totals(session)

            drift = transcript_totals != stored_totals or transcript_totals != cached_totals
            audited += 1
            if drift:
                drifted += 1
                click.echo(f"{session_id}: drift detected")
                click.echo(f"  {_format_totals('transcript', transcript_totals)}")
                click.echo(f"  {_format_totals('token_events', stored_totals)}")
                click.echo(f"  {_format_totals('session', cached_totals)}")

                if fix:
                    store.delete_session_events(session_id)
                    for event in transcript_events:
                        store.record(event)
                    session_manager.update_usage(
                        session_id=session_id,
                        input_tokens=transcript_totals["input_tokens"],
                        output_tokens=transcript_totals["output_tokens"],
                        cache_creation_tokens=transcript_totals["cache_creation_tokens"],
                        cache_read_tokens=transcript_totals["cache_read_tokens"],
                        context_window=(
                            session.context_window
                            if isinstance(session.context_window, int)
                            else None
                        ),
                        model=last_model,
                    )
                    repaired += 1
                    click.echo("  repaired")
            elif not fix:
                click.echo(f"{session_id}: ok")

        click.echo(
            f"audited={audited} drifted={drifted} repaired={repaired}"
            if fix
            else f"audited={audited} drifted={drifted}"
        )
    finally:
        db.close()


@tokens.command("stats")
@click.option("--project", "project_ref", help="Filter to a project.")
def token_stats(project_ref: str | None) -> None:
    """Show token event ledger statistics."""
    project_id = resolve_project_ref(project_ref, exit_on_not_found=False) if project_ref else None
    db = LocalDatabase()
    try:
        run_migrations(db)
        store = TokenEventStore(db)
        breakdown = store.get_breakdown(project_id=project_id)
        total_rows = db.fetchone(
            "SELECT COUNT(*) AS count FROM token_events"
            + (" WHERE project_id = ?" if project_id else ""),
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
    finally:
        db.close()
