"""Node-scoped workspaces, tabs, and panes with reusable ``node:workspace:tab:pane`` refs.

Every ref is the lowest non-negative integer free among its siblings, picked under a
``SELECT ... FOR UPDATE`` lock on the parent row inside the mutation's single
transaction, so a released number is reused. A mutation spanning two parent rows
locks them in ascending id order. The parent-row lock is the only serialization.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal, TypedDict
from uuid import uuid4

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from gobby.storage.hub.protocol import HubDatabase, Row, Transaction
from gobby.storage.machines import (
    LocalMachineManager,
    Machine,
    MachineNotRegisteredError,
    lowest_free_ref,
    parse_node_ref,
)
from gobby.storage.terminals import truncate_title
from gobby.utils.machine_id import require_machine_id
from gobby.utils.uuid_validation import parse_uuid_reference

DEFAULT_WORKSPACE_NAME = "default"

# Left-anchored and zero-based: `w`, `n:w`, `n:w:t`, `n:w:t:p`.
_REF_RE = re.compile(r"[0-9]+(?::[0-9]+){0,3}")
_DIGITS_RE = re.compile(r"[0-9]+")

type LayoutAxis = Literal["horizontal", "vertical"]
type _Table = Literal["machines", "workspaces", "workspace_tabs", "workspace_panes"]
_PARENT_COLUMN: dict[_Table, str] = {
    "workspaces": "machine_id",
    "workspace_tabs": "workspace_id",
    "workspace_panes": "tab_id",
}


class WorkspaceNotFoundError(LookupError):
    """A workspace, tab, pane, or node reference matched no row."""


class InvalidWorkspaceRefError(ValueError):
    """A reference is malformed: not a uuid, a name, or ``w``/``n:w``/``n:w:t``/``n:w:t:p``."""


class InvalidWorkspaceOpError(ValueError):
    """A mutation's arguments would break a row or layout invariant."""


class LayoutLeaf(TypedDict):
    kind: Literal["pane"]
    pane_id: str


class LayoutSplit(TypedDict):
    kind: Literal["split"]
    axis: LayoutAxis
    ratio: float
    children: list[LayoutNode]


type LayoutNode = LayoutLeaf | LayoutSplit


def validate_layout(value: object) -> LayoutNode:
    """Return ``value`` as a tab's split tree, or raise when it is not one.

    A tree is a ``pane`` leaf naming a pane uuid, or a ``split`` with an axis, a
    ratio strictly between 0 and 1, and exactly two children. No pane repeats.
    """
    return _validate_node(value, set())


def layout_pane_ids(layout: LayoutNode) -> list[str]:
    """Return the tree's pane ids in reading order."""
    if layout["kind"] == "pane":
        return [layout["pane_id"]]
    return [pane_id for child in layout["children"] for pane_id in layout_pane_ids(child)]


def _validate_node(value: object, seen: set[str]) -> LayoutNode:
    if not isinstance(value, Mapping):
        raise InvalidWorkspaceOpError("Layout node must be an object")
    kind = value.get("kind")
    if kind == "pane":
        pane_id = parse_uuid_reference(value.get("pane_id"))
        if pane_id is None or str(pane_id) in seen:
            raise InvalidWorkspaceOpError("Layout leaf needs a pane uuid that appears once")
        seen.add(str(pane_id))
        return _leaf(str(pane_id))
    if kind == "split":
        children = value.get("children")
        if not isinstance(children, list) or len(children) != 2:
            raise InvalidWorkspaceOpError("Layout split needs exactly two children")
        return _split(
            _axis(value.get("axis")),
            _ratio(value.get("ratio")),
            [_validate_node(child, seen) for child in children],
        )
    raise InvalidWorkspaceOpError(f"Unknown layout node kind {kind!r}")


def _axis(value: object) -> LayoutAxis:
    if value == "horizontal":
        return "horizontal"
    if value == "vertical":
        return "vertical"
    raise InvalidWorkspaceOpError(f"Split axis must be horizontal or vertical, not {value!r}")


