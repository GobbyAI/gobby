import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { GobbyTask } from "../../../hooks/useTasks";
import type { StageRegistryEntry } from "../../../hooks/useStagesRegistry";
import type { StageStateView } from "../../../lib/stageActions";
import { TasksBoardView } from "../TasksBoardView";

function stage(
  name: string,
  position: number,
  state: StageStateView["state"],
): StageStateView {
  return {
    name,
    display_name: name === "build" ? "Build" : name,
    category: "build",
    state,
    review_policy: "none",
    updated_at: null,
    position,
  };
}

function registryEntry(
  name: string,
  position: number,
): StageRegistryEntry {
  return {
    ...stage(name, position, "ready"),
    display_name: name === "build" ? "Build" : name,
    sequence_order: position,
  } as StageRegistryEntry;
}

function makeTask(id: string, title: string, stages: StageStateView[]): GobbyTask {
  return {
    id,
    ref: `#${id}`,
    title,
    status: "open",
    state: null,
    compat: null,
    priority: 2,
    task_type: "task",
    parent_task_id: null,
    created_at: "2026-05-16T00:00:00Z",
    updated_at: "2026-05-16T00:00:00Z",
    seq_num: 1,
    path_cache: null,
    requires_user_review: false,
    assignee: null,
    agent_name: null,
    sequence_order: null,
    start_date: null,
    due_date: null,
    project_id: "proj-1",
    current_stage: null,
    stages,
  } as GobbyTask;
}

const registry: StageRegistryEntry[] = [
  registryEntry("plan", 0),
  registryEntry("build", 1),
  registryEntry("review", 2),
];

describe("TasksBoardView (#14773 / D6)", () => {
  it("renders lifecycle-stage columns in registry order with counts", () => {
    const onMove = vi.fn().mockResolvedValue(undefined);
    render(
      <TasksBoardView
        tasks={[
          makeTask("a", "Plan it", [
            stage("plan", 0, "in_progress"),
            stage("build", 1, "ready"),
          ]),
          makeTask("b", "Build it", [
            stage("plan", 0, "done"),
            stage("build", 1, "in_progress"),
          ]),
        ]}
        stagesRegistry={registry}
        selectedTaskId={null}
        onSelectTask={vi.fn()}
        onMoveTaskToStage={onMove}
      />,
    );

    const buildColumn = screen.getByRole("region", { name: "Build" });
    expect(buildColumn).toBeInTheDocument();
    // "Build it" sits in the Build lane (canonical stage = first non-done).
    expect(buildColumn).toHaveTextContent("Build it");
    expect(buildColumn).not.toHaveTextContent("Plan it");
  });

  it("buckets tasks with no registry stage into an Unstaged lane", () => {
    render(
      <TasksBoardView
        tasks={[makeTask("z", "Orphan", [stage("retired", 9, "ready")])]}
        stagesRegistry={registry}
        selectedTaskId={null}
        onSelectTask={vi.fn()}
        onMoveTaskToStage={vi.fn().mockResolvedValue(undefined)}
      />,
    );

    expect(
      screen.getByRole("region", { name: "Unstaged" }),
    ).toHaveTextContent("Orphan");
  });

  it("shares selection — clicking a card calls onSelectTask", () => {
    const onSelectTask = vi.fn();
    render(
      <TasksBoardView
        tasks={[makeTask("a", "Plan it", [stage("plan", 0, "in_progress")])]}
        stagesRegistry={registry}
        selectedTaskId={null}
        onSelectTask={onSelectTask}
        onMoveTaskToStage={vi.fn().mockResolvedValue(undefined)}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /Plan it/ }));
    expect(onSelectTask).toHaveBeenCalledWith("a");
  });

  it("surfaces a stage-move error banner", () => {
    render(
      <TasksBoardView
        tasks={[makeTask("a", "Plan it", [stage("plan", 0, "in_progress")])]}
        stagesRegistry={registry}
        selectedTaskId={null}
        onSelectTask={vi.fn()}
        onMoveTaskToStage={vi.fn().mockResolvedValue(undefined)}
        moveErrors={{ a: "Couldn't move stage: review not allowed" }}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(
      "review not allowed",
    );
  });
});
