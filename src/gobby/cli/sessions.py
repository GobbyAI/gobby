"""
Session management CLI commands.
"""

import asyncio
import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

import click

from gobby.cli.runtime import require_cli_database
from gobby.cli.utils import resolve_project_ref, resolve_session_id
from gobby.sessions.machine_scope import (
    RemoteSessionOwnershipError,
    require_local_session_ownership,
)
from gobby.storage.attention import AttentionStateManager
from gobby.storage.projects import LocalProjectManager
from gobby.storage.sessions import SessionManager
from gobby.utils.json_helpers import json_dumps


def get_session_manager() -> SessionManager:
    """Get initialized session manager."""
    db = require_cli_database()
    return SessionManager(db)


@contextmanager
def session_manager_context() -> Iterator[SessionManager]:
    """Yield a session manager borrowing the CLI runtime database."""
    yield get_session_manager()


def _normalize_project_path(path: str) -> str:
    return str(Path(path).expanduser().resolve(strict=False))


def _resolve_project_ref_or_path(project_ref: str, manager: SessionManager) -> str:
    # SessionManager owns manager.db; LocalProjectManager only borrows it here.
    project_manager = LocalProjectManager(manager.db)

    project = project_manager.get(project_ref)
    if project:
        return project.id

    project = project_manager.get_by_name(project_ref)
    if project:
        return project.id

    from gobby.storage.project_checkouts import CheckoutNotFoundError, require_root
    from gobby.storage.workspace_machine_scope import require_local_machine_id

    target_path = _normalize_project_path(project_ref)
    for candidate in project_manager.list():
        try:
            machine_id = require_local_machine_id(
                None, resource_kind="project_checkout", resource_id=candidate.id
            )
            root = require_root(project_manager.db, candidate.id, machine_id)
        except CheckoutNotFoundError:
            continue
        if _normalize_project_path(root) == target_path:
            return candidate.id

    raise click.ClickException(f"Project not found: {project_ref}")


def _format_seq_range(values: Sequence[int | None]) -> str:
    seq_nums = [value for value in values if value is not None]
    if not seq_nums:
        return "none"
    low = min(seq_nums)
    high = max(seq_nums)
    return f"#{low}" if low == high else f"#{low}..#{high}"


def _blocked_attention_by_session(manager: SessionManager) -> dict[str, tuple[int, str]]:
    """Aggregate blocked roster entries without conflating them with lifecycle state."""
    snapshot = AttentionStateManager(manager.db).snapshot()
    reasons_by_session: dict[str, set[str]] = {}
    for state in snapshot.states:
        if state.state != "blocked" or state.session_id is None:
            continue
        reasons_by_session.setdefault(state.session_id, set()).add(
            state.reason or "Attention required"
        )
    return {
        session_id: (len(reasons), "; ".join(sorted(reasons)))
        for session_id, reasons in reasons_by_session.items()
    }


def _format_attention(summary: tuple[int, str] | None) -> str:
    if summary is None:
        return ""
    count, reason = summary
    glyph = f"!{count}" if count > 1 else "!"
    return f"{glyph} {reason}"


def _format_turns_for_llm(turns: list[dict[str, Any]]) -> str:
    """Format transcript turns for LLM analysis."""
    formatted: list[str] = []
    for i, turn in enumerate(turns):
        message = turn.get("message", {})
        role = message.get("role", "unknown")
        content = message.get("content", "")

        if isinstance(content, list):
            text_parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(str(block.get("text", "")))
                    elif block.get("type") == "tool_use":
                        text_parts.append(f"[Tool: {block.get('name', 'unknown')}]")
            content = " ".join(text_parts)

        formatted.append(f"[Turn {i + 1} - {role}]: {content}")

    return "\n\n".join(formatted)


def _append_summary_notes(markdown: str, notes: str | None) -> str:
    """Append operator notes to an archival summary artifact."""
    if not notes:
        return markdown

    stripped_notes = notes.strip()
    if not stripped_notes:
        return markdown

    return f"{markdown.rstrip()}\n\n## Notes\n{stripped_notes}"


@click.group()
def sessions() -> None:
    """Manage Gobby sessions."""
    pass