def _ratio(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value < 1:
        raise InvalidWorkspaceOpError(f"Split ratio must be between 0 and 1, not {value!r}")
    return float(value)


def _leaf(pane_id: str) -> LayoutLeaf:
    return {"kind": "pane", "pane_id": pane_id}


def _split(axis: LayoutAxis, ratio: float, children: list[LayoutNode]) -> LayoutSplit:
    return {"kind": "split", "axis": axis, "ratio": ratio, "children": children}


def _map_leaves(layout: LayoutNode, replace: Callable[[LayoutLeaf], LayoutNode]) -> LayoutNode:
    if layout["kind"] == "pane":
        return replace(layout)
    children = [_map_leaves(child, replace) for child in layout["children"]]
    return _split(layout["axis"], layout["ratio"], children)


def _without_panes(layout: LayoutNode, pane_ids: Collection[str]) -> LayoutNode | None:
    """Drop leaves; a split left with one child collapses to that survivor."""
    if layout["kind"] == "pane":
        return None if layout["pane_id"] in pane_ids else layout
    kept = [
        survivor
        for child in layout["children"]
        if (survivor := _without_panes(child, pane_ids)) is not None
    ]
    if len(kept) == 2:
        return _split(layout["axis"], layout["ratio"], kept)
    return kept[0] if kept else None


def _place(
    layout: LayoutNode | None, pane_id: str, beside: str | None, axis: LayoutAxis
) -> LayoutNode:
    """Split ``beside`` (the whole tree when None) to hold ``pane_id`` second."""
    leaf = _leaf(pane_id)
    if layout is None:
        return leaf
    if beside is None:
        return _split(axis, 0.5, [layout, leaf])
    if beside not in layout_pane_ids(layout):
        raise WorkspaceNotFoundError(f"Pane {beside} is not in the target tab")
    return _map_leaves(
        layout,
        lambda node: _split(axis, 0.5, [node, leaf]) if node["pane_id"] == beside else node,
    )


def _with_ratio(layout: LayoutNode, pane_id: str, ratio: float) -> LayoutNode:
    """Set the ratio of the split whose direct child is ``pane_id``'s leaf."""
    if layout["kind"] == "pane":
        return layout
    children = layout["children"]
    is_parent = any(child["kind"] == "pane" and child["pane_id"] == pane_id for child in children)
    return _split(
        layout["axis"],
        ratio if is_parent else layout["ratio"],
        [_with_ratio(child, pane_id, ratio) for child in children],
    )


def _uuid(value: str) -> str:
    parsed = parse_uuid_reference(value.strip())
    if parsed is None:
        raise InvalidWorkspaceRefError(f"{value!r} is not a uuid")
    return str(parsed)


def mint_pane_id() -> str:
    """Mint a pane row identity before its insert; runtimes never generate their own."""
    return str(uuid4())


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _looks_like_ref(text: str) -> bool:
    return ":" in text or _DIGITS_RE.fullmatch(text) is not None


def _parse_ref(text: str) -> tuple[str | None, str, str | None, str | None]:
    """Split ``w``, ``n:w``, ``n:w:t``, or ``n:w:t:p`` into node, workspace, tab, and pane."""
    if _REF_RE.fullmatch(text) is None:
        raise InvalidWorkspaceRefError(f"Malformed workspace ref {text!r}")
    parts = text.split(":")
    if len(parts) == 1:
        return None, parts[0], None, None
    node_ref, workspace_ref, *rest = parts
    tab_ref = rest[0] if rest else None
    pane_ref = rest[1] if len(rest) > 1 else None
    return node_ref, workspace_ref, tab_ref, pane_ref


def _workspace_name(name: str) -> str:
    clean = name.strip()
    if not clean or parse_uuid_reference(clean) is not None or _looks_like_ref(clean):
        raise InvalidWorkspaceOpError(f"Workspace name {name!r} is empty or reads as a ref or uuid")
    return clean


@dataclass
class Workspace:
    """One workspaces row: a named layout owned by a node."""

    id: str
    machine_id: str
    ref: int
    name: str
    focused_project_id: str | None
    focused_tab_id: str | None
    created_at: datetime
    updated_at: datetime
    default_project_id: str | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Workspace:
        return cls(
            id=str(row["id"]),
            machine_id=str(row["machine_id"]),
            ref=int(row["ref"]),
            name=str(row["name"]),
            focused_project_id=_optional_str(row["focused_project_id"]),
            focused_tab_id=_optional_str(row["focused_tab_id"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            default_project_id=_optional_str(row["default_project_id"]),
        )

    def to_dict(self) -> dict[str, Any]:
        # A projectless scratch omits the association, as gclient's WorkspaceRow expects.
        payload = asdict(self)
        if payload["default_project_id"] is None:
            del payload["default_project_id"]
        return payload


@dataclass
class WorkspaceTab:
    """One workspace_tabs row with its validated split tree."""

    id: str
    workspace_id: str
    ref: int
    title: str | None
    project_id: str
    worktree_id: str | None
    position: int
    focused_pane_id: str | None
    layout: LayoutNode
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> WorkspaceTab:
        return cls(
            id=str(row["id"]),
            workspace_id=str(row["workspace_id"]),
            ref=int(row["ref"]),
            title=_optional_str(row["title"]),
            project_id=str(row["project_id"]),
            worktree_id=_optional_str(row["worktree_id"]),
            position=int(row["position"]),
            focused_pane_id=_optional_str(row["focused_pane_id"]),
            # The hub pool serializes jsonb values to text at the row boundary.
            layout=validate_layout(json.loads(row["layout"])),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class WorkspacePane:
    """One workspace_panes row; terminal_id is NULL while its spawn is in flight."""

    id: str
    tab_id: str
    ref: int
    terminal_id: str | None
    owns_terminal: bool
    label: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> WorkspacePane:
        return cls(
            id=str(row["id"]),
            tab_id=str(row["tab_id"]),
            ref=int(row["ref"]),
            terminal_id=_optional_str(row["terminal_id"]),
            owns_terminal=bool(row["owns_terminal"]),
            label=_optional_str(row["label"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WorkspaceTarget:
    """A resolved reference: the node and workspace, plus the tab and pane it names."""

    node: Machine
    workspace: Workspace
    tab: WorkspaceTab | None = None
    pane: WorkspacePane | None = None


@dataclass(frozen=True)
class LayoutChange:
    """Rows one layout mutation wrote or removed, for the caller's workspace events."""

    panes: tuple[WorkspacePane, ...] = ()
    tabs: tuple[WorkspaceTab, ...] = ()
    removed_panes: tuple[WorkspacePane, ...] = ()
    removed_tabs: tuple[WorkspaceTab, ...] = ()


def _required(row: Row | None, what: str) -> Row:
    if row is None:
        raise WorkspaceNotFoundError(f"{what} not found")
    return row


def _lock_rows(conn: Transaction, table: _Table, *ids: str) -> dict[str, Row]:
    """Lock parent rows ``FOR UPDATE`` in ascending id order."""
    rows: dict[str, Row] = {}
    for row_id in sorted(set(ids)):
        sql = f"SELECT * FROM {table} WHERE id = %s FOR UPDATE"
        rows[row_id] = _required(conn.execute(sql, (row_id,)).fetchone(), f"{table} {row_id}")
    return rows


def _free_ref(conn: Transaction, table: _Table, parent_id: str) -> int:
    sql = f"SELECT ref FROM {table} WHERE {_PARENT_COLUMN[table]} = %s"
    return lowest_free_ref(int(row["ref"]) for row in conn.execute(sql, (parent_id,)).fetchall())


def _pane_tab_id(conn: Transaction, pane_id: str) -> str:
    row = conn.execute("SELECT tab_id FROM workspace_panes WHERE id = %s", (pane_id,)).fetchone()
    return str(_required(row, f"Pane {pane_id}")["tab_id"])


def _insert_pane(conn: Transaction, tab_id: str, pane_id: str) -> WorkspacePane:
    row = conn.execute(
        """
        INSERT INTO workspace_panes (id, tab_id, ref, terminal_id, owns_terminal)
        VALUES (%s, %s, %s, NULL, false)
        RETURNING *
        """,
        (pane_id, tab_id, _free_ref(conn, "workspace_panes", tab_id)),
    ).fetchone()
    return WorkspacePane.from_row(_required(row, f"Pane {pane_id}"))


def _write_layout(conn: Transaction, tab_id: str, layout: LayoutNode) -> WorkspaceTab:
    row = conn.execute(
        "UPDATE workspace_tabs SET layout = %s, updated_at = now() WHERE id = %s RETURNING *",
        (Jsonb(validate_layout(layout)), tab_id),
    ).fetchone()
    return WorkspaceTab.from_row(_required(row, f"Tab {tab_id}"))


def _delete_tab(conn: Transaction, tab_id: str) -> None:
    """Delete a tab row; a workspace focus hint naming it goes with it.

    ``focused_tab_id`` has no foreign key, so the hint is cleared here rather than
    by the schema. The workspace row is touched after the caller's tab locks;
    workspace ops run one at a time on the daemon loop, so that never waits.
    """
    conn.execute("DELETE FROM workspace_tabs WHERE id = %s", (tab_id,))
    conn.execute(
        "UPDATE workspaces SET focused_tab_id = NULL, updated_at = now() WHERE focused_tab_id = %s",
        (tab_id,),
    )


def _drop_from_layout(
    conn: Transaction, tab: WorkspaceTab, pane_ids: Collection[str]
) -> WorkspaceTab | None:
    """Collapse removed panes out of a tab; returns None when the emptied tab is deleted.

    A focus hint naming a removed pane is cleared first, so the returned row never
    seeds a pane that is gone.
    """
    layout = _without_panes(tab.layout, pane_ids)
    if layout is None:
        _delete_tab(conn, tab.id)
        return None
    if tab.focused_pane_id in pane_ids:
        conn.execute("UPDATE workspace_tabs SET focused_pane_id = NULL WHERE id = %s", (tab.id,))
    return _write_layout(conn, tab.id, layout)


def _lock_pane_tabs(
    conn: Transaction, pane_ids: Sequence[str], extra_tab_ids: Sequence[str] = ()
) -> tuple[list[str], dict[str, WorkspaceTab]]:
    """Lock the tabs holding ``pane_ids`` (plus ``extra_tab_ids``) and re-check each home."""
    homes = [_pane_tab_id(conn, pane_id) for pane_id in pane_ids]
    locked = _lock_rows(conn, "workspace_tabs", *homes, *extra_tab_ids)
    if [_pane_tab_id(conn, pane_id) for pane_id in pane_ids] != homes:
        raise WorkspaceNotFoundError("A pane moved to another tab concurrently; retry")
    return homes, {tab_id: WorkspaceTab.from_row(row) for tab_id, row in locked.items()}


class WorkspaceManager:
    """Hub storage for node-scoped workspaces, tabs, and panes.

    The daemon builds exactly one: it owns the process-local set of pane ids
    whose spawn is in flight, which every surface running an op must share.
    """

    def __init__(self, db: HubDatabase) -> None:
        self.db = db
        self._machines = LocalMachineManager(db)
        self._spawns_in_flight: set[str] = set()

    def resolve_node(self, node: str | None = None) -> Machine:
        """Resolve a node by uuid, ref, hostname, or label among the local owner's machines.

        ``None`` is this daemon's own machine: only the receiving daemon answers it.
        """
        local_id = require_machine_id()
        local = self._machines.get(local_id)
        if local is None:
            raise MachineNotRegisteredError(f"Local machine {local_id} is not registered")
        text = (node or "").strip()
        if not text:
            return local
        owner = local.owner_user_id
        if parse_uuid_reference(text) is not None or parse_node_ref(text) is not None:
            found = self._machines.get(text, owner_user_id=owner)
            matches = [] if found is None or found.owner_user_id != owner else [found]
        else:
            owned = self._machines.list_for_user(owner)
            matches = [machine for machine in owned if text in (machine.hostname, machine.label)]
        if len(matches) > 1:
            raise InvalidWorkspaceRefError(f"Node {text!r} matches several machines; use its ref")
        if not matches:
            raise WorkspaceNotFoundError(f"Node {text!r} not found")
        return matches[0]

    def resolve_reference(self, reference: str, *, node: str | None = None) -> WorkspaceTarget:
        """Resolve a workspace, tab, or pane reference.

        A uuid names any of the three rows. ``w``, ``n:w``, ``n:w:t``, or ``n:w:t:p``
        walks refs from its node (``node`` for a lone ``w``). Other text is a
        workspace name on ``node``.
        """
        text = reference.strip()
        uuid_ref = parse_uuid_reference(text)
        if uuid_ref is not None:
            return self._resolve_id(str(uuid_ref))
        tab_ref = pane_ref = None
        if _looks_like_ref(text):
            node_ref, workspace_ref, tab_ref, pane_ref = _parse_ref(text)
            machine = self.resolve_node(node if node_ref is None else node_ref)
            row = self.db.fetchone(
                "SELECT * FROM workspaces WHERE machine_id = %s AND ref = %s",
                (machine.id, int(workspace_ref)),
            )
        else:
            machine = self.resolve_node(node)
            row = self.db.fetchone(
                "SELECT * FROM workspaces WHERE machine_id = %s AND name = %s",
                (machine.id, text),
            )
        workspace = Workspace.from_row(_required(row, f"Workspace {text!r}"))
        if tab_ref is None:
            return WorkspaceTarget(machine, workspace)
        tab_row = self.db.fetchone(
            "SELECT * FROM workspace_tabs WHERE workspace_id = %s AND ref = %s",
            (workspace.id, int(tab_ref)),
        )
        tab = WorkspaceTab.from_row(_required(tab_row, f"Tab {text!r}"))
        if pane_ref is None:
            return WorkspaceTarget(machine, workspace, tab)
        pane_row = self.db.fetchone(
            "SELECT * FROM workspace_panes WHERE tab_id = %s AND ref = %s",
            (tab.id, int(pane_ref)),
        )
        return WorkspaceTarget(
            machine, workspace, tab, WorkspacePane.from_row(_required(pane_row, f"Pane {text!r}"))
        )

    def _resolve_id(self, row_id: str) -> WorkspaceTarget:
        pane_row = self.db.fetchone("SELECT * FROM workspace_panes WHERE id = %s", (row_id,))
        pane = None if pane_row is None else WorkspacePane.from_row(pane_row)
        tab_row = self.db.fetchone(
            "SELECT * FROM workspace_tabs WHERE id = %s",
            (row_id if pane is None else pane.tab_id,),
        )
        tab = None if tab_row is None else WorkspaceTab.from_row(tab_row)
        workspace = self.get(row_id if tab is None else tab.workspace_id)
        node = None if workspace is None else self._machines.get(workspace.machine_id)
        if workspace is None or node is None or (pane is not None and tab is None):
            raise WorkspaceNotFoundError(f"No workspace, tab, or pane has id {row_id}")
        return WorkspaceTarget(node, workspace, tab, pane)

    def create(self, machine_id: str, name: str = DEFAULT_WORKSPACE_NAME) -> tuple[Workspace, bool]:
        """Return the node's workspace named ``name`` and whether this call created it.

        A missing workspace is created with the lowest free ref.
        """
        machine_id, clean_name = _uuid(machine_id), _workspace_name(name)
        with self.db.transaction() as conn:
            _lock_rows(conn, "machines", machine_id)
            row = conn.execute(
                "SELECT * FROM workspaces WHERE machine_id = %s AND name = %s",
                (machine_id, clean_name),
            ).fetchone()
            created = row is None
            if row is None:
                row = conn.execute(
                    "INSERT INTO workspaces (id, machine_id, ref, name) VALUES (%s, %s, %s, %s) "
                    "RETURNING *",
                    (
                        str(uuid4()),
                        machine_id,
                        _free_ref(conn, "workspaces", machine_id),
                        clean_name,
                    ),
                ).fetchone()
        return Workspace.from_row(_required(row, f"Workspace {clean_name!r}")), created

    def list_for_node(self, machine_id: str) -> list[Workspace]:
        rows = self.db.fetchall(
            "SELECT * FROM workspaces WHERE machine_id = %s ORDER BY ref", (_uuid(machine_id),)
        )
        return [Workspace.from_row(row) for row in rows]

    def get(self, workspace_id: str) -> Workspace | None:
        row = self.db.fetchone("SELECT * FROM workspaces WHERE id = %s", (_uuid(workspace_id),))
        return None if row is None else Workspace.from_row(row)

    def list_tabs(self, workspace_id: str) -> list[WorkspaceTab]:
        rows = self.db.fetchall(
            "SELECT * FROM workspace_tabs WHERE workspace_id = %s ORDER BY position, ref",
            (_uuid(workspace_id),),
        )
        return [WorkspaceTab.from_row(row) for row in rows]

    def list_panes(self, workspace_id: str) -> list[WorkspacePane]:
        rows = self.db.fetchall(
            """
            SELECT p.* FROM workspace_panes p
            JOIN workspace_tabs t ON t.id = p.tab_id
            WHERE t.workspace_id = %s
            ORDER BY t.position, t.ref, p.ref
            """,
            (_uuid(workspace_id),),
        )
        return [WorkspacePane.from_row(row) for row in rows]

    def rename(self, workspace_id: str, name: str) -> Workspace:
        clean_name = _workspace_name(name)
        try:
            row = self.db.fetchone(
                "UPDATE workspaces SET name = %s, updated_at = now() WHERE id = %s RETURNING *",
                (clean_name, _uuid(workspace_id)),
            )
        except UniqueViolation as exc:
            raise InvalidWorkspaceOpError(f"Workspace name {clean_name!r} is taken") from exc
        return Workspace.from_row(_required(row, f"Workspace {workspace_id}"))

    def close(self, workspace_id: str) -> Workspace:
        """Delete a workspace with its tabs and panes; its ref becomes free."""
        row = self.db.fetchone(
            "DELETE FROM workspaces WHERE id = %s RETURNING *", (_uuid(workspace_id),)
        )
        return Workspace.from_row(_required(row, f"Workspace {workspace_id}"))

    def create_tab(
        self,
        workspace_id: str,
        *,
        pane_id: str,
        project_id: str,
        worktree_id: str | None = None,
        title: str | None = None,
    ) -> LayoutChange:
        """Append a tab holding one pane; the caller binds the pane's terminal afterwards."""
        workspace_id, pane_id = _uuid(workspace_id), _uuid(pane_id)
        with self.db.transaction() as conn:
            _lock_rows(conn, "workspaces", workspace_id)
            last = conn.execute(
                "SELECT MAX(position) AS position FROM workspace_tabs WHERE workspace_id = %s",
                (workspace_id,),
            ).fetchone()
            last_position = None if last is None else last["position"]
            tab_row = conn.execute(
                """
                INSERT INTO workspace_tabs (
                    id, workspace_id, ref, title, project_id, worktree_id, position, layout
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    str(uuid4()),
                    workspace_id,
                    _free_ref(conn, "workspace_tabs", workspace_id),
                    truncate_title(title),
                    _uuid(project_id),
                    None if worktree_id is None else _uuid(worktree_id),
                    0 if last_position is None else int(last_position) + 1,
                    Jsonb(_leaf(pane_id)),
                ),
            ).fetchone()
            tab = WorkspaceTab.from_row(_required(tab_row, "Tab"))
            pane = _insert_pane(conn, tab.id, pane_id)
        return LayoutChange(panes=(pane,), tabs=(tab,))

    def rename_tab(self, tab_id: str, title: str | None) -> WorkspaceTab:
        row = self.db.fetchone(
            "UPDATE workspace_tabs SET title = %s, updated_at = now() WHERE id = %s RETURNING *",
            (truncate_title(title), _uuid(tab_id)),
        )
        return WorkspaceTab.from_row(_required(row, f"Tab {tab_id}"))

    def move_tab(self, tab_id: str, *, workspace_id: str, position: int) -> LayoutChange:
        """Move a tab to ``position`` in a workspace; a new workspace gives it a free ref."""
        tab_id, target_id = _uuid(tab_id), _uuid(workspace_id)
        with self.db.transaction() as conn:
            source = conn.execute(
                "SELECT workspace_id FROM workspace_tabs WHERE id = %s", (tab_id,)
            ).fetchone()
            source_id = str(_required(source, f"Tab {tab_id}")["workspace_id"])
            _lock_rows(conn, "workspaces", source_id, target_id)
            current = conn.execute(
                "SELECT workspace_id FROM workspace_tabs WHERE id = %s", (tab_id,)
            ).fetchone()
            if str(_required(current, f"Tab {tab_id}")["workspace_id"]) not in (
                source_id,
                target_id,
            ):
                raise WorkspaceNotFoundError(f"Tab {tab_id} moved concurrently; retry")
            # Lock every tab the position rewrite touches in ascending id order, as pane
            # moves do, so the rewrite below never acquires a tab lock out of order.
            conn.execute(
                "SELECT id FROM workspace_tabs WHERE workspace_id = %s OR id = %s "
                "ORDER BY id FOR UPDATE",
                (target_id, tab_id),
            )
            conn.execute(
                """
                UPDATE workspace_tabs SET workspace_id = %s, ref = %s
                WHERE id = %s AND workspace_id <> %s
                """,
                (target_id, _free_ref(conn, "workspace_tabs", target_id), tab_id, target_id),
            )
            # A tab that left its workspace no longer seeds that workspace's focus.
            conn.execute(
                "UPDATE workspaces SET focused_tab_id = NULL, updated_at = now() "
                "WHERE focused_tab_id = %s AND id <> %s",
                (tab_id, target_id),
            )
            order = [
                str(row["id"])
                for row in conn.execute(
                    "SELECT id FROM workspace_tabs WHERE workspace_id = %s AND id <> %s "
                    "ORDER BY position, ref",
                    (target_id, tab_id),
                ).fetchall()
            ]
            order.insert(max(0, min(position, len(order))), tab_id)
            tabs = tuple(
                WorkspaceTab.from_row(
                    _required(
                        conn.execute(
                            "UPDATE workspace_tabs SET position = %s, updated_at = now() "
                            "WHERE id = %s RETURNING *",
                            (index, row_id),
                        ).fetchone(),
                        f"Tab {row_id}",
                    )
                )
                for index, row_id in enumerate(order)
            )
        return LayoutChange(tabs=tabs)

    def close_tab(self, tab_id: str) -> LayoutChange:
        """Delete a tab with its panes; its ref becomes free."""
        tab_id = _uuid(tab_id)
        with self.db.transaction() as conn:
            tab = WorkspaceTab.from_row(_lock_rows(conn, "workspace_tabs", tab_id)[tab_id])
            panes = conn.execute(
                "DELETE FROM workspace_panes WHERE tab_id = %s RETURNING *", (tab_id,)
            ).fetchall()
            _delete_tab(conn, tab_id)
        return LayoutChange(
            removed_panes=tuple(WorkspacePane.from_row(row) for row in panes),
            removed_tabs=(tab,),
        )

    def add_pane(self, pane_id: str, *, beside: str, axis: str) -> LayoutChange:
        """Insert a pane in a new split beside ``beside`` along ``axis``.

        The caller mints ``pane_id`` and calls ``mark_spawn_in_flight`` first; the
        row keeps a NULL terminal until ``set_pane_terminal``.
        """
        pane_id, beside_id, split_axis = _uuid(pane_id), _uuid(beside), _axis(axis)
        with self.db.transaction() as conn:
            (home,), tabs = _lock_pane_tabs(conn, [beside_id])
            pane = _insert_pane(conn, home, pane_id)
            tab = _write_layout(
                conn, home, _place(tabs[home].layout, pane_id, beside_id, split_axis)
            )
        return LayoutChange(panes=(pane,), tabs=(tab,))

    def remove_pane(self, pane_id: str) -> LayoutChange:
        """Delete a pane, collapsing its split to the survivor; an emptied tab goes too."""
        pane_id = _uuid(pane_id)
        with self.db.transaction() as conn:
            (home,), tabs = _lock_pane_tabs(conn, [pane_id])
            removed = conn.execute(
                "DELETE FROM workspace_panes WHERE id = %s RETURNING *", (pane_id,)
            ).fetchall()
            tab = _drop_from_layout(conn, tabs[home], {pane_id})
        return LayoutChange(
            tabs=() if tab is None else (tab,),
            removed_panes=tuple(WorkspacePane.from_row(row) for row in removed),
            removed_tabs=(tabs[home],) if tab is None else (),
        )

    def swap_panes(self, first_pane_id: str, second_pane_id: str) -> WorkspaceTab:
        """Swap two panes' places within one tab's layout."""
        first, second = _uuid(first_pane_id), _uuid(second_pane_id)
        swapped = {first: second, second: first}
        with self.db.transaction() as conn:
            homes, tabs = _lock_pane_tabs(conn, [first, second])
            if homes[0] != homes[1]:
                raise InvalidWorkspaceOpError("Only panes in the same tab can swap")
            layout = _map_leaves(
                tabs[homes[0]].layout,
                lambda leaf: _leaf(swapped.get(leaf["pane_id"], leaf["pane_id"])),
            )
            return _write_layout(conn, homes[0], layout)

    def move_pane(
        self,
        pane_id: str,
        *,
        tab_id: str,
        beside: str | None = None,
        axis: str = "horizontal",
    ) -> LayoutChange:
        """Move a pane beside ``beside`` in ``tab_id`` (splitting the whole tab when None).

        The source split collapses to its survivor and an emptied source tab is
        removed; a pane that changes tab takes the lowest free ref there.
        """
        pane_id, target_id, split_axis = _uuid(pane_id), _uuid(tab_id), _axis(axis)
        beside_id = None if beside is None else _uuid(beside)
        if beside_id == pane_id:
            raise InvalidWorkspaceOpError("A pane cannot move beside itself")
        with self.db.transaction() as conn:
            (home,), tabs = _lock_pane_tabs(conn, [pane_id], [target_id])
            source: WorkspaceTab | None = None
            if home == target_id:
                layout = _without_panes(tabs[target_id].layout, {pane_id})
                pane_row = conn.execute(
                    "SELECT * FROM workspace_panes WHERE id = %s", (pane_id,)
                ).fetchone()
            else:
                layout = tabs[target_id].layout
                pane_row = conn.execute(
                    "UPDATE workspace_panes SET tab_id = %s, ref = %s, updated_at = now() "
                    "WHERE id = %s RETURNING *",
                    (target_id, _free_ref(conn, "workspace_panes", target_id), pane_id),
                ).fetchone()
                source = _drop_from_layout(conn, tabs[home], {pane_id})
            target = _write_layout(conn, target_id, _place(layout, pane_id, beside_id, split_axis))
        moved_away = home != target_id
        return LayoutChange(
            panes=(WorkspacePane.from_row(_required(pane_row, f"Pane {pane_id}")),),
            tabs=(target,) if source is None else (source, target),
            removed_tabs=(tabs[home],) if moved_away and source is None else (),
        )

    def set_ratio(self, pane_id: str, ratio: float) -> WorkspaceTab:
        """Set the ratio of the split directly holding ``pane_id``."""
        pane_id, split_ratio = _uuid(pane_id), _ratio(ratio)
        with self.db.transaction() as conn:
            (home,), tabs = _lock_pane_tabs(conn, [pane_id])
            layout = tabs[home].layout
            if layout["kind"] == "pane":
                raise InvalidWorkspaceOpError("A tab's only pane has no split to resize")
            return _write_layout(conn, home, _with_ratio(layout, pane_id, split_ratio))

    def rename_pane(self, pane_id: str, label: str | None) -> WorkspacePane:
        row = self.db.fetchone(
            "UPDATE workspace_panes SET label = %s, updated_at = now() WHERE id = %s RETURNING *",
            (truncate_title(label), _uuid(pane_id)),
        )
        return WorkspacePane.from_row(_required(row, f"Pane {pane_id}"))

    def set_focus_hints(
        self,
        workspace_id: str,
        *,
        project_id: str | None,
        tab_id: str | None,
        pane_id: str | None,
    ) -> tuple[Workspace, WorkspaceTab | None]:
        """Store the last window's focus as the seed for the next window.

        ``pane_id`` is recorded on ``tab_id``; both must belong to the workspace.
        """
        workspace_id = _uuid(workspace_id)
        focused_tab = None if tab_id is None else _uuid(tab_id)
        focused_pane = None if pane_id is None else _uuid(pane_id)
        if focused_tab is None and focused_pane is not None:
            raise InvalidWorkspaceOpError("A focused pane needs its focused tab")
        with self.db.transaction() as conn:
            row = conn.execute(
                """
                UPDATE workspaces
                SET focused_project_id = %s, focused_tab_id = %s, updated_at = now()
                WHERE id = %s
                RETURNING *
                """,
                (None if project_id is None else _uuid(project_id), focused_tab, workspace_id),
            ).fetchone()
            workspace = Workspace.from_row(_required(row, f"Workspace {workspace_id}"))
            if focused_tab is None:
                return workspace, None
            tab_row = conn.execute(
                """
                UPDATE workspace_tabs SET focused_pane_id = %s, updated_at = now()
                WHERE id = %s AND workspace_id = %s
                  AND (%s::uuid IS NULL OR EXISTS (
                      SELECT 1 FROM workspace_panes WHERE id = %s AND tab_id = %s
                  ))
                RETURNING *
                """,
                (focused_pane, focused_tab, workspace_id, focused_pane, focused_pane, focused_tab),
            ).fetchone()
            if tab_row is None:
                raise InvalidWorkspaceOpError(
                    "Focus hints name a tab or pane outside the workspace"
                )
            return workspace, WorkspaceTab.from_row(tab_row)

    def set_pane_terminal(
        self, pane_id: str, terminal_id: str, *, owns_terminal: bool
    ) -> WorkspacePane | None:
        """Bind a pane to its terminal; None when the pane row is gone.

        Raises psycopg ``UniqueViolation`` when another pane already holds the terminal.
        """
        row = self.db.fetchone(
            """
            UPDATE workspace_panes SET terminal_id = %s, owns_terminal = %s, updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (_uuid(terminal_id), owns_terminal, _uuid(pane_id)),
        )
        return None if row is None else WorkspacePane.from_row(row)

    def get_pane_for_terminal(self, terminal_id: str) -> WorkspacePane | None:
        """The pane holding ``terminal_id``, if any (at most one, by the partial unique index)."""
        row = self.db.fetchone(
            "SELECT * FROM workspace_panes WHERE terminal_id = %s", (_uuid(terminal_id),)
        )
        return None if row is None else WorkspacePane.from_row(row)

    def sweep_dead_panes(self, workspace_id: str) -> LayoutChange:
        """Prune the workspace's dead panes; read-time derivation, never a hook.

        A pane is dead when its terminal is neither ``pending`` nor ``live``, or its
        terminal_id is NULL and its spawn is not in flight here. Splits collapse to
        their survivors and emptied tabs are removed; the workspace itself survives.
        """
        workspace_id = _uuid(workspace_id)
        with self.db.transaction() as conn:
            _lock_rows(conn, "workspaces", workspace_id)
            tabs = [
                WorkspaceTab.from_row(row)
                for row in conn.execute(
                    "SELECT * FROM workspace_tabs WHERE workspace_id = %s ORDER BY id FOR UPDATE",
                    (workspace_id,),
                ).fetchall()
            ]
            candidates = conn.execute(
                """
                SELECT p.id, p.terminal_id FROM workspace_panes p
                JOIN workspace_tabs t ON t.id = p.tab_id
                LEFT JOIN terminals term ON term.id = p.terminal_id
                WHERE t.workspace_id = %s
                  AND (term.state IS NULL OR term.state NOT IN ('pending', 'live'))
                """,
                (workspace_id,),
            ).fetchall()
            dead = [
                str(row["id"])
                for row in candidates
                if row["terminal_id"] is not None or str(row["id"]) not in self._spawns_in_flight
            ]
            if not dead:
                return LayoutChange()
            # Re-check liveness in the delete: a spawn that bound its terminal and
            # left the in-flight set after the read above must survive.
            removed = [
                WorkspacePane.from_row(row)
                for row in conn.execute(
                    """
                    DELETE FROM workspace_panes p
                    WHERE p.id = ANY(%s::uuid[])
                      AND NOT EXISTS (
                          SELECT 1 FROM terminals term
                          WHERE term.id = p.terminal_id AND term.state IN ('pending', 'live')
                      )
                    RETURNING p.*
                    """,
                    (dead,),
                ).fetchall()
            ]
            kept: list[WorkspaceTab] = []
            dropped: list[WorkspaceTab] = []
            for tab in tabs:
                gone = {pane.id for pane in removed if pane.tab_id == tab.id}
                if not gone:
                    continue
                survivor = _drop_from_layout(conn, tab, gone)
                if survivor is None:
                    dropped.append(tab)
                else:
                    kept.append(survivor)
        return LayoutChange(
            tabs=tuple(kept), removed_panes=tuple(removed), removed_tabs=tuple(dropped)
        )

    def mark_spawn_in_flight(self, pane_id: str) -> None:
        """Spare ``pane_id``'s NULL terminal from the sweep until it is cleared."""
        self._spawns_in_flight.add(_uuid(pane_id))

    def clear_spawn_in_flight(self, pane_id: str) -> None:
        self._spawns_in_flight.discard(_uuid(pane_id))

    def is_spawn_in_flight(self, pane_id: str) -> bool:
        return _uuid(pane_id) in self._spawns_in_flight
