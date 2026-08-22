"""
Agent management CLI commands.

Commands for managing subagent runs:
- spawn: Spawn a new agent
- list: List agent definitions
- show: Show details for an agent definition
- runs list: List agent runs for a session
- runs show: Show details for an agent run
- status: Check status of a running agent
- stop: Stop a running agent and cancel the run
- kill: Kill a running agent process (SIGTERM/SIGKILL)
"""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import click
import httpx

from gobby.cli.agents_steps import check_agent, list_agent_steps
from gobby.cli.runtime import require_cli_database
from gobby.cli.utils import resolve_session_id
from gobby.cli.utils_config import get_daemon_client, get_daemon_url
from gobby.storage.agents import AgentRunStatus, LocalAgentRunManager
from gobby.storage.definitions.agents import AgentDefinitionManager, AgentDefinitionRow
from gobby.storage.hub.protocol import HubDatabase
from gobby.storage.sql_dialect import older_than_now_expr
from gobby.utils.json_helpers import json_dumps
from gobby.utils.local_token import daemon_auth_headers
from gobby.utils.uuid_validation import is_full_uuid
from gobby.workflows.definitions import AgentDefinitionBody


@contextmanager
def _runtime_db_context() -> Iterator[HubDatabase]:
    """Yield the database borrowed from the current CLI runtime."""
    yield require_cli_database()


def get_agent_run_manager() -> LocalAgentRunManager:
    """Get initialized agent run manager."""
    return LocalAgentRunManager(require_cli_database())


@contextmanager
def agent_run_manager_context() -> Iterator[LocalAgentRunManager]:
    """Yield an agent run manager borrowing the CLI database."""
    yield get_agent_run_manager()


def get_agent_definition_manager() -> AgentDefinitionManager:
    """Get initialized typed manager for agent definitions."""
    return AgentDefinitionManager(require_cli_database())


@contextmanager
def agent_definition_manager_context() -> Iterator[AgentDefinitionManager]:
    """Yield an agent definition manager borrowing the CLI database."""
    yield get_agent_definition_manager()


def _escape_like_prefix(prefix: str) -> str:
    """Escape SQL LIKE wildcard characters in an ID prefix."""
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _agent_body(row: AgentDefinitionRow) -> tuple[AgentDefinitionBody, dict[str, Any]]:
    """Parse and validate an agent definition row."""
    data = row.definition_json
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Agent definition '{row.name}' is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"Agent definition '{row.name}' is not a JSON object")
        data = parsed
    elif not isinstance(data, dict):
        raise ValueError(f"Agent definition '{row.name}' is not a JSON object")
    payload = dict(data)
    payload.setdefault("name", row.name)
    body = AgentDefinitionBody.model_validate(payload)
    return body, payload


def _agent_definition_summary(row: AgentDefinitionRow) -> dict[str, Any]:
    """Build an agent definition summary for CLI output."""
    body, raw = _agent_body(row)
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description or body.description,
        "provider": body.provider,
        "model": body.model,
        "mode": raw.get("mode"),
        "isolation": body.isolation,
        "surfaces": body.surfaces,
        "has_steps": body.step_workflow is not None,
        "step_count": len(body.step_workflow.steps) if body.step_workflow else 0,
        "enabled": row.enabled,
        "source": row.source,
        "project_id": row.project_id,
    }


def _agent_definition_detail(row: AgentDefinitionRow) -> dict[str, Any]:
    """Build detailed agent definition output."""
    body, raw = _agent_body(row)
    return {
        **_agent_definition_summary(row),
        "base_branch": body.base_branch,
        "timeout": body.timeout,
        "prompts": body.prompts.model_dump(exclude_none=True),
        "workflows": body.workflows.model_dump(exclude_none=True),
        "step_workflow": (
            body.step_workflow.model_dump(exclude_none=True) if body.step_workflow else None
        ),
        "blocked_tools": body.blocked_tools,
        "blocked_mcp_tools": body.blocked_mcp_tools,
        "sources": body.sources,
        "tags": row.tags,
        "raw_mode": raw.get("mode"),
    }


