import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ComponentProps } from "react";

import { TaskQuickMenu, type TaskContextMenu } from "../TaskQuickMenu";
import type { BuildState, GobbyTask } from "../../../hooks/useTasks";

function makeTask(overrides: Partial<GobbyTask> = {}): GobbyTask {
  const base = {
    id: "task-1",
    ref: "#13909",
    title: "Sample task",
    status: "open",
    state: null,
    compat: null,
    priority: 2,
    task_type: "task",
    parent_task_id: null,
    created_at: "2026-05-14T17:00:00Z",
    updated_at: "2026-05-14T17:55:00Z",
    seq_num: 13909,
    path_cache: null,
    requires_user_review: false,
    assignee: null,
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
      onBuild={noop}
      onBuildQuick={noop}
      onStopBuild={noop}
      onResumeBuild={noop}
      onReleaseClaim={noop}
      onCloseTask={noop}
      onReopenTask={noop}
      {...overrides}
    />,
  );
}

const buildLabels = ["Build", "Build Quick", "Stop Build", "Resume Build"];

function visibleBuildButtons(): string[] {
  return buildLabels.filter((label) =>
    screen.queryByRole("menuitem", { name: label }),
  );
}

describe("TaskQuickMenu — build_state drives build controls (#14770 / D3)", () => {
  const cases: Array<[BuildState, string[]]> = [
    ["never_started", ["Build", "Build Quick"]],
    ["running", ["Stop Build"]],
    ["paused", ["Resume Build"]],
  ];

  it.each(cases)(
    "shows exactly the %s controls",
    (buildState, expected) => {
      renderMenu(makeTask({ build_state: buildState }));
      expect(visibleBuildButtons().sort()).toEqual([...expected].sort());
    },
  );

  it("defaults to Start when build_state is absent", () => {
    renderMenu(makeTask({ build_state: undefined }));
    expect(visibleBuildButtons().sort()).toEqual(
      ["Build", "Build Quick"].sort(),
    );
  });

  it("renders menu semantics for task actions", () => {
    renderMenu(makeTask({ build_state: "never_started" }));
    expect(screen.getByRole("menu", { name: "Task actions" })).toBeTruthy();
    expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toContain("Build");
    expect(screen.getAllByRole("separator").length).toBeGreaterThan(0);
  });

  it("does not treat planning scaffolding as a started build (#12010/#13909)", () => {
    // The exact regression: stages + assigned_agent + isolation present, but
    // the build was never started. The old hasBuildEvidence heuristic showed
    // 'Resume Build' here; build_state must show 'Build'.
    renderMenu(
      makeTask({
        build_state: "never_started",
        assigned_agent: "backend-developer",
        additional_skills: ["tech-writer"],
        isolation: "worktree",
        dispatch_failure_count: 3,
        current_stage: {
          name: "development",
          display_name: "Development",
          category: "implementation",
          state: "in_progress",
          review_policy: "required",
          updated_at: "2026-05-14T18:00:00Z",
          position: 10,
        },
      }),
    );
    expect(screen.queryByRole("menuitem", { name: "Resume Build" })).toBeNull();
    expect(screen.getByRole("menuitem", { name: "Build" })).toBeTruthy();
  });

  it("hides all build controls on a closed task", () => {
    renderMenu(
      makeTask({
        build_state: "paused",
        state: { is_closed: true } as GobbyTask["state"],
      }),
    );
    expect(visibleBuildButtons()).toEqual([]);
  });

  it("supports keyboard navigation across enabled menu items", () => {
    const onClose = vi.fn();
    renderMenu(makeTask({ build_state: "never_started" }), { onClose });
    const menu = screen.getByRole("menu", { name: "Task actions" });
    const assign = screen.getByRole("menuitem", { name: "Assign to Main Chat" });
    const build = screen.getByRole("menuitem", { name: "Build" });
    const close = screen.getByRole("menuitem", { name: "Close..." });

    expect(document.activeElement).toBe(assign);
    fireEvent.keyDown(menu, { key: "ArrowDown" });
    expect(document.activeElement).toBe(build);
    fireEvent.keyDown(menu, { key: "End" });
    expect(document.activeElement).toBe(close);
    fireEvent.keyDown(menu, { key: "Home" });
    expect(document.activeElement).toBe(assign);
    fireEvent.keyDown(menu, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("starts keyboard navigation from the first item when nothing is focused", () => {
    renderMenu(makeTask({ build_state: "never_started" }));
    const menu = screen.getByRole("menu", { name: "Task actions" });
    const assign = screen.getByRole("menuitem", { name: "Assign to Main Chat" });

    (document.activeElement as HTMLElement | null)?.blur();
    expect(document.activeElement).not.toBe(assign);
    fireEvent.keyDown(menu, { key: "ArrowDown" });

    expect(document.activeElement).toBe(assign);
  });
});
