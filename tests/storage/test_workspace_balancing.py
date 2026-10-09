"""Physical pane geometry for balanced lane tabs, including divider cells."""

from uuid import uuid4

import pytest

from gobby.storage.workspace_layout import (
    InvalidWorkspaceOpError,
    LayoutNode,
    balanced_layout,
    layout_pane_ids,
)


def pane_sizes(layout: LayoutNode, columns: int, rows: int) -> dict[str, tuple[int, int]]:
    if layout["kind"] == "pane":
        return {layout["pane_id"]: (columns, rows)}
    available = (columns if layout["axis"] == "horizontal" else rows) - 1
    first = round(available * layout["ratio"])
    sizes: dict[str, tuple[int, int]] = {}
    for child, extent in zip(layout["children"], (first, available - first), strict=True):
        width, height = (extent, rows) if layout["axis"] == "horizontal" else (columns, extent)
        sizes.update(pane_sizes(child, width, height))
    return sizes


@pytest.mark.parametrize("columns", [80, 160, 161, 242, 323, 640])
@pytest.mark.parametrize("count", [4, 7, 12])
def test_every_pane_keeps_minimum_width_after_each_launch(columns: int, count: int) -> None:
    manager = str(uuid4())
    layout: LayoutNode = {"kind": "pane", "pane_id": manager}
    expected = [manager]
    for _ in range(count):
        pane = str(uuid4())
        expected.append(pane)
        layout = balanced_layout(
            {
                "kind": "split",
                "axis": "vertical",
                "ratio": 0.5,
                "children": [layout, {"kind": "pane", "pane_id": pane}],
            },
            columns,
            180,
        )
        sizes = pane_sizes(layout, columns, 180)
        assert layout_pane_ids(layout) == expected
        assert min(width for width, _ in sizes.values()) >= 80
        assert min(height for _, height in sizes.values()) >= 12
        assert sizes[manager][0] >= 80


def test_rebalance_repairs_geometric_squeeze_without_observed_pty_widths() -> None:
    panes = [str(uuid4()) for _ in range(7)]
    layout: LayoutNode = {"kind": "pane", "pane_id": panes[0]}
    for pane in panes[1:]:
        layout = {
            "kind": "split",
            "axis": "horizontal",
            "ratio": 0.5,
            "children": [layout, {"kind": "pane", "pane_id": pane}],
        }
    before = pane_sizes(layout, 160, 80)
    assert before[panes[0]][0] < 4
    after = pane_sizes(balanced_layout(layout, 161, 80), 161, 80)
    assert min(width for width, _ in after.values()) >= 80
    assert set(after) == set(before)


def test_small_budget_refuses() -> None:
    layout: LayoutNode = {"kind": "pane", "pane_id": str(uuid4())}
    with pytest.raises(InvalidWorkspaceOpError, match="at least 80 columns"):
        balanced_layout(layout, 79)


@pytest.mark.parametrize("count,columns,rows", [(7, 165, 48), (9, 165, 60), (9, 80, 60)])
def test_crowded_lane_refuses_instead_of_squeezing_height(
    count: int, columns: int, rows: int
) -> None:
    layout: LayoutNode = {"kind": "pane", "pane_id": str(uuid4())}
    for _ in range(count - 1):
        layout = {
            "kind": "split",
            "axis": "horizontal",
            "ratio": 0.5,
            "children": [layout, {"kind": "pane", "pane_id": str(uuid4())}],
        }
    before = layout_pane_ids(layout)
    with pytest.raises(InvalidWorkspaceOpError, match="cannot fit"):
        balanced_layout(layout, columns, rows)
    assert layout_pane_ids(layout) == before


def test_nine_seat_lane_fits_three_columns_at_height_floor() -> None:
    layout: LayoutNode = {"kind": "pane", "pane_id": str(uuid4())}
    for _ in range(8):
        layout = {
            "kind": "split",
            "axis": "horizontal",
            "ratio": 0.5,
            "children": [layout, {"kind": "pane", "pane_id": str(uuid4())}],
        }
    sizes = pane_sizes(balanced_layout(layout, 242, 38), 242, 38)
    assert len(sizes) == 9
    assert set(sizes.values()) == {(80, 12)}
