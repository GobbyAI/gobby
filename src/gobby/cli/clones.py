"""
Clone management CLI commands.

Commands for managing git clones:
- create: Create a new clone
- list: List clones
- spawn: Spawn an agent in a clone
- sync: Sync a clone with remote
- merge: Merge clone branch to target
- delete: Delete a clone
"""

import json

import click
import httpx

from gobby.storage.clones import LocalCloneManager
from gobby.storage.hub.runtime import open_runtime_hub_database


def get_clone_manager() -> LocalCloneManager:
    """Get initialized clone manager."""
    db = open_runtime_hub_database(apply_migrations=False)
    return LocalCloneManager(db)


def get_daemon_url() -> str:
    """Get daemon URL from config."""
    from gobby.config.app import load_config

    config = load_config()
    return f"http://localhost:{config.daemon_port}"


@click.group()
def clones() -> None:
    """Manage git clones for parallel development."""
    pass


@clones.command("list")
@click.option("--status", "-s", help="Filter by status (active, stale, syncing, cleanup)")
@click.option("--project", "-p", "project_id", help="Filter by project ID")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def list_clones(
    status: str | None,
    project_id: str | None,
    json_format: bool,
) -> None:
    """List clones."""
    manager = get_clone_manager()

    clones_list = manager.list_clones(status=status, project_id=project_id)

    if json_format:
        click.echo(json.dumps([c.to_dict() for c in clones_list], indent=2, default=str))
        return

    if not clones_list:
        click.echo("No clones found.")
        return

    click.echo(f"Found {len(clones_list)} clone(s):\n")
    for clone in clones_list:
        status_icon = {
            "active": "●",
            "syncing": "↻",
            "stale": "○",
            "cleanup": "✗",
        }.get(clone.status, "?")

        session_info = f" (session: {clone.agent_session_id[:8]})" if clone.agent_session_id else ""
        click.echo(
            f"{status_icon} {clone.id}  {clone.branch_name:<30} {clone.status:<10}{session_info}"
        )


@clones.command("create")
@click.argument("branch_name")
@click.argument("clone_path")
@click.option("--base", "-b", "base_branch", default="main", help="Base branch to clone from")
@click.option("--task", "-t", "task_id", help="Link clone to a task")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def create_clone(
    branch_name: str,
    clone_path: str,
    base_branch: str,
    task_id: str | None,
    json_format: bool,
) -> None:
    """Create a new clone for parallel development.

    Examples:

        gobby clones create feature/my-feature /path/to/clone

        gobby clones create bugfix/fix-123 /tmp/fix --base develop --task #47
    """
    daemon_url = get_daemon_url()

    arguments = {
        "branch_name": branch_name,
        "clone_path": clone_path,
        "base_branch": base_branch,
    }

    if task_id:
        arguments["task_id"] = task_id

    try:
        response = httpx.post(
            f"{daemon_url}/mcp/gobby-clones/tools/create_clone",
            json=arguments,
            timeout=300.0,  # Clone can take a while
        )
        response.raise_for_status()
        result = response.json()
    except httpx.ConnectError:
        click.echo("Error: Cannot connect to Gobby daemon. Is it running?", err=True)
        return
    except httpx.HTTPStatusError as e:
        click.echo(f"HTTP Error {e.response.status_code}: {e.response.text}", err=True)
        return
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        return

    if json_format:
        click.echo(json.dumps(result, indent=2, default=str))
        return

    if result.get("success"):
        clone_info = result.get("clone", {})
        click.echo(f"Created clone: {clone_info.get('id', 'unknown')}")
        click.echo(f"  Branch: {clone_info.get('branch_name', 'unknown')}")
    else:
        click.echo(f"Failed to create clone: {result.get('error')}", err=True)


