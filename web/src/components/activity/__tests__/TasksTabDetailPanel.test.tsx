import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";

import {
  TasksTabDetailPanel,
  type ParentTaskRef,
} from "../TasksTabDetailPanel";
import type { GobbyTaskDetail } from "../../../hooks/useTasks";
import type { StageStateView } from "../../../lib/stageActions";

function makeStage(
  overrides: Partial<StageStateView> = {},
): StageStateView {
  return {
    name: "development",
    display_name: "Development",
    category: "implementation",
    state: "in_progress",
    review_policy: "required",
    updated_at: "2026-05-14T18:00:00Z",
    position: 10,
    ...overrides,
  };
}

function makeTask(overrides: Partial<GobbyTaskDetail> = {}): GobbyTaskDetail {
  const baseStage = makeStage();
  return {
    id: "task-1",
    ref: "#101",
    title: "Sample task",
    status: "in_progress",
    state: null,
    compat: null,
    priority: 2,
    task_type: "task",
    parent_task_id: null,
    created_at: "2026-05-14T17:00:00Z",
    updated_at: "2026-05-14T17:55:00Z",
    seq_num: 101,
    path_cache: "src/path/to/file.ts",
    requires_user_review: false,
    assignee: null,
    agent_name: "codex",
    sequence_order: null,
    start_date: null,
    due_date: null,
    project_id: "proj-1",
    current_stage: baseStage,
    stages: [baseStage],
    description: null,
    labels: null,
    category: "code",
    validation_status: null,
    validation_feedback: null,
    validation_criteria: null,
    validation_fail_count: 0,
    validation_override_reason: null,
    closed_at: null,
    closed_reason: null,
    closed_commit_sha: null,
    commits: null,
    escalated_at: null,
    escalation_reason: null,
    pre_escalation_status: null,
    created_in_session_id: null,
    closed_in_session_id: null,
    complexity_score: null,
    is_expanded: false,
    expansion_status: "none",
    github_pr_number: null,
    github_repo: null,
    allow_automation: null,
    yolo: null,
    isolation: null,
    ...overrides,
  } as GobbyTaskDetail;
}

describe("TasksTabDetailPanel — impeccable redesign (#14686)", () => {
  it("renders the stage name as the hero, not the task title", () => {
    const { container, queryByText } = render(
      <TasksTabDetailPanel task={makeTask()} />,
    );
    const hero = container.querySelector(".activity-task-detail-hero__stage");
    expect(hero?.textContent).toBe("Development");
    // The task title is rendered by the parent pane bar, not the panel hero
    expect(queryByText("Sample task")).toBeNull();
  });

  it("applies the active stage variant when display state is in_progress", () => {
    const { container } = render(
      <TasksTabDetailPanel task={makeTask()} />,
    );
    const hero = container.querySelector(".activity-task-detail-hero__stage");
    expect(hero?.className).toContain("activity-task-detail-hero__stage--active");
  });

  it("applies the escalated stage variant and label when task is escalated", () => {
    const task = makeTask({ escalated_at: "2026-05-14T17:50:00Z" });
    const { container } = render(<TasksTabDetailPanel task={task} />);
    const hero = container.querySelector(".activity-task-detail-hero__stage");
    expect(hero?.textContent).toBe("Escalated");
    expect(hero?.className).toContain(
      "activity-task-detail-hero__stage--escalated",
    );
  });

  it("applies the closed stage variant when task is closed", () => {
    const closedStage = makeStage({ state: "done" });
    const task = makeTask({
      closed_at: "2026-05-14T17:40:00Z",
      stages: [closedStage],
      current_stage: closedStage,
    });
    const { container } = render(<TasksTabDetailPanel task={task} />);
    const hero = container.querySelector(".activity-task-detail-hero__stage");
    expect(hero?.textContent).toBe("Closed");
    expect(hero?.className).toContain(
      "activity-task-detail-hero__stage--closed",
    );
  });

  it("renders agent name and a relative-time string in the hero", () => {
    const { container } = render(
      <TasksTabDetailPanel task={makeTask()} />,
    );
    const agent = container.querySelector(".activity-task-detail-hero__agent");
    expect(agent?.textContent).toContain("Driven by");
    expect(agent?.textContent).toContain("codex");
    expect(agent?.textContent).toMatch(/(just now|ago)/);
  });

  it("uses the mono hero class only for owner session ids", () => {
    const task = makeTask({
      agent_name: null,
      claimed_by_session_id: "session-123",
    });
    const { container } = render(<TasksTabDetailPanel task={task} />);

    const owner = container.querySelector(".activity-task-detail-hero__agent-name");
    expect(owner?.className).toContain("activity-task-detail-hero__agent-name--mono");
  });

  it("renders 'Unassigned' in the hero when no agent or owner session exists", () => {
    const task = makeTask({ agent_name: null });
    const { container } = render(<TasksTabDetailPanel task={task} />);
    const agent = container.querySelector(".activity-task-detail-hero__agent");
    expect(agent?.textContent).toContain("Unassigned");
  });

  it("renders the Validation row with an explicit label when validation_status is set", () => {
    const task = makeTask({
      validation_status: "pending",
      validation_fail_count: 0,
    });
    const { container, getByText } = render(
      <TasksTabDetailPanel task={task} />,
    );
    const row = container.querySelector(".activity-task-detail-validation-row");
    expect(row).not.toBeNull();
    expect(getByText("Validation")).toBeTruthy();
  });

  it("omits the Validation row entirely when validation_status is null (no floating chip)", () => {
    const { container } = render(
      <TasksTabDetailPanel task={makeTask()} />,
    );
    expect(
      container.querySelector(".activity-task-detail-validation-row"),
    ).toBeNull();
  });

  it("renders a parent reference row whose title wraps freely (no nowrap overflow)", () => {
    const parent: ParentTaskRef = {
      id: "parent-1",
      ref: "#12018",
      title:
        "Clean repo logging system before enforcing logging-format rules across the whole project",
    };
    const { container } = render(
      <TasksTabDetailPanel task={makeTask()} parentTask={parent} />,
    );
    const parentTitle = container.querySelector(
      ".activity-task-detail-kv-row .activity-task-detail-parent-title",
    );
    expect(parentTitle).not.toBeNull();
    expect(parentTitle?.textContent).toContain("Clean repo logging system");
    // The title sits inside a kv-row (which applies white-space: normal),
    // not in the old auto-fit meta grid that forced nowrap overflow.
    const enclosingRow = parentTitle?.closest(".activity-task-detail-kv-row");
    expect(enclosingRow).not.toBeNull();
  });

  it("renders metadata as horizontal kv-rows, not as stacked uppercase headers", () => {
    const { container, getByText } = render(
      <TasksTabDetailPanel task={makeTask()} />,
    );
    expect(getByText("Claimed by")).toBeTruthy();
    expect(getByText("Path")).toBeTruthy();
    // Each metadata row uses the new horizontal kv-row, not the old meta-row
    const rows = container.querySelectorAll(".activity-task-detail-kv-row");
    expect(rows.length).toBeGreaterThanOrEqual(4);
  });
});
