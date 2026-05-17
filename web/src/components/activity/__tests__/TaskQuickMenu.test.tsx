import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";

import { TaskQuickMenu, type TaskContextMenu } from "../TaskQuickMenu";
import type { BuildState, GobbyTask } from "../../../hooks/useTasks";

function makeTask(overrides: Partial<GobbyTask> = {}): GobbyTask {
  return {
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
    ...overrides,
  } as GobbyTask;
}

function renderMenu(task: GobbyTask) {
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
    />,
  );
}

const buildLabels = ["Build", "Build Quick", "Stop Build", "Resume Build"];

function visibleBuildButtons(): string[] {
  return buildLabels.filter((label) =>
    screen.queryByRole("button", { name: label }),
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
    expect(screen.queryByRole("button", { name: "Resume Build" })).toBeNull();
    expect(screen.getByRole("button", { name: "Build" })).toBeTruthy();
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
});