@clones.command("spawn")
@click.argument("clone_ref")
@click.argument("prompt")
@click.option(
    "--parent-session-id",
    "-p",
    "parent_session_id",
    required=True,
    help="Parent session ID (required)",
)
@click.option("--workflow", "-w", help="Workflow to activate")
@click.option("--reasoning-effort", help="Reasoning effort override")
@click.option(
    "--reasoning-required/--no-reasoning-required",
    default=False,
    help="Fail instead of warning when the requested reasoning is unsupported",
)
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def spawn_agent(
    clone_ref: str,
    prompt: str,
    parent_session_id: str,
    workflow: str | None,
    reasoning_effort: str | None,
    reasoning_required: bool,
    json_format: bool,
) -> None:
    """Spawn an agent to work in a clone.

    Examples:

        gobby clones spawn clone-123 "Fix the authentication bug"

        gobby clones spawn clone-123 "Implement feature" --workflow auto-task
    """
    manager = get_clone_manager()
    clone_id = resolve_clone_id(manager, clone_ref)

    if not clone_id:
        click.echo(f"Clone not found: {clone_ref}", err=True)
        return

    daemon_url = get_daemon_url()

    arguments: dict[str, str | bool] = {
        "prompt": prompt,
        "parent_session_id": parent_session_id,
        "isolation": "clone",
        "clone_id": clone_id,
    }

    if workflow:
        arguments["workflow"] = workflow
    if reasoning_effort:
        arguments["reasoning_effort"] = reasoning_effort
        arguments["reasoning_required"] = reasoning_required

    try:
        response = httpx.post(
            f"{daemon_url}/mcp/gobby-agents/tools/spawn_agent",
            json=arguments,
            timeout=60.0,
        )
        response.raise_for_status()
        result = response.json()
    except httpx.ConnectError:
        click.echo("Error: Cannot connect to Gobby daemon. Is it running?", err=True)
        return
    except httpx.HTTPStatusError as e:
        click.echo(f"HTTP Error {e.response.status_code}: {e.response.text}", err=True)
        return
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        return

    if json_format:
        click.echo(json.dumps(result, indent=2, default=str))
        return

    if result.get("success"):
        session_id = result.get("session_id", "unknown")
        click.echo(f"Spawned agent in clone {clone_id}")
        click.echo(f"  Session: {session_id}")
        reasoning = result.get("reasoning") or {}
        if reasoning.get("message"):
            click.echo(f"  Reasoning: {reasoning['message']}")
    else:
        click.echo(f"Failed to spawn agent: {result.get('error')}", err=True)


@clones.command("sync")
@click.argument("clone_ref")
@click.option("--direction", "-d", default="pull", help="Sync direction (pull, push, both)")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def sync_clone(clone_ref: str, direction: str, json_format: bool) -> None:
    """Sync clone with remote.

    Examples:

        gobby clones sync clone-123

        gobby clones sync clone-123 --direction push
    """
    manager = get_clone_manager()
    clone_id = resolve_clone_id(manager, clone_ref)

    if not clone_id:
        click.echo(f"Clone not found: {clone_ref}", err=True)
        return

    daemon_url = get_daemon_url()

    try:
        response = httpx.post(
            f"{daemon_url}/mcp/gobby-clones/tools/sync_clone",
            json={"clone_id": clone_id, "direction": direction},
            timeout=120.0,
        )
        response.raise_for_status()
        result = response.json()
    except httpx.ConnectError:
        click.echo("Error: Cannot connect to Gobby daemon. Is it running?", err=True)
        return
    except httpx.HTTPStatusError as e:
        click.echo(f"HTTP Error {e.response.status_code}: {e.response.text}", err=True)
        return
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        return

    if json_format:
        click.echo(json.dumps(result, indent=2, default=str))
        return

    if result.get("success"):
        click.echo(f"Synced clone {clone_id}")
    else:
        click.echo(f"Failed to sync clone: {result.get('error')}", err=True)


