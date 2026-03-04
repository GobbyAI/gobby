"""
Session management CLI commands.
"""

import asyncio
import json
from typing import Any

import click

from gobby.cli.utils import resolve_project_ref, resolve_session_id
from gobby.storage.database import LocalDatabase
from gobby.storage.session_messages import LocalSessionMessageManager
from gobby.storage.sessions import LocalSessionManager


def get_session_manager() -> LocalSessionManager:
    """Get initialized session manager."""
    db = LocalDatabase()
    return LocalSessionManager(db)


def get_message_manager() -> LocalSessionMessageManager:
    """Get initialized message manager."""
    db = LocalDatabase()
    return LocalSessionMessageManager(db)


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


@click.group()
def sessions() -> None:
    """Manage Gobby sessions."""
    pass


@sessions.command("list")
@click.option("--project", "-p", "project_ref", help="Filter by project (name or UUID)")
@click.option("--status", "-s", help="Filter by status (active, completed, handoff_ready)")
@click.option(
    "--source", help="Filter by source (claude, gemini, codex, cursor, windsurf, copilot)"
)
@click.option("--limit", "-n", default=20, help="Max sessions to show")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def list_sessions(
    project_ref: str | None,
    status: str | None,
    source: str | None,
    limit: int,
    json_format: bool,
) -> None:
    """List sessions with optional filtering."""
    project_id = resolve_project_ref(project_ref) if project_ref else None
    manager = get_session_manager()
    sessions_list = manager.list(
        project_id=project_id,
        status=status,
        source=source,
        limit=limit,
    )

    if json_format:
        click.echo(json.dumps([s.to_dict() for s in sessions_list], indent=2, default=str))
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

        cost_str = ""
        if session.usage_total_cost_usd > 0:
            cost_str = f"${session.usage_total_cost_usd:.2f}"

        seq_str = f"#{session.seq_num}" if session.seq_num else ""
        click.echo(
            f"{status_icon} {seq_str:<5} {session.id[:8]}  {session.source:<12} {title:<40} {cost_str}"
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

    manager = get_session_manager()
    session = manager.get(session_id)

    if not session:
        click.echo(f"Session not found: {session_id}", err=True)
        return

    if json_format:
        click.echo(json.dumps(session.to_dict(), indent=2, default=str))
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
        click.echo(f"  Total Cost: ${session.usage_total_cost_usd:.4f}")

    if session.summary_markdown:
        click.echo(f"\nSummary:\n{session.summary_markdown[:500]}")
        if len(session.summary_markdown) > 500:
            click.echo("...")


@sessions.command("messages")
@click.argument("session_id")
@click.option("--limit", "-n", default=50, help="Max messages to show")
@click.option("--role", "-r", help="Filter by role (user, assistant, tool)")
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

    session_manager = get_session_manager()
    message_manager = get_message_manager()

    # Resolve session ID
    session = session_manager.get(session_id)
    if not session:
        click.echo(f"Session not found: {session_id}", err=True)
        return

    # Fetch messages
    messages = asyncio.run(
        message_manager.get_messages(
            session_id=session.id,
            limit=limit,
            offset=offset,
            role=role,
        )
    )

    if json_format:
        click.echo(json.dumps(messages, indent=2, default=str))
        return

    if not messages:
        click.echo("No messages found.")
        return

    total = asyncio.run(message_manager.count_messages(session.id))
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


@sessions.command("search")
@click.argument("query")
@click.option("--session", "-s", "session_id", help="Search within specific session")
@click.option("--project", "-p", "project_ref", help="Search within project (name or UUID)")
@click.option("--limit", "-n", default=20, help="Max results")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def search_messages(
    query: str,
    session_id: str | None,
    project_ref: str | None,
    limit: int,
    json_format: bool,
) -> None:
    """Search messages across sessions."""
    if session_id:
        try:
            session_id = resolve_session_id(session_id)
        except click.ClickException as e:
            raise SystemExit(1) from e

    project_id = resolve_project_ref(project_ref) if project_ref else None
    message_manager = get_message_manager()

    results = asyncio.run(
        message_manager.search_messages(
            query_text=query,
            limit=limit,
            session_id=session_id,
            project_id=project_id,
        )
    )

    if json_format:
        click.echo(json.dumps(results, indent=2, default=str))
        return

    if not results:
        click.echo(f"No messages found matching '{query}'")
        return

    click.echo(f"Found {len(results)} messages matching '{query}':\n")

    for msg in results:
        content = msg.get("content") or ""
        if len(content) > 100:
            content = content[:97] + "..."

        session_short = msg["session_id"][:8]
        role_icon = {"user": "👤", "assistant": "🤖", "tool": "🔧"}.get(msg["role"], "?")
        click.echo(f"{role_icon} [{session_short}] {content}")


@sessions.command("delete")
@click.argument("session_id")
@click.confirmation_option(prompt="Are you sure you want to delete this session?")
def delete_session(session_id: str) -> None:
    """Delete a session."""
    try:
        session_id = resolve_session_id(session_id)
    except click.ClickException as e:
        raise SystemExit(1) from e

    manager = get_session_manager()
    session = manager.get(session_id)
    if not session:
        click.echo(f"Session not found: {session_id}", err=True)
        return

    success = manager.delete(session.id)
    if success:
        click.echo(f"Deleted session: {session.id}")
    else:
        click.echo(f"Failed to delete session: {session.id}", err=True)


@sessions.command("stats")
@click.option("--project", "-p", "project_ref", help="Filter by project (name or UUID)")
def session_stats(project_ref: str | None) -> None:
    """Show session statistics."""
    project_id = resolve_project_ref(project_ref) if project_ref else None
    manager = get_session_manager()
    message_manager = get_message_manager()

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

    # Get message counts
    message_counts = asyncio.run(message_manager.get_all_counts())
    total_messages = sum(message_counts.values())

    click.echo("Session Statistics:")
    click.echo(f"  Total Sessions: {len(sessions_list)}")
    click.echo(f"  Total Messages: {total_messages}")

    click.echo("\n  By Status:")
    for status, count in sorted(by_status.items()):
        click.echo(f"    {status}: {count}")

    click.echo("\n  By Source:")
    for source, count in sorted(by_source.items()):
        click.echo(f"    {source}: {count}")


@sessions.command("create-handoff")
@click.option("--session-id", "-s", help="Session ID (defaults to current active session)")
@click.option("--compact", "-c", is_flag=True, default=False, help="Generate compact summary only")
@click.option(
    "--full", "full_summary", is_flag=True, default=False, help="Generate full LLM summary only"
)
@click.option(
    "--output",
    type=click.Choice(["db", "file", "all"]),
    default="all",
    help="Where to save: db only, file only, or all (both)",
)
@click.option(
    "--path",
    "output_path",
    default="~/.gobby/session_summaries/",
    help="Directory path for file output",
)
@click.argument("notes", required=False)
def create_handoff(
    session_id: str | None,
    compact: bool,
    full_summary: bool,
    output: str,
    output_path: str,
    notes: str | None,
) -> None:
    """Create handoff context for a session.

    Extracts structured context from the session transcript:
    - Active gobby-task
    - TodoWrite state
    - Files modified
    - Git commits and status
    - Initial goal
    - Recent activity

    Summary types:
    - --compact: Fast structured extraction using TranscriptAnalyzer
    - --full: LLM-powered comprehensive summary
    - Neither flag: Generate both (default)

    Output destinations:
    - db: Save to database only
    - file: Write to file only (in --path directory)
    - all: Save to both database and file

    File output: full summary saved as session_*.md, compact as session_compact_*.md.

    If no session ID is provided, uses the current project's most recent active session.
    """
    import subprocess  # nosec B404 - subprocess needed for git commands
    import time
    from pathlib import Path

    from gobby.sessions.analyzer import TranscriptAnalyzer
    from gobby.sessions.formatting import format_handoff_as_markdown

    manager = get_session_manager()

    # Find session
    if session_id:
        try:
            session_id = resolve_session_id(session_id)
        except click.ClickException as e:
            raise SystemExit(1) from e
        session = manager.get(session_id)
        if not session:
            click.echo(f"Session not found: {session_id}", err=True)
            return
    else:
        # Get most recent active session
        try:
            session_id = resolve_session_id(None)  # uses get_active_session_id internally
        except click.ClickException as e:
            raise SystemExit(1) from e
        session = manager.get(session_id)
        if not session:
            click.echo(f"Session not found: {session_id}", err=True)
            return

    # Check for transcript
    if not session.jsonl_path:
        click.echo(f"Session {session.id[:12]} has no transcript path.", err=True)
        return

    path = Path(session.jsonl_path)
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

    # Analyze transcript
    analyzer = TranscriptAnalyzer()
    handoff_ctx = analyzer.extract_handoff_context(turns)

    # Determine the git working directory - prefer project repo_path, fall back to transcript parent
    git_cwd = path.parent
    if session.project_id:
        from gobby.storage.projects import LocalProjectManager

        project_manager = LocalProjectManager(LocalDatabase())
        project = project_manager.get(session.project_id)
        if project and project.repo_path:
            project_repo = Path(project.repo_path)
            if project_repo.exists():
                git_cwd = project_repo

    # Enrich with real-time git status
    if not handoff_ctx.git_status:
        try:
            result = subprocess.run(  # nosec B603 B607 - hardcoded git command
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
        result = subprocess.run(  # nosec B603 B607 - hardcoded git command
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

    # Determine what to generate (neither flag = both)
    generate_compact = not full_summary or compact  # generate if --compact or neither flag
    generate_full = not compact or full_summary  # generate if --full or neither flag

    # Generate content
    compact_markdown = None
    full_markdown = None

    if generate_compact:
        # Try LLM-powered compact summary first, fall back to code-only
        try:
            from gobby.sessions.summarize import generate_session_summaries

            _compact_db = LocalDatabase()

            async def _gen_compact() -> dict[str, Any]:
                return await generate_session_summaries(
                    session_id=session.id,
                    session_manager=manager,
                    db=_compact_db,
                    compact_only=True,
                    set_handoff_ready=False,
                )

            try:
                compact_result = asyncio.run(_gen_compact())
            finally:
                _compact_db.close()
            if compact_result.get("success") and compact_result.get("compact_length", 0) > 0:
                updated = manager.get(session.id)
                if updated:
                    compact_markdown = updated.compact_markdown
            if not compact_markdown:
                click.echo(
                    f"Warning: LLM compact summary failed ({compact_result.get('full_error') or compact_result.get('error', 'unknown')}), using code-only fallback",
                    err=True,
                )
                compact_markdown = format_handoff_as_markdown(handoff_ctx)
        except Exception as e:
            click.echo(f"Warning: LLM compact failed ({e}), using code-only fallback", err=True)
            compact_markdown = format_handoff_as_markdown(handoff_ctx)

    if generate_full:
        # Generate LLM-powered full summary
        try:
            from gobby.config.app import load_config
            from gobby.llm.claude import ClaudeLLMProvider
            from gobby.sessions.transcripts.claude import ClaudeTranscriptParser

            config = load_config()
            provider = ClaudeLLMProvider(config)
            transcript_parser = ClaudeTranscriptParser()

            # Get prompt template: try PromptLoader first, then config fallback
            prompt_template = None
            try:
                from gobby.prompts.loader import PromptLoader

                loader = PromptLoader(db=LocalDatabase())
                prompt_obj = loader.load("handoff/session_end")
                prompt_template = prompt_obj.content
            except FileNotFoundError:
                pass
            except Exception as e:
                click.echo(f"Warning: Failed to load prompt template: {e}", err=True)

            if not prompt_template:
                click.echo(
                    "Warning: No prompt template found for handoff/session_end",
                    err=True,
                )
                # Only fail if --full was explicitly requested without --compact
                if full_summary and not compact:
                    return
                # Otherwise, skip full generation but continue with compact
            else:
                # Prepare context for LLM
                last_turns = transcript_parser.extract_turns_since_clear(turns, max_turns=50)
                last_messages = transcript_parser.extract_last_messages(turns, num_pairs=2)

                context = {
                    "transcript_summary": _format_turns_for_llm(last_turns),
                    "last_messages": last_messages,
                    "git_status": handoff_ctx.git_status or "",
                    "file_changes": "",
                    "external_id": session.id[:12],
                    "session_id": session.id,
                    "session_source": session.source,
                }

                import anyio

                async def _generate() -> str:
                    return await provider.generate_summary(context, prompt_template=prompt_template)

                full_markdown = anyio.run(_generate)

        except Exception as e:
            click.echo(f"Warning: Failed to generate full summary: {e}", err=True)
            if full_summary and not compact:
                # Only --full was requested and it failed
                return

    # Determine what to save
    save_to_db = output in ("db", "all")
    save_to_file = output in ("file", "all")

    # Save to database - always save both compact and full when available
    if save_to_db:
        if compact_markdown:
            manager.update_compact_markdown(session.id, compact_markdown)
            click.echo(f"Saved compact to database: {len(compact_markdown)} chars")
        if full_markdown:
            manager.update_summary(session.id, summary_markdown=full_markdown)
            click.echo(f"Saved full to database: {len(full_markdown)} chars")

    # Save to file
    files_written = []
    if save_to_file:
        try:
            summary_dir = Path(output_path).expanduser()
            summary_dir.mkdir(parents=True, exist_ok=True)
            timestamp = int(time.time())

            # Write full summary as session_*.md
            if full_markdown:
                full_file = summary_dir / f"session_{timestamp}_{session.id[:12]}.md"
                full_file.write_text(full_markdown, encoding="utf-8")
                files_written.append(str(full_file))
                click.echo(f"Saved full to file: {full_file}")

            # Write compact summary as session_compact_*.md
            if compact_markdown:
                compact_file = summary_dir / f"session_compact_{timestamp}_{session.id[:12]}.md"
                compact_file.write_text(compact_markdown, encoding="utf-8")
                files_written.append(str(compact_file))
                click.echo(f"Saved compact to file: {compact_file}")

        except Exception as e:
            click.echo(f"Error writing file: {e}", err=True)

    # Output summary
    summary_type = "none"
    if compact_markdown and full_markdown:
        summary_type = "both"
    elif compact_markdown:
        summary_type = "compact"
    elif full_markdown:
        summary_type = "full"
    click.echo(f"\nCreated handoff context for session {session.id[:12]}")
    click.echo(f"  Type: {summary_type}")
    click.echo(f"  Output: {output}")
    if compact_markdown:
        click.echo(f"  Compact length: {len(compact_markdown)} chars")
    if full_markdown:
        click.echo(f"  Full length: {len(full_markdown)} chars")
    click.echo(f"  Active task: {'Yes' if handoff_ctx.active_gobby_task else 'No'}")
    click.echo(f"  Files modified: {len(handoff_ctx.files_modified)}")
    click.echo(f"  Git commits: {len(handoff_ctx.git_commits)}")
    click.echo(f"  Initial goal: {'Yes' if handoff_ctx.initial_goal else 'No'}")

    if notes:
        click.echo(f"  Notes: {notes[:50]}{'...' if len(notes) > 50 else ''}")
    for file_path in files_written:
        click.echo(f"  File: {file_path}")