def resolve_agent_run_id(run_ref: str) -> str:
    """
    Resolve agent run reference (exact or prefix) to full ID.

    Args:
        run_ref: Agent run ID or prefix

    Returns:
        Full UUID string

    Raises:
        click.ClickException: If run not found or ambiguous
    """
    run_ref = run_ref.strip()
    if not run_ref:
        raise click.ClickException("Agent run reference cannot be empty")

    # Try exact UUID matches before prefix lookup. Avoid opening the manager for
    # short prefixes; tests commonly patch one runtime DB and prefix lookup must
    # not see a connection already closed by the exact-match path.
    if is_full_uuid(run_ref):
        with agent_run_manager_context() as manager:
            if manager.get(run_ref):
                return run_ref

    with _runtime_db_context() as db:
        rows = db.fetchall(
            "SELECT id FROM agent_runs WHERE id::text LIKE %s ESCAPE '\\' LIMIT 5",
            (f"{_escape_like_prefix(run_ref)}%",),
        )

    if not rows:
        raise click.ClickException(f"Agent run not found: {run_ref}")

    if len(rows) > 1:
        click.echo(f"Ambiguous agent run reference '{run_ref}' matches:", err=True)
        for row in rows:
            click.echo(f"  {row['id']}", err=True)
        raise click.ClickException(f"Ambiguous agent run reference: {run_ref}")

    return str(rows[0]["id"])


@click.group()
def agents() -> None:
    """Manage agent definitions and runs."""
    pass


@agents.command("spawn")
@click.argument("prompt")
@click.option("--session", "-s", "parent_session_id", required=True, help="Parent session ID")
@click.option("--workflow", "-w", help="Workflow name to execute")
@click.option("--task", "-t", help="Task ID or 'next' for auto-select")
@click.option(
    "--terminal",
    type=click.Choice(["auto", "ghostty", "iterm", "kitty", "wezterm", "terminal"]),
    default="auto",
    help="Terminal for terminal mode",
)
@click.option("--provider", "-p", default="claude", help="LLM provider (claude, qwen, etc.)")
@click.option("--model", help="Model override")
@click.option("--fast", is_flag=True, help="Request the provider's fast route")
@click.option(
    "--reasoning-effort", help="Reasoning effort override (e.g. low, medium, high, xhigh)"
)
@click.option(
    "--reasoning-required/--no-reasoning-required",
    default=False,
    help="Fail instead of warning when the requested reasoning is unsupported",
)
@click.option("--timeout", default=120.0, help="Execution timeout in seconds")
@click.option(
    "--context",
    "-c",
    "session_context",
    default="summary_markdown",
    help="Context source (summary_markdown, transcript:<n>, file:<path>)",
)
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def spawn_agent_cmd(
    prompt: str,
    parent_session_id: str,
    workflow: str | None,
    task: str | None,
    terminal: str,
    provider: str,
    model: str | None,
    fast: bool,
    reasoning_effort: str | None,
    reasoning_required: bool,
    timeout: float,
    session_context: str,
    json_format: bool,
) -> None:
    """Spawn a new agent with the given prompt.

    Examples:

        gobby agents spawn "Implement feature X" --session sess-abc123

        gobby agents spawn "Fix the bug" -s sess-abc123 -p qwen

        gobby agents spawn "Run tests" -s sess-abc123 --task next
    """
    if reasoning_required and reasoning_effort is None:
        raise click.UsageError(
            "--reasoning-required requires --reasoning-effort. "
            "Provide --reasoning-effort with a supported value."
        )

    daemon_url = get_daemon_url()

    # Resolve session ID
    try:
        parent_session_id = resolve_session_id(parent_session_id)
    except click.ClickException as e:
        raise SystemExit(1) from e

    # Build arguments for the MCP tool call
    arguments = {
        "prompt": prompt,
        "parent_session_id": parent_session_id,
        "terminal": terminal,
        "provider": provider,
        "speed_mode": "fast" if fast else "standard",
        "timeout": timeout,
        "session_context": session_context,
    }

    if workflow:
        arguments["workflow"] = workflow
    if task:
        arguments["task"] = task
    if model:
        arguments["model"] = model
    if reasoning_effort:
        arguments["reasoning_effort"] = reasoning_effort
        arguments["reasoning_required"] = reasoning_required

    # Call the daemon's MCP tool endpoint
    try:
        response = httpx.post(
            f"{daemon_url}/mcp/gobby-agents/tools/spawn_agent",
            json=arguments,
            headers=daemon_auth_headers(),
            timeout=30.0,
        )
        response.raise_for_status()
        result = response.json()
    except httpx.ConnectError as e:
        click.echo("Error: Cannot connect to Gobby daemon. Is it running?", err=True)
        click.echo("Start with: gobby start", err=True)
        raise SystemExit(1) from e
    except httpx.HTTPStatusError as e:
        click.echo(f"Error: Daemon returned {e.response.status_code}", err=True)
        click.echo(e.response.text, err=True)
        raise SystemExit(1) from e
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1) from e

    if json_format:
        click.echo(json_dumps(result, indent=2, default=str))
        if not result.get("success"):
            raise SystemExit(1)
        return

    # Check result
    if result.get("success"):
        run_id = result.get("run_id", "unknown")
        child_session_id = result.get("child_session_id", "unknown")
        status = result.get("status", "unknown")

        click.echo(f"Started agent run: {run_id}")
        click.echo(f"  Child session: {child_session_id}")
        click.echo(f"  Status: {status}")

        if result.get("message"):
            click.echo(f"  {result['message']}")
        reasoning = result.get("reasoning") or {}
        if reasoning.get("message"):
            click.echo(f"  Reasoning: {reasoning['message']}")

        if result.get("output"):
            click.echo(f"\nOutput:\n{result['output']}")
    else:
        error = result.get("error", "Unknown error")
        click.echo(f"Failed to start agent: {error}", err=True)
        raise SystemExit(1)


