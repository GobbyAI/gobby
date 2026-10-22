import type { MockFetchInstance } from "../../../test/mocks/fetch";

export type TestTaskState = "ready" | "in_progress" | "closed";

export function taskStatePayload(
  state: TestTaskState = "ready",
  overrides: Record<string, unknown> = {},
) {
  return {
    owner_session_id: state === "in_progress" ? "fixture-session" : null,
    is_claimed: state === "in_progress",
    is_closed: state === "closed",
    is_escalated: false,
    is_blocked: false,
    ...overrides,
  };
}

export const taskList = [
  {
    id: "task-review",
    ref: "#401",
    title: "Ready task",
    state: taskStatePayload("ready"),
    priority: 2,
    task_type: "task",
    parent_task_id: null,
    created_at: "2026-04-12T00:00:00Z",
    updated_at: "2026-04-12T00:00:00Z",
    seq_num: 401,
    path_cache: "401",
    requires_user_review: false,
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
    priority: 2,
    task_type: "task",
    parent_task_id: null,
    created_at: `2026-04-${String(11 - index).padStart(2, "0")}T00:00:00Z`,
    updated_at: `2026-04-${String(11 - index).padStart(2, "0")}T00:00:00Z`,
    seq_num: 410 + index,
    path_cache: String(410 + index),
    requires_user_review: false,
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
    state: taskStatePayload("closed", {
      is_closed: true,
      closed_at: "2026-04-13T00:00:00Z",
    }),
    closed_at: "2026-04-13T00:00:00Z",
    priority: 2,
    task_type: "task",
    parent_task_id: null,
    created_at: "2026-04-13T00:00:00Z",
    updated_at: "2026-04-13T00:00:00Z",
    seq_num: 499,
    path_cache: "499",
    requires_user_review: false,
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
  mockFetch.mockJsonResponse(/\/api\/tasks\?/, { tasks: taskList });
  mockFetch.mockJsonResponse(/\/api\/tasks\/[^/]+$/, {
    task: {
      ...taskList[0],
      description: "Ready task detail",
      category: null,
      validation_criteria: null,
      closed_at: null,
    },
  });
}
