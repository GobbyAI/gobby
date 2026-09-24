"""gobby-workspaces: workspaces, tabs, and panes addressed by ref.

Every tool executes through the WebSocket server's shared ``WorkspaceOps``, so changes
publish to subscribed clients and pane writes carry the ``send_keys`` policy. The actor
comes from the request, never from arguments: a session context acts as that session,
the operator's local token acts as the operator, and every other caller is refused.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, is_dataclass
from typing import Any, Literal

from gobby.mcp_proxy.tools.internal import InternalToolRegistry
from gobby.mcp_proxy.wait_tools import (
    MCP_WRAPPER_WAIT_TOOL_TIMEOUT_SECONDS,
    clamp_wait_tool_timeout,
)
from gobby.storage.machines import LocalMachineManager
from gobby.storage.workspaces import (
    DEFAULT_WORKSPACE_NAME,
    Workspace,
    WorkspaceManager,
    WorkspacePane,
    WorkspaceTab,
)
from gobby.terminals.actor_scope import OPERATOR_ACTOR, SESSION_ACTOR_PREFIX
from gobby.terminals.workspace_contract import WorkspaceOpError, storage_errors
from gobby.terminals.workspace_ops import WorkspaceOps
from gobby.utils.datetime import to_json_safe
from gobby.utils.session_context import get_request_principal, get_session_context

type OpsResolver = Callable[[], WorkspaceOps | None]

_NODE = "Optional `node` (ref, hostname, label, or id) resolves refs on that machine."


async def current_actor() -> str:
    """Return ``session:<id>`` or ``operator`` for this request, refusing any other caller.

    The principal decides first: an agent API token is refused whatever session header
    it carries, and an unseeded var (no request in scope) is refused the same way.
    """
    try:
        principal = await get_request_principal()
    except LookupError:
        principal = False
    if principal is not None:
        raise WorkspaceOpError(
            "forbidden", "Workspace tools act only for a session or the operator's local token"
        )
    context = get_session_context()
    if context is not None:
        return f"{SESSION_ACTOR_PREFIX}{context.session_id}"
    return OPERATOR_ACTOR


def _json(value: object) -> Any:
    if isinstance(value, list | tuple):
        return [_json(item) for item in value]
    if isinstance(value, Workspace):
        return to_json_safe(value.to_dict())
    if is_dataclass(value) and not isinstance(value, type):
        return to_json_safe(asdict(value))
    return to_json_safe(value)


def _payload(value: object) -> dict[str, Any]:
    """Name a single row by its kind; a result record spreads its own fields."""
    for key, kind in (("workspace", Workspace), ("tab", WorkspaceTab), ("pane", WorkspacePane)):
        if isinstance(value, kind):
            return {key: _json(value)}
    return dict(_json(value))


async def _run(work: Callable[[str], Awaitable[object]]) -> dict[str, Any]:
    try:
        result = await work(await current_actor())
    except WorkspaceOpError as exc:
        return {"success": False, "error": str(exc), "code": exc.code}
    return {"success": True, **_payload(result)}


def create_workspaces_registry(
    workspace_manager: WorkspaceManager, ops_resolver: OpsResolver
) -> InternalToolRegistry:
    """Create the gobby-workspaces registry over the shared ops the resolver returns."""
    registry = InternalToolRegistry(
        name="gobby-workspaces",
        description="Workspaces, tabs, and panes addressed by node:workspace:tab:pane refs",
    )

    def ops() -> WorkspaceOps:
        shared = ops_resolver()
        if shared is None:
            raise WorkspaceOpError("terminal_failed", "Workspace ops are not configured")
        return shared

    @registry.tool(
        description=f"List the operator's machines; `local` marks this daemon's node. {_NODE}",
        read_only=True,
    )
    async def list_nodes(node: str | None = None) -> dict[str, Any]:
        async def work(_actor: str) -> dict[str, Any]:
            with storage_errors():
                local = workspace_manager.resolve_node()
                machines = (
                    [workspace_manager.resolve_node(node)]
                    if node
                    else LocalMachineManager(workspace_manager.db).list_for_user(
                        local.owner_user_id
                    )
                )
            return {
                "nodes": [
                    {**_json(machine.to_dict()), "local": machine.id == local.id}
                    for machine in machines
                ]
            }

        return await _run(work)

    @registry.tool(
        description=f"List a node's workspaces (this node by default). {_NODE}", read_only=True
    )
    async def list_workspaces(node: str | None = None) -> dict[str, Any]:
        async def work(_actor: str) -> dict[str, Any]:
            with storage_errors():
                machine = workspace_manager.resolve_node(node)
                rows = workspace_manager.list_for_node(machine.id)
            return {"node_ref": machine.ref, "workspaces": _json(rows)}

        return await _run(work)

    @registry.tool(description=f"Read a workspace with its tabs and panes. {_NODE}", read_only=True)
    async def get_workspace(workspace: str, node: str | None = None) -> dict[str, Any]:
        async def work(actor: str) -> dict[str, Any]:
            snapshot = await ops().workspace_snapshot(actor, workspace, node=node)
            return {
                "workspace": {**_json(snapshot.workspace), "node_ref": snapshot.node.ref},
                "tabs": _json(snapshot.tabs),
                "panes": _json(snapshot.panes),
            }

        return await _run(work)

    @registry.tool(description=f"Return the workspace named `name`, creating it. {_NODE}")
    async def create_workspace(
        name: str = DEFAULT_WORKSPACE_NAME, node: str | None = None
    ) -> dict[str, Any]:
        return await _run(lambda actor: ops().workspace_create(actor, name, node=node))

    @registry.tool(description=f"Close a workspace and kill its owned terminals. {_NODE}")
    async def close_workspace(workspace: str, node: str | None = None) -> dict[str, Any]:
        return await _run(lambda actor: ops().workspace_close(actor, workspace, node=node))

    @registry.tool(
        description=(
            "Open a tab in a workspace: a new shell in the project (or worktree), or an "
            f"adopted `terminal_id`. {_NODE}"
        )
    )
    async def create_tab(
        workspace: str,
        project_id: str,
        worktree_id: str | None = None,
        title: str | None = None,
        terminal_id: str | None = None,
        node: str | None = None,
    ) -> dict[str, Any]:
        return await _run(
            lambda actor: ops().tab_create(
                actor,
                workspace,
                project_id,
                worktree_id=worktree_id,
                title=title,
                terminal_id=terminal_id,
                node=node,
            )
        )

    @registry.tool(description=f"Close a tab and kill its owned terminals. {_NODE}")
    async def close_tab(tab: str, node: str | None = None) -> dict[str, Any]:
        return await _run(lambda actor: ops().tab_close(actor, tab, node=node))

    @registry.tool(
        description=f"Split a pane, opening a new shell or adopting `terminal_id`. {_NODE}"
    )
    async def split_pane(
        pane: str,
        axis: Literal["horizontal", "vertical"],
        terminal_id: str | None = None,
        node: str | None = None,
    ) -> dict[str, Any]:
        return await _run(
            lambda actor: ops().pane_split(actor, pane, axis, terminal_id=terminal_id, node=node)
        )

    @registry.tool(description=f"Close a pane and kill its owned terminal. {_NODE}")
    async def close_pane(pane: str, node: str | None = None) -> dict[str, Any]:
        return await _run(lambda actor: ops().pane_close(actor, pane, node=node))

    @registry.tool(
        description=f"Move a pane into another tab, optionally beside one of its panes. {_NODE}"
    )
    async def move_pane(
        pane: str,
        tab: str,
        beside: str | None = None,
        axis: Literal["horizontal", "vertical"] = "horizontal",
        node: str | None = None,
    ) -> dict[str, Any]:
        return await _run(
            lambda actor: ops().pane_move(actor, pane, tab, beside=beside, axis=axis, node=node)
        )

    @registry.tool(description=f"Swap two panes' places. {_NODE}")
    async def swap_panes(pane: str, other: str, node: str | None = None) -> dict[str, Any]:
        return await _run(lambda actor: ops().pane_swap(actor, pane, other, node=node))

    @registry.tool(
        description=(
            "Rename what `ref` names: a workspace's name, or a tab's title or pane's label "
            f"(omit `name` to clear those). {_NODE}"
        )
    )
    async def rename_workspace_item(
        ref: str, name: str | None = None, node: str | None = None
    ) -> dict[str, Any]:
        async def work(actor: str) -> object:
            shared = ops()
            with storage_errors():
                target = workspace_manager.resolve_reference(ref, node=node)
            if target.pane is not None:
                return await shared.pane_rename(actor, ref, name, node=node)
            if target.tab is not None:
                return await shared.tab_rename(actor, ref, name, node=node)
            if name is None:
                raise WorkspaceOpError("invalid_op", "A workspace rename needs a name")
            return await shared.workspace_rename(actor, ref, name, node=node)

        return await _run(work)

    @registry.tool(
        description=(
            "Type text into a pane; `submit` presses Enter after it. Reuse "
            f"`idempotency_key` to retry an indeterminate write safely. {_NODE}"
        )
    )
    async def send_text(
        pane: str,
        text: str,
        submit: bool = False,
        idempotency_key: str | None = None,
        node: str | None = None,
    ) -> dict[str, Any]:
        return await _run(
            lambda actor: ops().pane_send_text(
                actor, pane, text, submit=submit, idempotency_key=idempotency_key, node=node
            )
        )

    @registry.tool(
        description=(
            "Send keys as gobby-sessions send_keys does: a trailing newline submits, and "
            f"with `literal` false a key name (enter, escape, c-c) is pressed. {_NODE}"
        )
    )
    async def send_pane_keys(
        pane: str,
        keys: str,
        literal: bool = True,
        idempotency_key: str | None = None,
        node: str | None = None,
    ) -> dict[str, Any]:
        return await _run(
            lambda actor: ops().pane_send_keys(
                actor, pane, keys, literal=literal, idempotency_key=idempotency_key, node=node
            )
        )

    @registry.tool(
        description=f"Read the last `lines` lines of a pane's screen. {_NODE}", read_only=True
    )
    async def read_pane(pane: str, lines: int = 50, node: str | None = None) -> dict[str, Any]:
        return await _run(lambda actor: ops().pane_read(actor, pane, lines=lines, node=node))

    @registry.tool(
        description=(
            "Wait until a pane's screen matches the `pattern` regex. `reason` is matched, "
            f"timeout, or pane_lost; `timeout_seconds` is capped at 300. {_NODE}"
        )
    )
    async def wait_for_pane_output(
        pane: str,
        pattern: str,
        timeout_seconds: float = MCP_WRAPPER_WAIT_TOOL_TIMEOUT_SECONDS,
        poll_interval_seconds: float = 2.0,
        node: str | None = None,
    ) -> dict[str, Any]:
        timeout = clamp_wait_tool_timeout(
            "wait_for_pane_output",
            timeout_seconds,
            default=MCP_WRAPPER_WAIT_TOOL_TIMEOUT_SECONDS,
        )
        return await _run(
            lambda actor: ops().pane_wait_for_output(
                actor,
                pane,
                pattern,
                timeout_seconds=timeout,
                poll_interval_seconds=poll_interval_seconds,
                node=node,
            )
        )

    return registry