@agents.group("runs")
def agent_runs() -> None:
    """Manage agent run instances."""
    pass


@agents.command("list")
@click.option(
    "--enabled",
    "enabled_flag",
    flag_value=True,
    default=None,
    help="Show only enabled definitions",
)
@click.option("--disabled", "enabled_flag", flag_value=False, help="Show only disabled definitions")
@click.option("--surface", default=None, help="Filter by supported surface")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def list_agent_definitions(
    enabled_flag: bool | None,
    surface: str | None,
    json_format: bool,
) -> None:
    """List agent definitions."""
    with agent_definition_manager_context() as manager:
        rows = manager.list_all(enabled=enabled_flag)
    summaries: list[dict[str, Any]] = []
    for row in rows:
        try:
            summaries.append(_agent_definition_summary(row))
        except ValueError:
            name = getattr(row, "name", None) or getattr(row, "id", "<unknown>")
            if not json_format:
                click.echo(f"Skipping invalid agent definition {name}", err=True)
            continue
    if surface:
        summaries = [agent for agent in summaries if surface in agent.get("surfaces", ["spawn"])]

    if json_format:
        click.echo(
            json_dumps({"agents": summaries, "count": len(summaries)}, indent=2, default=str)
        )
        return

    if not summaries:
        click.echo("No agent definitions found.")
        return

    click.echo(f"Found {len(summaries)} agent definition(s):\n")
    for agent in summaries:
        status = "on " if agent["enabled"] else "off"
        surfaces = ",".join(agent["surfaces"])
        provider = agent["provider"] or "inherit"
        model = f" {agent['model']}" if agent.get("model") else ""
        desc = f" - {agent['description']}" if agent.get("description") else ""
        click.echo(f"  {status}  {agent['name']}  {provider}{model}  [{surfaces}]{desc}")


