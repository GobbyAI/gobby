"""Workspace op contract shared by every surface.

Error codes, events, and snapshots live here. Storage failures become
``WorkspaceOpError`` so each surface maps one code set.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal, TypedDict

from psycopg.errors import ForeignKeyViolation

from gobby.agents.constants import (
    GOBBY_NODE_ID,
    GOBBY_NODE_REF,
    GOBBY_PANE_ID,
    GOBBY_PANE_REF,
    GOBBY_TAB_ID,
    GOBBY_WORKSPACE_ID,
)
from gobby.storage.machines import Machine, MachineNotRegisteredError
from gobby.storage.project_checkouts import (
    CheckoutNotFoundError,
    CheckoutSentinelRejectedError,
    MissingMachineContextError,
)
from gobby.storage.workspace_machine_scope import MachineOwnershipMismatchError
from gobby.storage.workspaces import (
    InvalidWorkspaceOpError,
    InvalidWorkspaceRefError,
    Workspace,
    WorkspaceNotFoundError,
    WorkspacePane,
    WorkspaceTab,
    WorkspaceTarget,
)
from gobby.terminals.runtime import SnapshotResult
from gobby.utils.machine_id import require_machine_id

WorkspaceOpErrorCode = Literal[
    "not_found",
    "invalid_ref",
    "invalid_op",
    "terminal_failed",
    "busy",
    "forbidden",
    "shutdown_in_progress",
]
WorkspaceEventKind = Literal[
    "workspace.created",
    "workspace.renamed",
    "workspace.closed",
    "tab.created",
    "tab.renamed",
    "tab.moved",
    "tab.closed",
    "tab.removed",
    "pane.added",
    "pane.swapped",
    "pane.moved",
    "pane.resized",
    "pane.renamed",
    "pane.removed",
    "focus_hints",
]


class WorkspaceOpError(Exception):
    """A failed op; each surface maps ``code`` to its own error shape."""

    def __init__(self, code: WorkspaceOpErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: WorkspaceOpErrorCode = code


class WorkspaceEvent(TypedDict):
    """One ``workspace_event`` payload carrying the rows a mutation changed.

    ``pane.removed`` carries the removed panes and the survivors' rewritten tabs;
    ``tab.removed`` the tabs a removal emptied; ``tab.closed`` the closed tab
    with its panes; ``workspace.*`` and ``focus_hints`` the workspace row.
    """

    kind: WorkspaceEventKind
    workspace_id: str
    workspace: dict[str, Any] | None
    tabs: list[dict[str, Any]]
    panes: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class PaneOutputWait:
    matched: bool
    reason: Literal["matched", "timeout", "pane_lost"]
    snapshot: SnapshotResult | None


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    """A swept workspace with every tab and pane row, and the node that owns it."""

    node: Machine
    workspace: Workspace
    tabs: tuple[WorkspaceTab, ...]
    panes: tuple[WorkspacePane, ...]


@contextmanager
def storage_errors() -> Iterator[None]:
    """Translate storage failures into typed op errors."""
    try:
        yield
    except (
        WorkspaceNotFoundError,
        MachineNotRegisteredError,
        CheckoutNotFoundError,
        ForeignKeyViolation,
    ) as exc:
        raise WorkspaceOpError("not_found", str(exc)) from exc
    except InvalidWorkspaceRefError as exc:
        raise WorkspaceOpError("invalid_ref", str(exc)) from exc
    except (
        InvalidWorkspaceOpError,
        MachineOwnershipMismatchError,
        MissingMachineContextError,
        CheckoutSentinelRejectedError,
    ) as exc:
        raise WorkspaceOpError("invalid_op", str(exc)) from exc


def _workspace_of(target: WorkspaceTarget, reference: str) -> Workspace:
    if target.tab is not None:
        raise WorkspaceOpError("invalid_ref", f"{reference!r} does not name a workspace")
    return target.workspace


def _tab_of(target: WorkspaceTarget, reference: str) -> WorkspaceTab:
    if target.tab is None or target.pane is not None:
        raise WorkspaceOpError("invalid_ref", f"{reference!r} does not name a tab")
    return target.tab


def _pane_of(target: WorkspaceTarget, reference: str) -> tuple[WorkspaceTab, WorkspacePane]:
    if target.tab is None or target.pane is None:
        raise WorkspaceOpError("invalid_ref", f"{reference!r} does not name a pane")
    return target.tab, target.pane


def _require_local(node: Machine) -> None:
    """Refuse another node's rows: its in-flight spawns are invisible to this daemon's sweep."""
    if node.id != require_machine_id():
        name = node.id if node.ref is None else str(node.ref)
        raise WorkspaceOpError(
            "invalid_op",
            f"Workspace ops on node {name} ({node.hostname or 'unnamed'}) run on that node",
        )


def _pane_ref(node: Machine, workspace: Workspace, tab: WorkspaceTab, pane: WorkspacePane) -> str:
    """The pane's ``node:workspace:tab:pane`` address; a pane ref always carries its node."""
    if node.ref is None:
        raise WorkspaceOpError("invalid_op", f"Node {node.id} has no ref to address panes by")
    return f"{node.ref}:{workspace.ref}:{tab.ref}:{pane.ref}"


def _identity_env(
    node: Machine, workspace: Workspace, tab: WorkspaceTab, pane: WorkspacePane
) -> dict[str, str]:
    """Spawn-time pane identity; the runtime adds GOBBY_TERMINAL_ID on top."""
    # Built first because it refuses a node with no ref, which is what makes
    # `node.ref` below a number rather than the string "None".
    pane_ref = _pane_ref(node, workspace, tab, pane)
    return {
        GOBBY_NODE_ID: node.id,
        GOBBY_WORKSPACE_ID: workspace.id,
        GOBBY_TAB_ID: tab.id,
        GOBBY_PANE_ID: pane.id,
        GOBBY_PANE_REF: pane_ref,
        GOBBY_NODE_REF: str(node.ref),
    }
