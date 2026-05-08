import type { MockFetchInstance } from "../../../test/mocks/fetch";

export type TestStageState =
  | "ready"
  | "in_progress"
  | "needs_review"
  | "review_approved"
  | "done";

export function stagePayload(
  state: TestStageState = "ready",
  name = "development",
) {
  return {
    name,
    display_name: name
      .split("_")
      .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
      .join(" "),
    category: "delivery",
    state,
    review_policy: "required",
    updated_at: "2026-04-12T00:00:00Z",
  };
}

export function taskStatePayload(
  state: TestStageState = "ready",
  overrides: Record<string, unknown> = {},
) {
  return {
    current_stage: stagePayload(state),
    ...overrides,
  };
}

export const taskList = [
  {
    id: "task-review",
    ref: "#401",
    title: "Review approved task",
    state: taskStatePayload("review_approved"),
    current_stage: stagePayload("review_approved"),
    priority: 2,
    task_type: "task",
    parent_task_id: null,
    created_at: "2026-04-12T00:00:00Z",
    updated_at: "2026-04-12T00:00:00Z",
    seq_num: 401,
    path_cache: "401",
    requires_user_review: false,
    assignee: null,
    agent_name: null,
    sequence_order: null,
    start_date: null,
    due_date: null,
    project_id: "proj-1",
  },
  ...Array.from({ length: 10 }, (_, index) => ({
    id: `task-${index + 1}`,
    ref: `#${410 + index}`,
    title: `Open task ${index + 1}`,
    state: taskStatePayload("ready"),
    current_stage: stagePayload("ready"),
    priority: 2,
    task_type: "task",
    parent_task_id: null,
    created_at: `2026-04-${String(11 - index).padStart(2, "0")}T00:00:00Z`,
    updated_at: `2026-04-${String(11 - index).padStart(2, "0")}T00:00:00Z`,
    seq_num: 410 + index,
    path_cache: String(410 + index),
    requires_user_review: false,
    assignee: null,
    agent_name: null,
    sequence_order: null,
    start_date: null,
    due_date: null,
    project_id: "proj-1",
  })),
  {
    id: "task-closed",
    ref: "#499",
    title: "Closed task",
    state: taskStatePayload("done", {
      is_closed: true,
      closed_at: "2026-04-13T00:00:00Z",
    }),
    current_stage: stagePayload("done"),
    closed_at: "2026-04-13T00:00:00Z",
    priority: 2,
    task_type: "task",
    parent_task_id: null,
    created_at: "2026-04-13T00:00:00Z",
    updated_at: "2026-04-13T00:00:00Z",
    seq_num: 499,
    path_cache: "499",
    requires_user_review: false,
    assignee: null,
    agent_name: null,
    sequence_order: null,
    start_date: null,
    due_date: null,
    project_id: "proj-1",
  },
];

export function installResizeObserverMock(): void {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
}

export function setupDefaultFetchRoutes(mockFetch: MockFetchInstance): void {
  mockFetch.mockJsonResponse("/api/stages/registry", {
    stages: [
      {
        name: "development",
        display_label: "Development",
        category: "implementation",
        review_policy: "required",
        position_hint: 10,
      },
      {
        name: "operator_review",
        display_label: "Operator Review",
        category: "verification",
        review_policy: "required",
        position_hint: 20,
      },
    ],
  });
  mockFetch.mockJsonResponse(/\/api\/tasks\?/, { tasks: taskList });
  mockFetch.mockJsonResponse(/\/api\/tasks\/[^/]+$/, {
    task: {
      ...taskList[0],
      description: "Review approved task detail",
      category: null,
      validation_criteria: null,
      closed_at: null,
    },
  });
}