@agents.command("show")
@click.argument("name")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def show_agent_definition(name: str, json_format: bool) -> None:
    """Show details for an agent definition."""
    with agent_definition_manager_context() as manager:
        row = manager.get_by_name(name)
    if row is None:
        click.echo(f"Agent definition not found: {name}", err=True)
        raise SystemExit(1)

    try:
        detail = _agent_definition_detail(row)
    except Exception as e:
        click.echo(f"Failed to parse agent definition '{name}': {e}", err=True)
        raise SystemExit(1) from e

    if json_format:
        click.echo(json_dumps(detail, indent=2, default=str))
        return

    click.echo(f"Agent: {detail['name']}")
    if detail.get("description"):
        click.echo(f"Description: {detail['description']}")
    click.echo(f"Enabled: {detail['enabled']}")
    click.echo(f"Provider: {detail['provider']}")
    if detail.get("model"):
        click.echo(f"Model: {detail['model']}")
    click.echo(f"Isolation: {detail['isolation']}")
    click.echo(f"Surfaces: {', '.join(detail['surfaces'])}")
    click.echo(f"Source: {detail['source']}")
    if detail.get("project_id"):
        click.echo(f"Project: {detail['project_id']}")
    if detail.get("timeout"):
        click.echo(f"Timeout: {detail['timeout']}")
    prompts = detail.get("prompts") or {}
    if prompts.get("persona"):
        click.echo(f"\nPersona prompt:\n{prompts['persona']}")
    if prompts.get("agent"):
        click.echo(f"\nAgent prompt:\n{prompts['agent']}")
    if detail.get("steps"):
        click.echo(f"\nSteps: {len(detail['steps'])}")


@agent_runs.command("list")
@click.option("--session", "-s", "session_id", help="Filter by parent session ID")
@click.option(
    "--status",
    type=click.Choice(["pending", "running", "success", "error", "timeout", "cancelled"]),
    help="Filter by status",
)
@click.option("--limit", "-n", default=20, help="Max runs to show")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def list_agent_runs(
    session_id: str | None,
    status: str | None,
    limit: int,
    json_format: bool,
) -> None:
    """List agent runs."""
    with agent_run_manager_context() as manager:
        if session_id:
            try:
                session_id = resolve_session_id(session_id)
            except click.ClickException as e:
                raise SystemExit(1) from e
            runs = manager.list_by_session(
                session_id, status=cast(AgentRunStatus | None, status), limit=limit
            )
        elif status == "running":
            runs = manager.list_running(limit=limit)
        else:
            runs = manager.list_by_status(status=status, limit=limit)

    if json_format:
        click.echo(json_dumps([r.to_dict() for r in runs], indent=2, default=str))
        return

    if not runs:
        click.echo("No agent runs found.")
        return

    click.echo(f"Found {len(runs)} agent run(s):\n")
    for run in runs:
        status_icon = {
            "pending": "○",
            "running": "◐",
            "success": "✓",
            "error": "✗",
            "timeout": "⏱",
            "cancelled": "⊘",
        }.get(run.status, "?")

        # Truncate prompt
        prompt = run.prompt[:40] + "..." if len(run.prompt) > 40 else run.prompt
        prompt = prompt.replace("\n", " ")

        click.echo(f"{status_icon} {run.id[:12]}  {run.status:<10} {run.provider:<8} {prompt}")


@agent_runs.command("show")
@click.argument("run_ref")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def show_agent_run(run_ref: str, json_format: bool) -> None:
    """Show details for an agent run (UUID or prefix)."""
    run_id = resolve_agent_run_id(run_ref)
    with agent_run_manager_context() as manager:
        run = manager.get(run_id)

    if not run:
        # Should not happen if resolve succeeded, but safe check
        raise click.ClickException(f"Agent run not found: {run_id}")

    if json_format:
        click.echo(json_dumps(run.to_dict(), indent=2, default=str))
        return

    click.echo(f"Agent Run: {run.id}")
    click.echo(f"Status: {run.status}")
    click.echo(f"Provider: {run.provider}")
    if run.model:
        click.echo(f"Model: {run.model}")
    click.echo(f"Parent Session: {run.parent_session_id}")
    if run.child_session_id:
        click.echo(f"Child Session: {run.child_session_id}")
    if run.workflow_name:
        click.echo(f"Workflow: {run.workflow_name}")

    click.echo(f"\nPrompt:\n{run.prompt[:500]}")
    if len(run.prompt) > 500:
        click.echo("...")

    if run.result:
        click.echo(f"\nResult:\n{run.result[:500]}")
        if len(run.result) > 500:
            click.echo("...")

    if run.error:
        click.echo(f"\nError: {run.error}")

    click.echo(f"\nTurns Used: {run.turns_used}")
    click.echo(f"Tool Calls: {run.tool_calls_count}")
    click.echo(f"Created: {run.created_at}")
    if run.started_at:
        click.echo(f"Started: {run.started_at}")
    if run.completed_at:
        click.echo(f"Completed: {run.completed_at}")


