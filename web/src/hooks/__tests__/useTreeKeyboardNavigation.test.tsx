import { useMemo, useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import {
  useTreeKeyboardNavigation,
  type TreeNavItem,
} from "../useTreeKeyboardNavigation";

// Two top-level parents; "a" has two children, "b" has one.
const PARENTS = ["a", "b"] as const;
const CHILDREN: Record<string, string[]> = {
  a: ["a1", "a2"],
  b: ["b1"],
};

function Harness({
  onSelect = () => {},
  selectionFollowsFocus,
}: {
  onSelect?: (id: string) => void;
  selectionFollowsFocus?: boolean;
}) {
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const items = useMemo<TreeNavItem[]>(() => {
    const rows: TreeNavItem[] = [];
    for (const parent of PARENTS) {
      const isExpanded = expanded.has(parent);
      rows.push({ id: parent, depth: 0, isExpandable: true, isExpanded });
      if (!isExpanded) continue;
      for (const child of CHILDREN[parent] ?? []) {
        rows.push({ id: child, depth: 1, isExpandable: false, isExpanded: false });
      }
    }
    return rows;
  }, [expanded]);

  const handleSelect = (id: string) => {
    setSelectedId(id);
    onSelect(id);
  };

  const handleToggle = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const { setRowRef, handleKeyDown, getTabIndex } = useTreeKeyboardNavigation({
    items,
    selectedId,
    onSelect: handleSelect,
    onToggle: handleToggle,
    selectionFollowsFocus,
  });

  return (
    <div role="tree" aria-label="Test tree">
      {items.map((item) => (
        <div
          key={item.id}
          data-testid={`row-${item.id}`}
          ref={(node) => setRowRef(item.id, node)}
          role="treeitem"
          aria-level={item.depth + 1}
          aria-expanded={item.isExpandable ? item.isExpanded : undefined}
          aria-selected={selectedId === item.id}
          tabIndex={getTabIndex(item.id)}
          onClick={() => handleSelect(item.id)}
          onKeyDown={(event) => handleKeyDown(item.id, event)}
        >
          <span>{item.id}</span>
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              handleToggle(item.id);
            }}
          >
            {`toggle-${item.id}`}
          </button>
        </div>
      ))}
    </div>
  );
}

function WindowedHarness() {
  const items = useMemo<TreeNavItem[]>(
    () => [
      { id: "a", depth: 0, isExpandable: false, isExpanded: false },
      { id: "b", depth: 0, isExpandable: false, isExpanded: false },
    ],
    [],
  );
  const [selectedId, setSelectedId] = useState<string | null>("a");
  const [mountedId, setMountedId] = useState("a");
  const { setRowRef, handleKeyDown, getTabIndex } = useTreeKeyboardNavigation({
    items,
    selectedId,
    onSelect: setSelectedId,
    onToggle: () => {},
    onFocusRequest: setMountedId,
  });

  return (
    <div role="tree" aria-label="Windowed tree">
      <div
        data-testid={`windowed-row-${mountedId}`}
        ref={(node) => setRowRef(mountedId, node)}
        role="treeitem"
        tabIndex={getTabIndex(mountedId)}
        onKeyDown={(event) => handleKeyDown(mountedId, event)}
      >
        {mountedId}
      </div>
    </div>
  );
}