@sessions.command("list")
@click.option("--project", "-p", "project_ref", help="Filter by project (name or UUID)")
@click.option(
    "--status",
    "-s",
    type=click.Choice(["active", "completed", "handoff_ready", "expired"]),
    help="Filter by status",
)
@click.option(
    "--source",
    type=click.Choice(["claude", "grok", "qwen", "codex", "droid", "agy"]),
    help="Filter by source",
)
@click.option("--machine-id", help="Filter by client machine id")
@click.option("--limit", "-n", type=click.IntRange(min=1), default=20, help="Max sessions to show")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def list_sessions(
    project_ref: str | None,
    status: str | None,
    source: str | None,
    machine_id: str | None,
    limit: int,
    json_format: bool,
) -> None:
    """List sessions with optional filtering."""
    project_id = resolve_project_ref(project_ref) if project_ref else None
    with session_manager_context() as manager:
        sessions_list = manager.list(
            project_id=project_id,
            status=status,
            source=source,
            machine_id=machine_id,
            limit=limit,
        )
        attention_by_session = _blocked_attention_by_session(manager)

    if json_format:
        click.echo(json_dumps([s.to_dict() for s in sessions_list], indent=2, default=str))
        return

    if not sessions_list:
        click.echo("No sessions found.")
        return

    click.echo(f"Found {len(sessions_list)} sessions:\n")
    for session in sessions_list:
        status_icon = {
            "active": "●",
            "completed": "✓",
            "handoff_ready": "→",
            "expired": "○",
        }.get(session.status, "?")

        title = session.title or "(no title)"
        if len(title) > 50:
            title = title[:47] + "..."

        total_tokens = session.usage_input_tokens + session.usage_output_tokens
        tokens_str = ""
        if total_tokens > 0:
            if total_tokens >= 1_000_000:
                tokens_str = f"{total_tokens / 1_000_000:.1f}M"
            elif total_tokens >= 1_000:
                tokens_str = f"{total_tokens / 1_000:.1f}K"
            else:
                tokens_str = str(total_tokens)

        seq_str = f"#{session.seq_num}" if session.seq_num else ""
        attention_str = _format_attention(attention_by_session.get(session.id))
        click.echo(
            f"{status_icon} {seq_str:<5} {session.id[:8]}  {session.source:<12} "
            f"{attention_str:<28} {title:<40} {tokens_str}"
        )


@sessions.command("show")
@click.argument("session_id")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def show_session(session_id: str, json_format: bool) -> None:
    """Show details for a session."""
    try:
        session_id = resolve_session_id(session_id)
    except click.ClickException as e:
        raise SystemExit(1) from e

    with session_manager_context() as manager:
        session = manager.get(session_id)

        if not session:
            click.echo(f"Session not found: {session_id}", err=True)
            raise SystemExit(1)

    if json_format:
        click.echo(json_dumps(session.to_dict(), indent=2, default=str))
        return

    click.echo(f"Session: {session.id}")
    click.echo(f"Status: {session.status}")
    click.echo(f"Source: {session.source}")
    click.echo(f"Project: {session.project_id}")
    if session.title:
        click.echo(f"Title: {session.title}")
    if session.git_branch:
        click.echo(f"Branch: {session.git_branch}")
    click.echo(f"Created: {session.created_at}")
    click.echo(f"Updated: {session.updated_at}")
    if session.parent_session_id:
        click.echo(f"Parent: {session.parent_session_id}")
    if session.usage_input_tokens > 0 or session.usage_output_tokens > 0:
        click.echo("\nUsage Stats:")
        click.echo(f"  Input Tokens: {session.usage_input_tokens}")
        click.echo(f"  Output Tokens: {session.usage_output_tokens}")
        click.echo(f"  Cache Write: {session.usage_cache_creation_tokens}")
        click.echo(f"  Cache Read: {session.usage_cache_read_tokens}")

    if session.summary_markdown:
        click.echo(f"\nSummary:\n{session.summary_markdown[:500]}")
        if len(session.summary_markdown) > 500:
            click.echo("...")