@agents.command("status")
@click.argument("run_ref")
def agent_status(run_ref: str) -> None:
    """Check status of an agent run (UUID or prefix)."""
    run_id = resolve_agent_run_id(run_ref)
    with agent_run_manager_context() as manager:
        run = manager.get(run_id)

    if not run:
        raise click.ClickException(f"Agent run not found: {run_id}")

    status_icon = {
        "pending": "○",
        "running": "◐",
        "success": "✓",
        "error": "✗",
        "timeout": "⏱",
        "cancelled": "⊘",
    }.get(run.status, "?")

    click.echo(f"{status_icon} {run.id}: {run.status}")

    if run.status == "running" and run.started_at:
        click.echo(f"   Running since: {run.started_at}")
        click.echo(f"   Turns used: {run.turns_used}")
    elif run.status not in ("pending", "running"):
        if run.completed_at:
            click.echo(f"   Completed: {run.completed_at}")
        if run.error:
            click.echo(f"   Error: {run.error}")


@agents.command("stop")
@click.argument("run_ref")
@click.confirmation_option(prompt="Are you sure you want to stop this agent run?")
def stop_agent(run_ref: str) -> None:
    """Stop a running agent and cancel the run."""
    run_id = resolve_agent_run_id(run_ref)
    with agent_run_manager_context() as manager:
        run = manager.get(run_id)

    if not run:
        raise click.ClickException(f"Agent run not found: {run_id}")

    if run.status not in ("pending", "running"):
        raise click.ClickException(f"Cannot stop agent in status: {run.status}")

    client = get_daemon_client()
    try:
        result = client.call_mcp_tool(
            server_name="gobby-agents",
            tool_name="stop_agent",
            arguments={"run_id": run.id},
        )
    except Exception as e:
        raise click.ClickException(str(e)) from e

    if result.get("success"):
        click.echo(f"Stopped agent run: {run.id}")
    else:
        raise click.ClickException(f"Failed: {result.get('error')}")