describe("useTreeKeyboardNavigation", () => {
  it("makes the first row the tab entry point when nothing is selected", () => {
    render(<Harness />);
    expect(screen.getByTestId("row-a")).toHaveAttribute("tabindex", "0");
    expect(screen.getByTestId("row-b")).toHaveAttribute("tabindex", "-1");
  });

  it("expands and collapses parents with Arrow Right/Left", () => {
    render(<Harness />);
    fireEvent.keyDown(screen.getByTestId("row-a"), { key: "ArrowRight" });
    expect(screen.getByTestId("row-a")).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("row-a1")).toBeInTheDocument();

    fireEvent.keyDown(screen.getByTestId("row-a"), { key: "ArrowLeft" });
    expect(screen.getByTestId("row-a")).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("row-a1")).toBeNull();
  });

  it("moves the roving anchor down, up, into children, and to the parent", () => {
    render(<Harness />);
    // Expand "a", then step into its first child.
    fireEvent.keyDown(screen.getByTestId("row-a"), { key: "ArrowRight" });
    fireEvent.keyDown(screen.getByTestId("row-a"), { key: "ArrowRight" });
    expect(screen.getByTestId("row-a1")).toHaveAttribute("tabindex", "0");
    expect(screen.getByTestId("row-a")).toHaveAttribute("tabindex", "-1");

    fireEvent.keyDown(screen.getByTestId("row-a1"), { key: "ArrowDown" });
    expect(screen.getByTestId("row-a2")).toHaveAttribute("tabindex", "0");

    fireEvent.keyDown(screen.getByTestId("row-a2"), { key: "ArrowUp" });
    expect(screen.getByTestId("row-a1")).toHaveAttribute("tabindex", "0");

    // ArrowLeft on a leaf moves to its parent.
    fireEvent.keyDown(screen.getByTestId("row-a1"), { key: "ArrowLeft" });
    expect(screen.getByTestId("row-a")).toHaveAttribute("tabindex", "0");
  });

  it("jumps to the first and last visible rows with Home and End", () => {
    const onSelect = vi.fn();
    render(<Harness onSelect={onSelect} />);
    // Expand "a" so the visible list is a, a1, a2, b.
    fireEvent.keyDown(screen.getByTestId("row-a"), { key: "ArrowRight" });

    fireEvent.keyDown(screen.getByTestId("row-a"), { key: "End" });
    expect(screen.getByTestId("row-b")).toHaveAttribute("tabindex", "0");
    expect(onSelect).toHaveBeenLastCalledWith("b");

    fireEvent.keyDown(screen.getByTestId("row-b"), { key: "Home" });
    expect(screen.getByTestId("row-a")).toHaveAttribute("tabindex", "0");
    expect(onSelect).toHaveBeenLastCalledWith("a");
  });

  it("selects on Enter and Space", () => {
    const onSelect = vi.fn();
    render(<Harness onSelect={onSelect} />);
    fireEvent.keyDown(screen.getByTestId("row-a"), { key: "Enter" });
    fireEvent.keyDown(screen.getByTestId("row-a"), { key: " " });
    expect(onSelect).toHaveBeenCalledTimes(2);
    expect(onSelect).toHaveBeenCalledWith("a");
  });

  it("selects on arrow navigation when selectionFollowsFocus is true (default)", () => {
    const onSelect = vi.fn();
    render(<Harness onSelect={onSelect} />);
    fireEvent.keyDown(screen.getByTestId("row-a"), { key: "ArrowDown" });
    expect(onSelect).toHaveBeenCalledWith("b");
  });

  it("moves focus without selecting when selectionFollowsFocus is false", () => {
    const onSelect = vi.fn();
    render(<Harness onSelect={onSelect} selectionFollowsFocus={false} />);
    fireEvent.keyDown(screen.getByTestId("row-a"), { key: "ArrowDown" });
    expect(screen.getByTestId("row-b")).toHaveAttribute("tabindex", "0");
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("focuses a virtualized row after the focus request mounts it", () => {
    render(<WindowedHarness />);
    const firstRow = screen.getByTestId("windowed-row-a");
    firstRow.focus();

    fireEvent.keyDown(firstRow, { key: "ArrowDown" });

    expect(screen.queryByTestId("windowed-row-a")).toBeNull();
    expect(screen.getByTestId("windowed-row-b")).toHaveFocus();
  });

  it("ignores key events bubbling from nested controls", () => {
    const onSelect = vi.fn();
    render(<Harness onSelect={onSelect} />);
    // Enter on the nested toggle button bubbles to the row, but the row handler
    // must ignore it (target !== currentTarget) so it does not select the row.
    fireEvent.keyDown(screen.getByText("toggle-a"), { key: "Enter" });
    expect(onSelect).not.toHaveBeenCalled();
  });
});