@sessions.command("messages")
@click.argument("session_id")
@click.option("--limit", "-n", type=click.IntRange(min=1), default=50, help="Max messages to show")
@click.option(
    "--role",
    "-r",
    type=click.Choice(["user", "assistant", "tool"]),
    help="Filter by role",
)
@click.option("--offset", "-o", default=0, help="Skip first N messages")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def show_messages(
    session_id: str,
    limit: int,
    role: str | None,
    offset: int,
    json_format: bool,
) -> None:
    """Show messages for a session."""
    try:
        session_id = resolve_session_id(session_id)
    except click.ClickException as e:
        raise SystemExit(1) from e

    with session_manager_context() as session_manager:
        # Resolve session ID
        session = session_manager.get(session_id)
        if not session:
            raise click.ClickException(f"Session not found: {session_id}")

        # Fetch messages (live JSONL + gzip archive fallback)
        from gobby.sessions.transcript_reader import TranscriptReader

        reader = TranscriptReader(session_manager)
        messages = asyncio.run(
            reader.get_messages(
                session_id=session.id,
                limit=limit,
                offset=offset,
                role=role,
            )
        )
        total = (
            None if json_format or not messages else asyncio.run(reader.count_messages(session.id))
        )

    if json_format:
        click.echo(json_dumps(messages, indent=2, default=str))
        return

    if not messages:
        click.echo("No messages found.")
        return

    click.echo(f"Messages for session {session.id[:12]} ({len(messages)}/{total}):\n")

    for msg in messages:
        role_icon = {"user": "👤", "assistant": "🤖", "tool": "🔧"}.get(msg["role"], "?")
        content = msg.get("content") or ""

        if msg.get("tool_name"):
            click.echo(f"{role_icon} [{msg['message_index']}] {msg['role']}: {msg['tool_name']}")
        else:
            # Truncate long content
            if len(content) > 200:
                content = content[:197] + "..."
            click.echo(f"{role_icon} [{msg['message_index']}] {msg['role']}: {content}")


@sessions.command("delete")
@click.argument("session_id")
@click.confirmation_option(prompt="Are you sure you want to delete this session?")
def delete_session(session_id: str) -> None:
    """Delete a session."""
    try:
        session_id = resolve_session_id(session_id)
    except click.ClickException as e:
        raise SystemExit(1) from e

    with session_manager_context() as manager:
        session = manager.get(session_id)
        if not session:
            click.echo(f"Session not found: {session_id}", err=True)
            raise SystemExit(1)

        success = manager.delete(session.id)
    if success:
        click.echo(f"Deleted session: {session.id}")
    else:
        raise click.ClickException(f"Failed to delete session: {session.id}")


@sessions.command("stats")
@click.option("--project", "-p", "project_ref", help="Filter by project (name or UUID)")
def session_stats(project_ref: str | None) -> None:
    """Show session statistics."""
    project_id = resolve_project_ref(project_ref) if project_ref else None

    with session_manager_context() as manager:
        sessions_list = manager.list(project_id=project_id, limit=10000)

    if not sessions_list:
        click.echo("No sessions found.")
        return

    # Count by status
    by_status: dict[str, int] = {}
    by_source: dict[str, int] = {}

    for session in sessions_list:
        by_status[session.status] = by_status.get(session.status, 0) + 1
        by_source[session.source] = by_source.get(session.source, 0) + 1

    click.echo("Session Statistics:")
    click.echo(f"  Total Sessions: {len(sessions_list)}")

    click.echo("\n  By Status:")
    for status, count in sorted(by_status.items()):
        click.echo(f"    {status}: {count}")

    click.echo("\n  By Source:")
    for source, count in sorted(by_source.items()):
        click.echo(f"    {source}: {count}")


@sessions.command("renumber")
@click.option("--project", "-p", "project_ref", required=True, help="Project name, UUID, or path")
@click.option("--apply", "apply_changes", is_flag=True, help="Write the new dense refs.")
def renumber_sessions(project_ref: str, apply_changes: bool) -> None:
    """Compact per-project session refs."""
    with session_manager_context() as manager:
        project_id = _resolve_project_ref_or_path(project_ref, manager)
        mapping = manager.renumber_project_sessions(project_id, dry_run=not apply_changes)

    mode = "Applied" if apply_changes else "Dry run"
    click.echo(f"{mode}: scanned {len(mapping)} session(s) for project {project_id}.")
    click.echo(f"Mapping count: {len(mapping)}")

    if not mapping:
        return

    changed = sum(1 for item in mapping if item["old_seq_num"] != item["new_seq_num"])
    old_range = _format_seq_range([item["old_seq_num"] for item in mapping])
    new_seq_nums = [item["new_seq_num"] for item in mapping if item["new_seq_num"] is not None]
    new_range = _format_seq_range(new_seq_nums)
    max_ref = max(new_seq_nums) if new_seq_nums else None

    click.echo(f"Changed refs: {changed}")
    click.echo(f"Old ref range: {old_range}")
    click.echo(f"New ref range: {new_range}")
    click.echo(f"Final max ref: #{max_ref}" if max_ref is not None else "Final max ref: -")
    if not apply_changes:
        click.echo("No changes written. Re-run with --apply to mutate refs.")


