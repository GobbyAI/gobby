import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ComponentProps } from "react";

import { TaskQuickMenu, type TaskContextMenu } from "../TaskQuickMenu";
import type { GobbyTask } from "../../../types/tasks";

function makeTask(overrides: Partial<GobbyTask> = {}): GobbyTask {
  const base = {
    id: "task-1",
    ref: "#13909",
    title: "Sample task",
    status: "open",
    state: null,
    priority: 2,
    task_type: "task",
    parent_task_id: null,
    created_at: "2026-05-14T17:00:00Z",
    updated_at: "2026-05-14T17:55:00Z",
    seq_num: 13909,
    path_cache: null,
    requires_user_review: false,
    agent_name: null,
    sequence_order: null,
    start_date: null,
    due_date: null,
    project_id: "proj-1",
    current_stage: null,
    stages: [],
    allow_automation: null,
    yolo: null,
    checkout_mode: null,
  } satisfies GobbyTask;
  return { ...base, ...overrides };
}

function renderMenu(
  task: GobbyTask,
  overrides: Partial<ComponentProps<typeof TaskQuickMenu>> = {},
) {
  const menu: TaskContextMenu = { x: 0, y: 0, task };
  const noop = () => {};
  return render(
    <TaskQuickMenu
      menu={menu}
      chatSessionId="sess-1"
      activeAction={null}
      onClose={noop}
      onAssignToMainChat={noop}
      onReleaseClaim={noop}
      onCloseTask={noop}
      onReopenTask={noop}
      {...overrides}
    />,
  );
}

it("offers lifecycle actions without retired build controls", () => {
  renderMenu(makeTask());
  expect(
    screen.getAllByRole("menuitem").map((item) => item.textContent),
  ).toEqual(["Assign to Main Chat", "Close..."]);
});

describe("TaskQuickMenu — lifecycle actions", () => {
  it("supports keyboard navigation across enabled menu items", () => {
    const onClose = vi.fn();
    renderMenu(makeTask(), { onClose });
    const menu = screen.getByRole("menu", { name: "Task actions" });
    const assign = screen.getByRole("menuitem", {
      name: "Assign to Main Chat",
    });
    const close = screen.getByRole("menuitem", { name: "Close..." });

    expect(document.activeElement).toBe(assign);
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(document.activeElement).toBe(close);
    fireEvent.keyDown(menu, { key: "End" });
    expect(document.activeElement).toBe(close);
    fireEvent.keyDown(menu, { key: "Home" });
    expect(document.activeElement).toBe(assign);
    fireEvent.keyDown(menu, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("starts keyboard navigation from the first item when nothing is focused", () => {
    renderMenu(makeTask());
    const menu = screen.getByRole("menu", { name: "Task actions" });
    const assign = screen.getByRole("menuitem", {
      name: "Assign to Main Chat",
    });

    (document.activeElement as HTMLElement | null)?.blur();
    expect(document.activeElement).not.toBe(assign);
    fireEvent.keyDown(menu, { key: "ArrowDown" });

    expect(document.activeElement).toBe(assign);
  });
});
