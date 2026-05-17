import { describe, expect, it, vi } from "vitest";
import { fireEvent, render } from "@testing-library/react";

import {
  TasksTabDetailPanel,
  type TaskInlineEditApi,
} from "../TasksTabDetailPanel";
import type { DependencyTree, GobbyTaskDetail } from "../../../hooks/useTasks";
import type { StageStateView } from "../../../lib/stageActions";

function makeStage(overrides: Partial<StageStateView> = {}): StageStateView {
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
    id: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    ref: "#101",
    title: "Sample task",
    status: "in_progress",
    state: null,
    compat: null,
    priority: 2,
    task_type: "epic",
    parent_task_id: null,
    created_at: "2026-05-14T17:00:00Z",
    updated_at: "2026-05-14T17:55:00Z",
    seq_num: 101,
    path_cache: "src/secret/path/to/file.ts",
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

function makeEdit(
  overrides: Partial<TaskInlineEditApi> = {},
): TaskInlineEditApi {
  return {
    commitField: vi.fn(),
    isFieldPending: () => false,
    errorFor: () => null,
    clearError: vi.fn(),
    ...overrides,
  };
}

describe("TasksTabDetailPanel — D5 IA (#14772)", () => {
  it("header is the single source of state truth: ref + title + chips", () => {
    const { container, getByText } = render(
      <TasksTabDetailPanel task={makeTask()} />,
    );
    const header = container.querySelector(".activity-task-detail-header");
    expect(header).not.toBeNull();
    expect(header?.textContent).toContain("#101");
    expect(getByText("Sample task")).toBeTruthy();
    // Type + priority chips render in the header.
    expect(getByText("epic")).toBeTruthy();
    expect(getByText("Medium")).toBeTruthy();
  });

  it("renders an editable title input only when an edit API is injected", () => {
    const edit = makeEdit();
    const { getByLabelText, rerender, queryByLabelText } = render(
      <TasksTabDetailPanel task={makeTask()} edit={edit} />,
    );
    const input = getByLabelText("Task title") as HTMLInputElement;
    expect(input.value).toBe("Sample task");
    fireEvent.change(input, { target: { value: "Renamed" } });
    fireEvent.blur(input);
    expect(edit.commitField).toHaveBeenCalledWith({
      task: expect.objectContaining({ id: makeTask().id }),
      field: "title",
      value: "Renamed",
    });

    rerender(<TasksTabDetailPanel task={makeTask()} />);
    expect(queryByLabelText("Task title")).toBeNull();
  });

  it("shows the owner exactly once, as the friendly ref, never the UUID", () => {
    const uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
    const task = makeTask({
      agent_name: null,
      claimed_by_session_id: uuid,
      owner_session_ref: {
        session_id: uuid,
        ref: "#5122",
        source: "claude",
      },
    });
    const { container } = render(<TasksTabDetailPanel task={task} />);
    const refs = container.querySelectorAll(
      ".activity-task-detail-statusline__owner-ref",
    );
    expect(refs).toHaveLength(1);
    expect(refs[0].textContent).toBe("#5122");
    expect(container.textContent).not.toContain(uuid);
    // The old duplicate metadata vocabulary is gone.
    expect(container.textContent).not.toContain("Driven by");
    expect(container.textContent).not.toContain("Claimed by");
  });

  it("renders 'Unassigned' when there is no owner", () => {
    const { container } = render(
      <TasksTabDetailPanel task={makeTask({ agent_name: null })} />,
    );
    expect(
      container.querySelector(
        ".activity-task-detail-statusline__owner--unassigned",
      )?.textContent,
    ).toContain("Unassigned");
  });

  it("never renders the dropped 'Path' field or its value", () => {
    const { queryByText, container } = render(
      <TasksTabDetailPanel task={makeTask()} />,
    );
    expect(queryByText("Path")).toBeNull();
    expect(container.textContent).not.toContain("src/secret/path/to/file.ts");
  });

  it("renders the escalation block only when the task is escalated", () => {
    const { container: a } = render(
      <TasksTabDetailPanel task={makeTask()} />,
    );
    expect(
      a.querySelector(".activity-task-detail-section--escalated"),
    ).toBeNull();

    const { container: b } = render(
      <TasksTabDetailPanel
        task={makeTask({
          escalated_at: "2026-05-14T17:50:00Z",
          escalation_reason: "needs human",
        })}
      />,
    );
    const block = b.querySelector(".activity-task-detail-section--escalated");
    expect(block).not.toBeNull();
    expect(block?.textContent).toContain("needs human");
  });

  it("renders Trace as a collapsed-by-default details element (no Path)", () => {
    const task = makeTask({
      commits: ["abcdef1234567890", "0987654321fedcba"],
    });
    const { container, getByText } = render(
      <TasksTabDetailPanel task={task} />,
    );
    const trace = container.querySelector(
      "details.activity-task-detail-trace",
    ) as HTMLDetailsElement;
    expect(trace).not.toBeNull();
    expect(trace.hasAttribute("open")).toBe(false);
    expect(getByText("Trace")).toBeTruthy();
    expect(getByText("abcdef1")).toBeTruthy();
  });

  it("links the PR inside Trace when repo metadata is present", () => {
    const { getByText } = render(
      <TasksTabDetailPanel
        task={makeTask({ github_pr_number: 42, github_repo: "example/repo" })}
      />,
    );
    const link = getByText("example/repo#42");
    expect(link.tagName).toBe("A");
    expect(link).toHaveAttribute(
      "href",
      "https://github.com/example/repo/pull/42",
    );
  });

  it("renders the editable core fields when an edit API is injected", () => {
    const { getByLabelText } = render(
      <TasksTabDetailPanel task={makeTask()} edit={makeEdit()} />,
    );
    expect(getByLabelText("Category")).toBeTruthy();
    expect(getByLabelText("Priority")).toBeTruthy();
    expect(getByLabelText("Labels")).toBeTruthy();
    expect(getByLabelText("Description")).toBeTruthy();
    expect(getByLabelText("Validation criteria")).toBeTruthy();
  });

  it("surfaces a single inline edit error with a dismiss control", () => {
    const edit = makeEdit({ errorFor: () => "Couldn't save title: 500" });
    const { getByRole, getByLabelText } = render(
      <TasksTabDetailPanel task={makeTask()} edit={edit} />,
    );
    expect(getByRole("alert").textContent).toContain(
      "Couldn't save title: 500",
    );
    fireEvent.click(getByLabelText("Dismiss error"));
    expect(edit.clearError).toHaveBeenCalledWith(makeTask().id);
  });

  it("renders dependencies as the actual tasks, clickable, not bare counts", () => {
    const onSelectTask = vi.fn();
    const dependencies: DependencyTree = {
      id: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
      blockers: [
        { id: "dep-1", ref: "#900", title: "Upstream schema migration" },
      ],
      blocking: [{ id: "dep-2", ref: "#950", title: "Downstream consumer" }],
    };
    const { getByText } = render(
      <TasksTabDetailPanel
        task={makeTask()}
        dependencies={dependencies}
        onSelectTask={onSelectTask}
      />,
    );
    expect(getByText("Blocked by (1)")).toBeTruthy();
    expect(getByText("Upstream schema migration")).toBeTruthy();
    const link = getByText("#900");
    fireEvent.click(link);
    expect(onSelectTask).toHaveBeenCalledWith("dep-1");
    expect(getByText("Downstream consumer")).toBeTruthy();
  });

  it("renders the Validation row only when validation_status is set", () => {
    const { container: a } = render(
      <TasksTabDetailPanel task={makeTask()} />,
    );
    expect(
      a.querySelector(".activity-task-detail-validation-row"),
    ).toBeNull();

    const { container: b, getByText } = render(
      <TasksTabDetailPanel
        task={makeTask({ validation_status: "pending" })}
      />,
    );
    expect(
      b.querySelector(".activity-task-detail-validation-row"),
    ).not.toBeNull();
    expect(getByText("Validation")).toBeTruthy();
  });
});
