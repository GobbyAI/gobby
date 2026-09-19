"""Workspace, pane, and node CLI commands.

`gobby workspaces`, `gobby panes`, and `gobby nodes` call the daemon's
gobby-workspaces registry over HTTP with the local CLI token, which the MCP route
classifies as the `operator` actor. Rows are addressed by ref (`2:0:0:1`) or id;
`--node` names the node for a lone workspace ref or a name, and defaults to the
daemon's own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from json import JSONDecodeError
from typing import Any, cast

import click
import httpx

from gobby.cli.utils_config import get_daemon_url
from gobby.utils.json_helpers import json_dumps
from gobby.utils.local_token import daemon_auth_headers

_NODE_PREFIX = re.compile(r"\d+:")
_SPLIT_DIRECTION = "panes split takes exactly one of --right, --left, --above, --below"
_MOVE_DIRECTION = "panes move takes one direction: --right, --left, --above, or --below"
_DEFAULT_TIMEOUT = 30.0
_SPAWN_TIMEOUT = 60.0


@dataclass(frozen=True)
class _Direction:
    """Where a pane goes: the split axis, and whether it takes the first place."""

    axis: str
    first: bool


_DIRECTIONS = {
    "right": _Direction("horizontal", False),
    "left": _Direction("horizontal", True),
    "above": _Direction("vertical", True),
    "below": _Direction("vertical", False),
}


class WorkspaceToolError(click.ClickException):
    """A workspace tool completed with an explicit failure result."""

    def __init__(self, tool_name: str, reason: str, code: str | None = None) -> None:
        self.tool_name = tool_name
        self.reason = reason
        self.code = code
        detail = f"{reason} ({code})" if code else reason
        super().__init__(f"Failed to {tool_name.replace('_', ' ')}: {detail}")


def _call_workspace_tool(
    tool_name: str,
    arguments: dict[str, Any],
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Call a workspace tool, returning its payload or raising its reported failure."""
    daemon_url = get_daemon_url()
    try:
        response = httpx.post(
            f"{daemon_url}/api/mcp/gobby-workspaces/tools/{tool_name}",
            json=arguments,
            headers=daemon_auth_headers(),
            timeout=timeout,
        )
        response.raise_for_status()
    except httpx.ConnectError as exc:
        raise click.ClickException("Cannot connect to Gobby daemon. Is it running?") from exc
    except httpx.TimeoutException as exc:
        raise click.ClickException(f"Timed out calling Gobby daemon: {exc}") from exc
    except httpx.HTTPStatusError as exc:
        raise click.ClickException(
            f"HTTP Error {exc.response.status_code}: {exc.response.text}"
        ) from exc
    except httpx.HTTPError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        raw_result = response.json()
    except JSONDecodeError as exc:
        raise click.ClickException(f"Invalid JSON response from Gobby daemon: {exc}") from exc

    if not isinstance(raw_result, dict):
        raise click.ClickException(
            f"Invalid response from Gobby daemon for {tool_name}: expected a JSON object"
        )

    outer = cast(dict[str, Any], raw_result)
    # A refused tool reports success=False inside the route's success envelope.
    inner = outer.get("result")
    payload = cast(dict[str, Any], inner) if isinstance(inner, dict) else outer
    if payload.get("success") is False:
        error = payload.get("error")
        reason = error if isinstance(error, str) and error else "No failure reason was provided"
        code = payload.get("code")
        raise WorkspaceToolError(tool_name, reason, code if isinstance(code, str) else None)
    if outer.get("success") is not True:
        raise click.ClickException(
            f"Invalid response from Gobby daemon for {tool_name}: missing boolean 'success'"
        )
    return payload


def _arguments(**values: Any) -> dict[str, Any]:
    """Drop unset options so each tool applies its own defaults."""
    return {name: value for name, value in values.items() if value is not None}


def _node_of(ref: str, node: str | None) -> str | None:
    """Return the node to send: a ref that names its own node overrides ``--node``."""
    return None if _NODE_PREFIX.match(ref.strip()) else node


def _direction(chosen: list[str], message: str) -> _Direction | None:
    if len(chosen) > 1:
        raise click.UsageError(message)
    return _DIRECTIONS[chosen[0]] if chosen else None


def _rows(result: dict[str, Any], key: str) -> list[dict[str, Any]]:
    rows = result.get(key)
    if not isinstance(rows, list):
        return []
    return [cast(dict[str, Any], row) for row in rows if isinstance(row, dict)]


def _row(result: dict[str, Any], key: str) -> dict[str, Any]:
    row = result.get(key)
    if not isinstance(row, dict):
        raise click.ClickException(f"Gobby daemon returned no {key}")
    return cast(dict[str, Any], row)


