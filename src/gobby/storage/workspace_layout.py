"""Pure split-tree layout functions for workspace tabs, and the errors they raise."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from typing import Literal, TypedDict

from gobby.utils.uuid_validation import parse_uuid_reference

type LayoutAxis = Literal["horizontal", "vertical"]


class WorkspaceNotFoundError(LookupError):
    """A workspace, tab, pane, or node reference matched no row."""


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