@sessions.command("summarize")
@click.option("--session-id", "-s", help="Session ID (defaults to current active session)")
@click.option(
    "--output",
    type=click.Choice(["db", "file", "all"]),
    default="all",
    help="Where to save: db only, file only, or all (both)",
)
@click.option(
    "--path",
    "output_path",
    default=".gobby/session_summaries/",
    help="Directory path for file output",
)
@click.option("--notes", "notes_option", help="Additional notes to include in the summary")
@click.argument("notes_arg", required=False)
def summarize_session(
    session_id: str | None,
    output: str,
    output_path: str,
    notes_option: str | None,
    notes_arg: str | None,
) -> None:
    """Create a transcript-based archival summary for a session.

    Extracts structured context from the session transcript:
    - Active gobby-task
    - TodoWrite state
    - Files modified
    - Git commits and status
    - Initial goal
    - Recent activity

    Generates an LLM-powered summary with code-only fallback on failure.

    Output destinations:
    - db: Save to database only
    - file: Write to file only (in --path directory)
    - all: Save to both database and file

    File output: summary saved as session_*.md.

    If no session ID is provided, uses the current project's most recent active session.
    """
    import subprocess  # nosec B404 # subprocess needed for git commands
    import time
    from pathlib import Path

    from gobby.sessions.analyzer import TranscriptAnalyzer
    from gobby.sessions.formatting import format_handoff_as_markdown
    from gobby.sessions.transcripts import get_parser

    operator_notes = notes_option if notes_option is not None else notes_arg

    # Find session
    with session_manager_context() as manager:
        if session_id:
            try:
                session_id = resolve_session_id(session_id)
            except click.ClickException as e:
                raise SystemExit(1) from e
            session = manager.get(session_id)
            if not session:
                click.echo(f"Session not found: {session_id}", err=True)
                raise SystemExit(1)
        else:
            # Get most recent active session
            try:
                session_id = resolve_session_id(None)  # uses get_active_session_id internally
            except click.ClickException as e:
                raise SystemExit(1) from e
            session = manager.get(session_id)
            if not session:
                click.echo(f"Session not found: {session_id}", err=True)
                raise SystemExit(1)

    try:
        require_local_session_ownership(session)
    except RemoteSessionOwnershipError as exc:
        click.echo(str(exc), err=True)
        raise SystemExit(1) from exc

    # Check for transcript
    if not session.transcript_path:
        click.echo(f"Session {session.id[:12]} has no transcript path.", err=True)
        return

    path = Path(session.transcript_path)
    if not path.exists():
        click.echo(f"Transcript file not found: {path}", err=True)
        return

    # Read and parse transcript
    turns = []
    with open(path) as f:
        for line_num, line in enumerate(f, start=1):
            if line.strip():
                try:
                    turns.append(json.loads(line))
                except json.JSONDecodeError as e:
                    snippet = line[:50] + "..." if len(line) > 50 else line.strip()
                    click.echo(
                        f"Warning: Skipping malformed JSON at line {line_num}: {e} ({snippet})",
                        err=True,
                    )
                    continue

    if not turns:
        click.echo("Transcript is empty.", err=True)
        return

    analyzer = TranscriptAnalyzer(
        get_parser(
            session.source,
            session_id=session.id,
            transcript_path=path,
        )
    )
    handoff_ctx = analyzer.extract_handoff_context(turns)

    # Determine the git working directory from the session-machine checkout
    git_cwd = path.parent
    if session.project_id:
        from gobby.storage.project_checkouts import CheckoutNotFoundError, require_root
        from gobby.storage.workspace_machine_scope import require_local_machine_id

        db = require_cli_database()
        try:
            machine_id = require_local_machine_id(
                getattr(session, "machine_id", None),
                resource_kind="project_checkout",
                resource_id=session.project_id,
            )
            project_repo = Path(require_root(db, session.project_id, machine_id))
            if project_repo.exists():
                git_cwd = project_repo
        except CheckoutNotFoundError:
            pass

    # Enrich with real-time git status
    if not handoff_ctx.git_status:
        try:
            result = subprocess.run(  # nosec B603 B607 # hardcoded git command
                ["git", "status", "--short"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=git_cwd,
            )
            handoff_ctx.git_status = result.stdout.strip() if result.returncode == 0 else ""
        except Exception as e:
            import logging

            logging.getLogger(__name__).debug("Git status is optional, failed: %s", e)

    # Get recent git commits
    try:
        result = subprocess.run(  # nosec B603 B607 # hardcoded git command
            ["git", "log", "--oneline", "-10", "--format=%H|%s"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=git_cwd,
        )
        if result.returncode == 0:
            commits = []
            for line in result.stdout.strip().split("\n"):
                if "|" in line:
                    hash_val, message = line.split("|", 1)
                    commits.append({"hash": hash_val, "message": message})
            if commits:
                handoff_ctx.git_commits = commits
    except Exception as e:
        import logging

        logging.getLogger(__name__).debug("Git log is optional, failed: %s", e)

    # Generate full summary via shared function
    full_markdown = None
    try:
        from gobby.cli.runtime import get_cli_runtime
        from gobby.llm.factory import create_llm_service
        from gobby.sessions.summarize import generate_session_summaries

        config = get_cli_runtime().require_config()
        llm_service = create_llm_service(config)

        with session_manager_context() as summary_manager:

            async def _gen_summary() -> dict[str, Any]:
                return await generate_session_summaries(
                    session_id=session.id,
                    session_manager=summary_manager,
                    llm_service=llm_service,
                    session_summary_config=config.session_summary,
                    db=summary_manager.db,
                    set_handoff_ready=False,
                )

            summary_result = asyncio.run(_gen_summary())
            if summary_result.get("success") and summary_result.get("full_length", 0) > 0:
                updated = summary_manager.get(session.id)
                if updated:
                    full_markdown = updated.summary_markdown
        if not full_markdown:
            click.echo(
                f"Warning: LLM summary failed ({summary_result.get('full_error') or summary_result.get('error', 'unknown')}), using code-only fallback",
                err=True,
            )
            full_markdown = format_handoff_as_markdown(handoff_ctx)
    except Exception as e:
        click.echo(f"Warning: LLM summary failed ({e}), using code-only fallback", err=True)
        full_markdown = format_handoff_as_markdown(handoff_ctx)

    if full_markdown:
        full_markdown = _append_summary_notes(full_markdown, operator_notes)

    # Determine what to save
    save_to_db = output in ("db", "all")
    save_to_file = output in ("file", "all")

    # Save to database
    if save_to_db and full_markdown:
        with session_manager_context() as manager:
            manager.update_summary(session.id, summary_markdown=full_markdown)
        click.echo(f"Saved summary to database: {len(full_markdown)} chars")

    # Save to file
    files_written = []
    if save_to_file and full_markdown:
        try:
            summary_dir = Path(output_path).expanduser()
            summary_dir.mkdir(parents=True, exist_ok=True)
            timestamp = int(time.time())

            full_file = summary_dir / f"session_{timestamp}_{session.id[:12]}.md"
            full_file.write_text(full_markdown, encoding="utf-8")
            files_written.append(str(full_file))
            click.echo(f"Saved summary to file: {full_file}")
        except Exception as e:
            click.echo(f"Error writing file: {e}", err=True)

    # Output summary
    click.echo(f"\nCreated handoff context for session {session.id[:12]}")
    click.echo(f"  Output: {output}")
    if full_markdown:
        click.echo(f"  Summary length: {len(full_markdown)} chars")
    click.echo(f"  Active task: {'Yes' if handoff_ctx.active_gobby_task else 'No'}")
    click.echo(f"  Files modified: {len(handoff_ctx.files_modified)}")
    click.echo(f"  Git commits: {len(handoff_ctx.git_commits)}")
    click.echo(f"  Initial goal: {'Yes' if handoff_ctx.initial_goal else 'No'}")

    if operator_notes:
        click.echo(f"  Notes: {operator_notes[:50]}{'...' if len(operator_notes) > 50 else ''}")
    for file_path in files_written:
        click.echo(f"  File: {file_path}")


@sessions.command("restore")
@click.argument("session_ref", required=False)
@click.option("--all", "restore_all", is_flag=True, help="Restore all sessions with archives")
@click.option("--path", "-p", "target_path", help="Override target path for restore")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def restore_transcript(
    session_ref: str | None,
    restore_all: bool,
    target_path: str | None,
    json_format: bool,
) -> None:
    """Restore session transcript(s) from gzip archives to disk.

    Useful when the CLI has purged the original transcript file
    but you want to resume the session.
    """
    from gobby.sessions.transcript_archive import get_archive_dir
    from gobby.sessions.transcript_archive import restore_transcript as _restore
    from gobby.storage.sessions import SessionManager

    if not session_ref and not restore_all:
        raise click.UsageError("Provide a session reference or use --all")

    with nullcontext(require_cli_database()) as db:
        sm = SessionManager(db)
        results: list[dict[str, Any]] = []

        if restore_all:
            archive_dir = get_archive_dir()
            for archive_file in archive_dir.glob("*.jsonl.gz"):
                external_id = archive_file.stem.replace(".jsonl", "")
                # Look up session by external_id
                row = db.fetchone(
                    "SELECT id, transcript_path FROM sessions WHERE external_id = %s",
                    (external_id,),
                )
                if not row or not row["transcript_path"]:
                    results.append(
                        {
                            "external_id": external_id,
                            "status": "skipped",
                            "reason": "no session/path",
                        }
                    )
                    continue
                restored = _restore(external_id, row["transcript_path"])
                if restored:
                    results.append(
                        {
                            "session_id": row["id"],
                            "path": row["transcript_path"],
                            "status": "restored",
                        }
                    )
                else:
                    results.append(
                        {"session_id": row["id"], "status": "skipped", "reason": "original exists"}
                    )
        else:
            assert session_ref is not None
            resolved_id = resolve_session_id(session_ref)
            session = sm.get(resolved_id)
            if not session or not session.external_id:
                click.echo("Session not found or missing external_id.", err=True)
                raise SystemExit(1)
            restore_path = target_path or session.transcript_path
            if not restore_path:
                click.echo("No transcript_path for session.", err=True)
                raise SystemExit(1)
            restored = _restore(session.external_id, restore_path)
            if restored:
                results.append(
                    {"session_id": resolved_id, "path": restore_path, "status": "restored"}
                )
            else:
                click.echo("No archive found or original file still exists.", err=True)
                raise SystemExit(1)

    if json_format:
        click.echo(json_dumps(results, indent=2, default=str))
        return

    restored_list = [r for r in results if r["status"] == "restored"]
    skipped = [r for r in results if r["status"] == "skipped"]

    for r in restored_list:
        sid = r.get("session_id", r.get("external_id", "?"))
        click.echo(f"Restored {sid[:12]} -> {r['path']}")

    if skipped:
        click.echo(f"\nSkipped {len(skipped)} session(s)")
        for r in skipped:
            display_ref = r.get("session_id") or r.get("external_id") or "?"
            click.echo(f"  {str(display_ref)[:12]}: {r.get('reason', 'skipped')}")

    click.echo(f"\nRestored {len(restored_list)} transcript(s)")


@sessions.command("backfill-context-windows")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Report what would change without writing any updates.",
)
def backfill_context_windows(dry_run: bool) -> None:
    """Recompute under-counted session context windows and usage ratios.

    Re-resolves each session's context window from its model and bumps any
    under-sized window (e.g. a 1M-context Opus stored at 200k) upward,
    recomputing the usage ratio. Larger windows are never shrunk.
    """
    from gobby.cli.runtime import get_cli_runtime
    from gobby.sessions.context_usage import backfill_session_context_windows

    overrides = get_cli_runtime().require_config().context_window_overrides
    with session_manager_context() as manager:
        result = backfill_session_context_windows(
            manager.db,
            dry_run=dry_run,
            overrides=overrides,
        )

    verb = "Would update" if dry_run else "Updated"
    click.echo(
        f"{verb} {result.updated} of {result.scanned} session(s) ({result.skipped} unchanged)."
    )