@clones.command("merge")
@click.argument("clone_ref")
@click.option("--target", "-t", "target_branch", default="main", help="Target branch to merge into")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def merge_clone(clone_ref: str, target_branch: str, json_format: bool) -> None:
    """Merge clone branch to target branch.

    Examples:

        gobby clones merge clone-123

        gobby clones merge clone-123 --target develop
    """
    manager = get_clone_manager()
    clone_id = resolve_clone_id(manager, clone_ref)

    if not clone_id:
        click.echo(f"Clone not found: {clone_ref}", err=True)
        return

    daemon_url = get_daemon_url()

    try:
        response = httpx.post(
            f"{daemon_url}/mcp/gobby-clones/tools/merge_clone",
            json={"clone_id": clone_id, "target_branch": target_branch},
            timeout=120.0,
        )
        response.raise_for_status()
        result = response.json()
    except httpx.ConnectError:
        click.echo("Error: Cannot connect to Gobby daemon. Is it running?", err=True)
        return
    except httpx.HTTPStatusError as e:
        click.echo(f"HTTP Error {e.response.status_code}: {e.response.text}", err=True)
        return
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        return

    if json_format:
        click.echo(json.dumps(result, indent=2, default=str))
        return

    if result.get("success"):
        click.echo(f"Merged clone {clone_id} to {target_branch}")
    else:
        # Check for merge conflicts
        if result.get("has_conflicts"):
            conflicted = result.get("conflicted_files", [])
            click.echo(f"Merge conflict in {len(conflicted)} file(s):", err=True)
            for f in conflicted:
                click.echo(f"  {f}", err=True)
        else:
            click.echo(f"Failed to merge clone: {result.get('error')}", err=True)


@clones.command("delete")
@click.argument("clone_ref")
@click.option("--force", "-f", is_flag=True, help="Force delete even if active")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def delete_clone(clone_ref: str, force: bool, yes: bool, json_format: bool) -> None:
    """Delete a clone.

    Examples:

        gobby clones delete clone-123 --yes

        gobby clones delete clone-123 --force --yes
    """
    manager = get_clone_manager()
    clone_id = resolve_clone_id(manager, clone_ref)

    if not clone_id:
        if json_format:
            click.echo(json.dumps({"success": False, "error": f"Clone not found: {clone_ref}"}))
        else:
            click.echo(f"Clone not found: {clone_ref}", err=True)
        return

    if not yes and not json_format:
        click.confirm("Are you sure you want to delete this clone?", abort=True)

    daemon_url = get_daemon_url()

    try:
        response = httpx.post(
            f"{daemon_url}/mcp/gobby-clones/tools/delete_clone",
            json={"clone_id": clone_id, "force": force},
            timeout=60.0,
        )
        response.raise_for_status()
        result = response.json()
    except httpx.ConnectError:
        if json_format:
            click.echo(json.dumps({"success": False, "error": "Cannot connect to Gobby daemon"}))
        else:
            click.echo("Error: Cannot connect to Gobby daemon. Is it running?", err=True)
        return
    except httpx.HTTPStatusError as e:
        if json_format:
            click.echo(
                json.dumps(
                    {
                        "success": False,
                        "error": f"HTTP Error {e.response.status_code}",
                        "detail": e.response.text,
                    }
                )
            )
        else:
            click.echo(f"HTTP Error {e.response.status_code}: {e.response.text}", err=True)
        return
    except Exception as e:
        if json_format:
            click.echo(json.dumps({"success": False, "error": str(e)}))
        else:
            click.echo(f"Error: {e}", err=True)
        return

    if json_format:
        click.echo(json.dumps(result, indent=2, default=str))
        return

    if result.get("success"):
        click.echo(f"Deleted clone: {clone_id}")
    else:
        click.echo(f"Failed to delete clone: {result.get('error')}", err=True)


def resolve_clone_id(manager: LocalCloneManager, clone_ref: str) -> str | None:
    """Resolve clone reference (UUID or prefix) to full ID."""
    # Check for exact match first
    if manager.get(clone_ref):
        return clone_ref

    # Try prefix match
    all_clones = manager.list_clones()
    matches = [c for c in all_clones if c.id.startswith(clone_ref)]

    if not matches:
        return None

    if len(matches) > 1:
        click.echo(f"Ambiguous clone reference '{clone_ref}' matches:", err=True)
        for c in matches:
            click.echo(f"  {c.id[:8]} ({c.branch_name})", err=True)
        return None

    return matches[0].id