def _replace_tab(result: dict[str, Any], tab: dict[str, Any]) -> None:
    """Put the tab a follow-up swap returned over its pre-swap copy in ``result``."""
    result["tabs"] = [tab if row.get("id") == tab["id"] else row for row in _rows(result, "tabs")]


def _emit(result: dict[str, Any], json_format: bool) -> bool:
    """Print the payload in JSON mode; report whether text output still has to run."""
    if json_format:
        click.echo(json_dumps(result, indent=2, default=str))
        return False
    return True


def _pane_line(tab_ref: str, pane: dict[str, Any]) -> str:
    label = f"  {pane['label']}" if pane.get("label") else ""
    terminal = pane.get("terminal_id") or "spawning"
    return f"  {tab_ref}:{pane['ref']}  {terminal}{label}"


@click.group()
def workspaces() -> None:
    """Manage the daemon's workspaces."""


@click.group()
def panes() -> None:
    """Arrange workspace panes and drive their terminals."""


@click.group()
def nodes() -> None:
    """Inspect the operator's nodes."""


@workspaces.command("list")
@click.option("--node", "node", help="Node ref (a number), id, hostname, or label")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def list_workspaces(node: str | None, json_format: bool) -> None:
    """List a node's workspaces."""
    result = _call_workspace_tool("list_workspaces", _arguments(node=node))
    if not _emit(result, json_format):
        return

    rows = _rows(result, "workspaces")
    if not rows:
        click.echo("No workspaces found.")
        return

    click.echo(f"Node {result.get('node_ref')}: {len(rows)} workspace(s)")
    for row in rows:
        click.echo(f"{row['ref']}  {row['name']}  {row['id']}")


@workspaces.command("show")
@click.argument("workspace")
@click.option("--node", "node", help="Node ref (a number), id, hostname, or label")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def show_workspace(workspace: str, node: str | None, json_format: bool) -> None:
    """Show WORKSPACE with its tabs and panes."""
    result = _call_workspace_tool(
        "get_workspace", _arguments(workspace=workspace, node=_node_of(workspace, node))
    )
    if not _emit(result, json_format):
        return

    row = _row(result, "workspace")
    workspace_ref = f"{row.get('node_ref')}:{row['ref']}"
    click.echo(f"Workspace {workspace_ref}  {row['name']}")
    panes_by_tab: dict[str, list[dict[str, Any]]] = {}
    for pane in _rows(result, "panes"):
        panes_by_tab.setdefault(str(pane["tab_id"]), []).append(pane)
    for tab in _rows(result, "tabs"):
        title = f"  {tab['title']}" if tab.get("title") else ""
        tab_ref = f"{workspace_ref}:{tab['ref']}"
        click.echo(f"{tab_ref}{title}  project {tab['project_id']}")
        for pane in panes_by_tab.get(str(tab["id"]), []):
            click.echo(_pane_line(tab_ref, pane))


@workspaces.command("create")
@click.argument("name")
@click.option("--node", "node", help="Node ref (a number), id, hostname, or label")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def create_workspace(name: str, node: str | None, json_format: bool) -> None:
    """Return the workspace named NAME, creating it on first use."""
    result = _call_workspace_tool("create_workspace", _arguments(name=name, node=node))
    if not _emit(result, json_format):
        return

    row = _row(result, "workspace")
    click.echo(f"Workspace {row['ref']}: {row['name']}")


@workspaces.command("delete")
@click.argument("workspace")
@click.option("--node", "node", help="Node ref (a number), id, hostname, or label")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def delete_workspace(workspace: str, node: str | None, json_format: bool) -> None:
    """Close WORKSPACE and kill the terminals it owns."""
    result = _call_workspace_tool(
        "close_workspace",
        _arguments(workspace=workspace, node=_node_of(workspace, node)),
        timeout=_SPAWN_TIMEOUT,
    )
    if not _emit(result, json_format):
        return

    row = _row(result, "workspace")
    click.echo(f"Closed workspace {row['ref']}: {row['name']}")


