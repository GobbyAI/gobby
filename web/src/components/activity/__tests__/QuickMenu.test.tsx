import { fireEvent, render, screen } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";

import { McpQuickMenu } from "../mcp/McpQuickMenu";
import { QuickMenu } from "../QuickMenu";
import { TaskQuickMenu, type TaskContextMenu } from "../TaskQuickMenu";
import type { GobbyTask } from "../../../types/tasks";

function rect(
  left: number,
  top: number,
  width: number,
  height: number,
): DOMRect {
  return {
    x: left,
    y: top,
    left,
    top,
    width,
    height,
    right: left + width,
    bottom: top + height,
    toJSON: () => ({}),
  } as DOMRect;
}

function mockMenuGeometry() {
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
    function getBoundingClientRect(this: HTMLElement) {
      if (this.getAttribute("aria-label") === "Open row actions") {
        return rect(360, 800, 44, 44);
      }
      if (this.getAttribute("role") === "menu") {
        return rect(0, 0, 180, 160);
      }
      return rect(0, 0, 0, 0);
    },
  );
}

function makeTask(overrides: Partial<GobbyTask> = {}): GobbyTask {
  return {
    id: "task-1",
    ref: "#17016",
    title: "Quick menu task",
    status: "open",
    state: null,
    priority: 2,
    task_type: "task",
    parent_task_id: null,
    created_at: "2026-06-13T00:00:00Z",
    updated_at: "2026-06-13T00:00:00Z",
    seq_num: 17016,
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
    isolation: null,
    ...overrides,
  } satisfies GobbyTask;
}

describe("QuickMenu (#17016)", () => {
  it("flips above and clamps horizontally at the viewport edge", () => {
    mockMenuGeometry();
    vi.stubGlobal("innerWidth", 390);
    vi.stubGlobal("innerHeight", 844);

    render(
      <QuickMenu
        triggerLabel="Open row actions"
        menuLabel="Row actions"
        items={[{ label: "Inspect", onSelect: vi.fn() }]}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Open row actions" }));

    const menu = screen.getByRole("menu", { name: "Row actions" });
    expect(menu).toHaveStyle({ left: "202px", top: "636px" });
  });

  it("supports outside click, Escape, and roving keyboard selection", () => {
    const onFirst = vi.fn();
    const onLast = vi.fn();
    const onOpenChange = vi.fn();

    render(
      <QuickMenu
        triggerLabel="Open row actions"
        menuLabel="Row actions"
        onOpenChange={onOpenChange}
        items={[
          { label: "First", onSelect: onFirst },
          { label: "Disabled", disabled: true, onSelect: vi.fn() },
          { label: "Last", onSelect: onLast },
        ]}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Open row actions" }));
    const menu = screen.getByRole("menu", { name: "Row actions" });
    const first = screen.getByRole("menuitem", { name: "First" });
    const last = screen.getByRole("menuitem", { name: "Last" });

    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(document.activeElement).toBe(last);
    fireEvent.keyDown(menu, { key: "Home" });
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(menu, { key: "End" });
    expect(document.activeElement).toBe(last);
    fireEvent.keyDown(menu, { key: "Enter" });
    expect(onLast).toHaveBeenCalledOnce();
    expect(onOpenChange).toHaveBeenLastCalledWith(false);

    fireEvent.click(screen.getByRole("button", { name: "Open row actions" }));
    fireEvent.keyDown(screen.getByRole("menu", { name: "Row actions" }), {
      key: "Escape",
    });
    expect(screen.queryByRole("menu", { name: "Row actions" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Open row actions" }));
    fireEvent.click(document.querySelector(".session-ctx-backdrop") as HTMLElement);
    expect(screen.queryByRole("menu", { name: "Row actions" })).toBeNull();
  });

  it("returns focus to the trigger when the menu closes on Escape", () => {
    render(
      <QuickMenu
        triggerLabel="Open row actions"
        menuLabel="Row actions"
        items={[{ label: "First", onSelect: vi.fn() }]}
      />,
    );

    const trigger = screen.getByRole("button", { name: "Open row actions" });
    fireEvent.click(trigger);
    fireEvent.keyDown(screen.getByRole("menu", { name: "Row actions" }), {
      key: "Escape",
    });

    expect(screen.queryByRole("menu", { name: "Row actions" })).toBeNull();
    expect(document.activeElement).toBe(trigger);
  });

  it("renders destructive items with an icon so hue is not the only signal", () => {
    render(
      <QuickMenu
        triggerLabel="Open row actions"
        menuLabel="Row actions"
        items={[{ label: "Remove server...", destructive: true, onSelect: vi.fn() }]}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Open row actions" }));

    const item = screen.getByRole("menuitem", { name: "Remove server..." });
    expect(item.querySelector("svg")).toBeTruthy();
  });

  it("keeps TaskQuickMenu and McpQuickMenu free of ad-hoc fixed coordinates", () => {
    const taskSource = readFileSync(
      "src/components/activity/TaskQuickMenu.tsx",
      "utf8",
    );
    const mcpSource = readFileSync(
      "src/components/activity/mcp/McpQuickMenu.tsx",
      "utf8",
    );

    expect(taskSource).not.toContain('position: "fixed"');
    expect(taskSource).not.toContain("left: menu.x");
    expect(taskSource).not.toContain("top: menu.y");
    expect(mcpSource).not.toContain('position: "fixed"');
    expect(mcpSource).not.toContain("left: menu.x");
    expect(mcpSource).not.toContain("top: menu.y");
  });

  it("renders TaskQuickMenu and McpQuickMenu through the shared menu semantics", () => {
    const taskMenu: TaskContextMenu = {
      x: 10,
      y: 20,
      width: 44,
      height: 44,
      task: makeTask({ build_state: "never_started" }),
    };

    const { unmount } = render(
      <TaskQuickMenu
        menu={taskMenu}
        chatSessionId="session-1"
        activeAction={null}
        onClose={vi.fn()}
        onAssignToMainChat={vi.fn()}
        onBuild={vi.fn()}
        onBuildQuick={vi.fn()}
        onStopBuild={vi.fn()}
        onResumeBuild={vi.fn()}
        onReleaseClaim={vi.fn()}
        onCloseTask={vi.fn()}
        onReopenTask={vi.fn()}
      />,
    );

    expect(screen.getByRole("menu", { name: "Task actions" })).toBeTruthy();
    expect(screen.getByRole("menuitem", { name: "Build" })).toBeTruthy();

    unmount();

    render(
      <McpQuickMenu
        menu={{
          x: 10,
          y: 20,
          width: 44,
          height: 44,
          kind: "server",
          serverName: "context7",
          isExternal: true,
          enabled: true,
        }}
        onClose={vi.fn()}
        onViewSchema={vi.fn()}
        onCallTool={vi.fn()}
        onViewServer={vi.fn()}
        onRefreshServer={vi.fn()}
        onToggleEnabled={vi.fn()}
        onRemoveServer={vi.fn()}
      />,
    );

    expect(screen.getByRole("menu", { name: "MCP server actions" })).toBeTruthy();
    expect(screen.getByRole("menuitem", { name: "Remove server..." })).toBeTruthy();
  });
});
