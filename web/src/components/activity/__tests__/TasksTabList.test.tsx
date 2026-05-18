import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { TasksTabList } from "../TasksTabList";
import type { VisibleTaskRow } from "../TasksTabModel";
import { makeTask } from "./fixtures";

function rows(open = true): VisibleTaskRow[] {
  const child = {
    id: "child",
    task: makeTask({
      id: "child",
      ref: "#102",
      seq_num: 102,
      title: "Child task",
      parent_task_id: "parent",
    }),
    children: [],
  };
  const parent = {
    id: "parent",
    task: makeTask({
      id: "parent",
      ref: "#101",
      seq_num: 101,
      title: "Parent task",
    }),
    children: [child],
  };
  return [
    { node: parent, depth: 0, isInternal: true, isOpen: open },
    ...(open ? [{ node: child, depth: 1, isInternal: false, isOpen: false }] : []),
  ];
}

function renderKeyboardList(open = true) {
  const onSelect = vi.fn();
  const onToggleOpen = vi.fn();

  function Host() {
    const [selectedTaskId, setSelectedTaskId] = useState("parent");
    return (
      <TasksTabList
        visibleRows={rows(open)}
        isEmpty={false}
        isLoading={false}
        hasAnyTasks
        selectedTaskId={selectedTaskId}
        activeTaskActionId={null}
        onSelect={(taskId) => {
          setSelectedTaskId(taskId);
          onSelect(taskId);
        }}
        onToggleOpen={onToggleOpen}
        onMenuButtonClick={vi.fn()}
      />
    );
  }

  render(<Host />);
  return { onSelect, onToggleOpen };
}

describe("TasksTabList keyboard navigation", () => {
  it("moves focus and selection with ArrowDown and ArrowUp", async () => {
    const { onSelect } = renderKeyboardList();
    const parent = screen.getByRole("treeitem", { name: /Parent task/ });
    const child = screen.getByRole("treeitem", { name: /Child task/ });

    parent.focus();
    fireEvent.keyDown(parent, { key: "ArrowDown" });

    await waitFor(() => expect(document.activeElement).toBe(child));
    expect(onSelect).toHaveBeenCalledWith("child");

    fireEvent.keyDown(child, { key: "ArrowUp" });
    await waitFor(() => expect(document.activeElement).toBe(parent));
    expect(onSelect).toHaveBeenCalledWith("parent");
  });

  it("uses ArrowRight and ArrowLeft for ARIA tree expansion", () => {
    const { onToggleOpen } = renderKeyboardList(false);
    const parent = screen.getByRole("treeitem", { name: /Parent task/ });

    fireEvent.keyDown(parent, { key: "ArrowRight" });
    expect(onToggleOpen).toHaveBeenCalledWith("parent");

    cleanup();
    const open = renderKeyboardList(true);
    const openParent = screen.getByRole("treeitem", { name: /Parent task/ });
    fireEvent.keyDown(openParent, { key: "ArrowLeft" });
    expect(open.onToggleOpen).toHaveBeenCalledWith("parent");
  });
});