@panes.command("split")
@click.argument("pane")
@click.option("--right", is_flag=True, help="Open the new pane to the right")
@click.option("--left", is_flag=True, help="Open the new pane to the left")
@click.option("--above", is_flag=True, help="Open the new pane above")
@click.option("--below", is_flag=True, help="Open the new pane below")
@click.option("--terminal", "terminal_id", help="Adopt this live terminal instead of spawning")
@click.option("--node", "node", help="Node ref (a number), id, hostname, or label")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def split_pane(
    pane: str,
    right: bool,
    left: bool,
    above: bool,
    below: bool,
    terminal_id: str | None,
    node: str | None,
    json_format: bool,
) -> None:
    """Split PANE, opening a shell or adopting a terminal."""
    chosen = [
        name
        for name, flag in (("right", right), ("left", left), ("above", above), ("below", below))
        if flag
    ]
    direction = _direction(chosen, _SPLIT_DIRECTION)
    if direction is None:
        raise click.UsageError(_SPLIT_DIRECTION)

    node_ref = _node_of(pane, node)
    result = _call_workspace_tool(
        "split_pane",
        _arguments(pane=pane, axis=direction.axis, terminal_id=terminal_id, node=node_ref),
        timeout=_SPAWN_TIMEOUT,
    )
    added = _rows(result, "panes")
    if not added:
        raise click.ClickException("Gobby daemon returned no pane for the split")
    if direction.first:
        # A split always lands second, so --left and --above swap it into place.
        swapped = _call_workspace_tool(
            "swap_panes", _arguments(pane=added[0]["id"], other=pane, node=node_ref)
        )
        _replace_tab(result, _row(swapped, "tab"))
    if not _emit(result, json_format):
        return

    click.echo(f"Pane {added[0]['ref']}  {added[0]['id']}")


@panes.command("close")
@click.argument("pane")
@click.option("--node", "node", help="Node ref (a number), id, hostname, or label")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def close_pane(pane: str, node: str | None, json_format: bool) -> None:
    """Close PANE and kill the terminal it owns."""
    result = _call_workspace_tool(
        "close_pane",
        _arguments(pane=pane, node=_node_of(pane, node)),
        timeout=_SPAWN_TIMEOUT,
    )
    if not _emit(result, json_format):
        return

    for row in _rows(result, "removed_panes"):
        click.echo(f"Closed pane {row['ref']}")
    for row in _rows(result, "removed_tabs"):
        click.echo(f"Closed empty tab {row['ref']}")


@panes.command("move")
@click.argument("pane")
@click.option("--tab", "tab", required=True, help="Destination tab ref or id")
@click.option("--right", help="Place PANE right of this pane")
@click.option("--left", help="Place PANE left of this pane")
@click.option("--above", help="Place PANE above this pane")
@click.option("--below", help="Place PANE below this pane")
@click.option("--node", "node", help="Node ref (a number), id, hostname, or label")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def move_pane(
    pane: str,
    tab: str,
    right: str | None,
    left: str | None,
    above: str | None,
    below: str | None,
    node: str | None,
    json_format: bool,
) -> None:
    """Move PANE into another tab, beside the pane a direction names."""
    named = [
        (name, value)
        for name, value in (("right", right), ("left", left), ("above", above), ("below", below))
        if value is not None
    ]
    direction = _direction([name for name, _ in named], _MOVE_DIRECTION)
    beside = named[0][1] if named else None

    node_ref = _node_of(pane, node)
    result = _call_workspace_tool(
        "move_pane",
        _arguments(
            pane=pane,
            tab=tab,
            beside=beside,
            axis=None if direction is None else direction.axis,
            node=node_ref,
        ),
    )
    if direction is not None and direction.first and beside is not None:
        # A moved pane lands second beside its target, so --left and --above swap it.
        # The move renumbers a pane that changed tab, so the swap addresses it by id.
        moved = _rows(result, "panes")
        if not moved:
            raise click.ClickException("Gobby daemon returned no pane for the move")
        swapped = _call_workspace_tool(
            "swap_panes", _arguments(pane=moved[0]["id"], other=beside, node=node_ref)
        )
        _replace_tab(result, _row(swapped, "tab"))
    if not _emit(result, json_format):
        return

    for row in _rows(result, "panes"):
        click.echo(f"Moved pane {row['ref']} into tab {tab}")


@panes.command("swap")
@click.argument("pane")
@click.argument("other")
@click.option("--node", "node", help="Node ref (a number), id, hostname, or label")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def swap_panes(pane: str, other: str, node: str | None, json_format: bool) -> None:
    """Swap PANE and OTHER in their tab."""
    result = _call_workspace_tool(
        "swap_panes", _arguments(pane=pane, other=other, node=_node_of(pane, node))
    )
    if not _emit(result, json_format):
        return

    click.echo(f"Swapped {pane} and {other} in tab {_row(result, 'tab')['ref']}")