@agents.command("kill")
@click.argument("run_ref")
@click.option("--force", "-f", is_flag=True, help="Use SIGKILL immediately")
@click.option("--stop", "-s", is_flag=True, help="Also end workflow (prevents restart)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def kill_agent(run_ref: str, force: bool, stop: bool, yes: bool) -> None:
    """Kill a running agent process.

    Sends SIGTERM (or SIGKILL with -f) to terminate the agent process.
    Without --stop: workflow may restart the agent in a new terminal.
    With --stop: also ends the workflow (prevents restart).

    \b
    Examples:
        gobby agents kill abc123 -y        # Kill with SIGTERM
        gobby agents kill abc123 -f -y     # Force kill with SIGKILL
        gobby agents kill abc123 -s -y     # Kill and end workflow
        gobby agents kill abc123 -fs -y    # Force kill and end workflow
    """
    run_id = resolve_agent_run_id(run_ref)

    if not yes:
        msg = "Force kill agent" if force else "Kill agent"
        if stop:
            msg += " and end workflow for"
        if not click.confirm(f"{msg} {run_id[:12]}?"):
            return

    # Call daemon MCP tool
    client = get_daemon_client()
    try:
        result = client.call_mcp_tool(
            server_name="gobby-agents",
            tool_name="kill_agent",
            arguments={
                "run_id": run_id,
                "force": force,
                "stop": stop,
            },
        )
    except Exception as e:
        raise click.ClickException(str(e)) from e

    if result.get("success"):
        msg = result.get("message", f"Killed agent {run_id}")
        click.echo(msg)
        if result.get("found_via") == "pgrep":
            click.echo(f"  (found via pgrep, PID {result.get('pid')})")
        if result.get("already_dead"):
            click.echo("  (process was already terminated)")
        if result.get("workflow_stopped"):
            click.echo("  (workflow ended)")
    else:
        raise click.ClickException(f"Failed: {result.get('error')}")


@agents.command("stats")
@click.option("--session", "-s", "session_id", help="Filter by parent session ID")
def agent_stats(session_id: str | None) -> None:
    """Show agent run statistics."""
    if session_id:
        try:
            session_id = resolve_session_id(session_id)
        except click.ClickException as e:
            raise SystemExit(1) from e
        with agent_run_manager_context() as manager:
            counts = manager.count_by_session(session_id)
        total = sum(counts.values())

        click.echo(f"Agent Statistics for session {session_id[:12]}:")
        click.echo(f"  Total Runs: {total}")
    else:
        with _runtime_db_context() as db:
            # Global stats
            row = db.fetchone(
                """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success,
                    SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as error,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) as running,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                    SUM(CASE WHEN status = 'timeout' THEN 1 ELSE 0 END) as timeout,
                    SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) as cancelled
                FROM agent_runs
                """
            )

        if row:
            click.echo("Agent Run Statistics:")
            click.echo(f"  Total Runs: {row['total']}")
            click.echo(f"  Running: {row['running']}")
            click.echo(f"  Pending: {row['pending']}")
            click.echo(f"  Success: {row['success']}")
            click.echo(f"  Error: {row['error']}")
            click.echo(f"  Timeout: {row['timeout']}")
            click.echo(f"  Cancelled: {row['cancelled']}")

            if row["total"] > 0:
                success_rate = (row["success"] / row["total"]) * 100
                click.echo(f"\n  Success Rate: {success_rate:.1f}%")
        else:
            click.echo("No agent runs found.")


@agents.command("cleanup")
@click.option("--timeout", "-t", default=30, help="Timeout in minutes for stale runs")
@click.option("--dry-run", "-d", is_flag=True, help="Show what would be cleaned up")
def cleanup_agents(timeout: int, dry_run: bool) -> None:
    """Clean up stale agent runs."""
    if dry_run:
        # Show what would be cleaned up
        with _runtime_db_context() as db:
            running_age_filter = older_than_now_expr(db, "started_at", "%s", "minute")
            pending_age_filter = older_than_now_expr(db, "created_at", "%s", "minute")
            stale_running = db.fetchall(
                f"""
                SELECT * FROM agent_runs
                WHERE status = 'running'
                AND {running_age_filter}
                """,  # nosec B608 # filter is generated by sql_dialect helper.
                (timeout,),
            )
            stale_pending = db.fetchall(
                f"""
                SELECT * FROM agent_runs
                WHERE status = 'pending'
                AND {pending_age_filter}
                """,  # nosec B608 # filter is generated by sql_dialect helper.
                (60,),
            )

        click.echo(f"Stale running runs (>{timeout}m): {len(stale_running)}")
        for row in stale_running[:5]:
            click.echo(f"  {row['id']}: started {row['started_at']}")

        click.echo(f"Stale pending runs (>60m): {len(stale_pending)}")
        for row in stale_pending[:5]:
            click.echo(f"  {row['id']}: created {row['created_at']}")
    else:
        try:
            response = get_daemon_client().call_http_api(
                "/api/agents/cleanup",
                json_data={"timeout_minutes": timeout},
            )
            response.raise_for_status()
            payload = response.json()
            run_ids = payload["run_ids"]
            if not isinstance(run_ids, list) or not all(isinstance(item, str) for item in run_ids):
                raise ValueError("Daemon cleanup response has invalid run_ids")
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise click.ClickException(f"Agent cleanup failed: {exc}") from exc

        click.echo(f"Cleaned up {len(run_ids)} stale agent runs.")


agents.add_command(list_agent_steps)
agents.add_command(check_agent)