@panes.command("rename")
@click.argument("ref")
@click.argument("name", required=False)
@click.option("--node", "node", help="Node ref (a number), id, hostname, or label")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def rename_workspace_item(ref: str, name: str | None, node: str | None, json_format: bool) -> None:
    """Rename what REF names; omit NAME to clear a tab title or pane label."""
    result = _call_workspace_tool(
        "rename_workspace_item", _arguments(ref=ref, name=name, node=_node_of(ref, node))
    )
    if not _emit(result, json_format):
        return

    for key, field in (("workspace", "name"), ("tab", "title"), ("pane", "label")):
        row = result.get(key)
        if isinstance(row, dict):
            click.echo(f"Renamed {key} {row['ref']}: {row.get(field) or '(cleared)'}")
            return


@panes.command("send")
@click.argument("pane")
@click.argument("text")
@click.option("--submit", is_flag=True, help="Press Enter after the text")
@click.option("--key", "as_key", is_flag=True, help="Send TEXT as a key name (enter, c-c)")
@click.option("--idempotency-key", "idempotency_key", help="Reuse to retry a write safely")
@click.option("--node", "node", help="Node ref (a number), id, hostname, or label")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def send_pane_input(
    pane: str,
    text: str,
    submit: bool,
    as_key: bool,
    idempotency_key: str | None,
    node: str | None,
    json_format: bool,
) -> None:
    """Type TEXT into PANE, or press it as a key with --key."""
    if as_key and submit:
        raise click.UsageError("panes send --key presses a key name; --submit applies to text")

    node_ref = _node_of(pane, node)
    if as_key:
        result = _call_workspace_tool(
            "send_pane_keys",
            _arguments(
                pane=pane,
                keys=text,
                literal=False,
                idempotency_key=idempotency_key,
                node=node_ref,
            ),
        )
    else:
        result = _call_workspace_tool(
            "send_text",
            _arguments(
                pane=pane,
                text=text,
                submit=submit or None,
                idempotency_key=idempotency_key,
                node=node_ref,
            ),
        )
    if not _emit(result, json_format):
        return

    state = "indeterminate" if result.get("indeterminate") else "accepted"
    detail = f": {result['detail']}" if result.get("detail") else ""
    click.echo(f"Write {state} (idempotency key {result.get('idempotency_key')}){detail}")


@panes.command("read")
@click.argument("pane")
@click.option("--lines", type=int, help="Screen lines to return (default 50)")
@click.option("--node", "node", help="Node ref (a number), id, hostname, or label")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def read_pane(pane: str, lines: int | None, node: str | None, json_format: bool) -> None:
    """Read the last lines of PANE's screen."""
    result = _call_workspace_tool(
        "read_pane", _arguments(pane=pane, lines=lines, node=_node_of(pane, node))
    )
    if not _emit(result, json_format):
        return

    click.echo(str(result.get("text", "")))
    if result.get("truncated"):
        click.echo(f"(truncated, dropped {result.get('dropped_bytes')} bytes)")


@panes.command("wait")
@click.argument("pane")
@click.argument("pattern")
@click.option("--timeout", "timeout_seconds", type=float, help="Seconds to wait (capped at 300)")
@click.option("--poll-interval", "poll_interval", type=float, help="Seconds between reads")
@click.option("--node", "node", help="Node ref (a number), id, hostname, or label")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def wait_for_pane_output(
    pane: str,
    pattern: str,
    timeout_seconds: float | None,
    poll_interval: float | None,
    node: str | None,
    json_format: bool,
) -> None:
    """Wait until PANE's screen matches the PATTERN regex."""
    result = _call_workspace_tool(
        "wait_for_pane_output",
        _arguments(
            pane=pane,
            pattern=pattern,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval,
            node=_node_of(pane, node),
        ),
        timeout=(timeout_seconds or 300.0) + _DEFAULT_TIMEOUT,
    )
    if not _emit(result, json_format):
        return

    click.echo(f"Wait {result.get('reason')}")
    snapshot = result.get("snapshot")
    if isinstance(snapshot, dict):
        click.echo(str(snapshot.get("text", "")))


@nodes.command("list")
@click.option("--node", "node", help="Show only this node (ref, id, hostname, or label)")
@click.option("--json", "json_format", is_flag=True, help="Output as JSON")
def list_nodes(node: str | None, json_format: bool) -> None:
    """List the operator's nodes; the daemon's own node is marked local."""
    result = _call_workspace_tool("list_nodes", _arguments(node=node))
    if not _emit(result, json_format):
        return

    rows = _rows(result, "nodes")
    if not rows:
        click.echo("No nodes found.")
        return

    for row in rows:
        local = "  local" if row.get("local") else ""
        label = f"  {row['label']}" if row.get("label") else ""
        click.echo(f"{row['ref']}  {row.get('hostname')}{label}{local}")
