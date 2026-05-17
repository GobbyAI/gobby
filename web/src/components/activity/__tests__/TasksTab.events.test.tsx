import {
  describe,
  it,
  expect,
  vi,
  beforeAll,
  beforeEach,
  afterEach,
} from "vitest";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { TasksTab } from "../TasksTab";
import {
  createMockFetch,
  type MockFetchInstance,
} from "../../../test/mocks/fetch";
import {
  installResizeObserverMock,
  setupDefaultFetchRoutes,
  stagePayload,
  taskList,
  taskStatePayload,
} from "./TasksTab.setup";

let wsHandler: ((data: Record<string, unknown>) => void) | null = null;
vi.mock("../../../hooks/useWebSocketEvent", () => ({
  useWebSocketEvent: (
    _eventType: string,
    handler: (data: Record<string, unknown>) => void,
  ) => {
    wsHandler = handler;
  },
}));

beforeAll(() => {
  installResizeObserverMock();
});

vi.mock("../../chat/artifacts/ResizeHandle", () => ({
  ResizeHandle: () => <div data-testid="resize-handle" />,
}));

let mockFetch: MockFetchInstance;

function taskRow(title: string): HTMLElement {
  const row = screen
    .getAllByText(title)[0]
    .closest('[role="treeitem"]');
  expect(row).toBeTruthy();
  return row as HTMLElement;
}

async function openTaskMenu(title: string): Promise<void> {
  const row = taskRow(title);
  fireEvent.click(
    within(row).getByRole("button", {
      name: "Task actions",
    }),
  );
  await screen.findByRole("button", { name: "Assign to Main Chat" });
}

async function openReviewTaskMenu(): Promise<void> {
  await openTaskMenu("Review approved task");
}

function findPostCall(path: string) {
  return mockFetch.fn.mock.calls.find(
    ([url, init]) =>
      String(url).includes(path) &&
      (init as RequestInit | undefined)?.method === "POST",
  );
}

function setupTaskRoutes(tasks: Array<Record<string, unknown>>): void {
  mockFetch.resetRoutes();
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
  mockFetch.mockJsonResponse(/\/api\/tasks\?/, { tasks });
  mockFetch.mockJsonResponse(/\/api\/tasks\/[^/]+$/, {
    task: {
      ...tasks[0],
      description: "Task detail",
      category: null,
      validation_criteria: null,
    },
  });
}

describe("TasksTab — events and row actions", () => {
  beforeEach(() => {
    mockFetch = createMockFetch();
    setupDefaultFetchRoutes(mockFetch);
  });

  afterEach(() => {
    mockFetch.restore();
    vi.restoreAllMocks();
    wsHandler = null;
  });

  it("adds a new task when a task_created WebSocket event fires", async () => {
    render(<TasksTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("Open task 1")).toBeTruthy();
    });

    expect(screen.queryByText("WS created task")).toBeNull();

    act(() => {
      wsHandler?.({
        type: "task_event",
        event: "task_created",
        task_id: "task-ws-new",
        task: {
          id: "task-ws-new",
          ref: "#900",
          title: "WS created task",
          state: taskStatePayload("ready"),
          current_stage: stagePayload("ready"),
          priority: 2,
          task_type: "task",
          parent_task_id: null,
          created_at: "2026-04-09T00:00:00Z",
          updated_at: "2026-04-09T00:00:00Z",
          seq_num: 900,
          path_cache: "900",
          project_id: "proj-1",
        },
      });
    });

    await waitFor(() => {
      expect(screen.getByText("WS created task")).toBeTruthy();
    });
  });

  it("removes a task when a task_deleted WebSocket event fires", async () => {
    render(<TasksTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("Open task 1")).toBeTruthy();
    });

    act(() => {
      wsHandler?.({
        type: "task_event",
        event: "task_deleted",
        task_id: "task-1",
        task: { id: "task-1" },
      });
    });

    await waitFor(() => {
      expect(screen.queryByText("Open task 1")).toBeNull();
    });
  });

  it("ignores WebSocket events for other projects", async () => {
    render(<TasksTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("Open task 1")).toBeTruthy();
    });

    act(() => {
      wsHandler?.({
        type: "task_event",
        event: "task_created",
        task_id: "task-other",
        task: {
          id: "task-other",
          ref: "#999",
          title: "Other project task",
          state: taskStatePayload("ready"),
          current_stage: stagePayload("ready"),
          priority: 2,
          task_type: "task",
          parent_task_id: null,
          created_at: "2026-04-09T00:00:00Z",
          updated_at: "2026-04-09T00:00:00Z",
          seq_num: 999,
          path_cache: "999",
          project_id: "proj-other",
        },
      });
    });

    expect(screen.queryByText("Other project task")).toBeNull();
  });

  it("ignores task events with invalid task payloads", async () => {
    render(<TasksTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("Open task 1")).toBeTruthy();
    });

    act(() => {
      wsHandler?.({
        type: "task_event",
        event: "task_created",
        task_id: "task-invalid",
        task: {
          id: 123,
          title: "Invalid task payload",
          project_id: "proj-1",
        },
      });
    });

    expect(screen.queryByText("Invalid task payload")).toBeNull();
  });

  it("assigns a task to the active main chat from the row actions menu", async () => {
    mockFetch.mockJsonResponse("/api/tasks/task-review/claim", {
      task: {
        ...taskList[0],
        assignee: "main-chat-1",
      },
    });

    render(<TasksTab projectId="proj-1" chatSessionId="main-chat-1" />);

    await waitFor(() => {
      expect(screen.getByText("Review approved task")).toBeTruthy();
    });

    const isListFetch = (url: unknown) => {
      const s = String(url);
      return s.includes("/api/tasks?") && !s.includes("parent_task_id=");
    };

    const initialTaskListFetches = mockFetch.fn.mock.calls.filter(([url]) =>
      isListFetch(url),
    ).length;

    await openReviewTaskMenu();

    const assignButton = await screen.findByRole("button", {
      name: "Assign to Main Chat",
    });
    fireEvent.click(assignButton);

    await waitFor(() => {
      const claimCall = mockFetch.fn.mock.calls.find(
        ([url, init]) =>
          String(url).includes("/api/tasks/task-review/claim") &&
          (init as RequestInit | undefined)?.method === "POST",
      );

      expect(claimCall).toBeTruthy();
      expect(claimCall?.[1]).toMatchObject({
        method: "POST",
        body: JSON.stringify({ session_id: "main-chat-1", force: true }),
      });
    });

    const finalTaskListFetches = mockFetch.fn.mock.calls.filter(([url]) =>
      isListFetch(url),
    ).length;
    expect(finalTaskListFetches).toBe(initialTaskListFetches);
  });

  it("shows an inline error when assigning a task to the main chat fails", async () => {
    mockFetch.mockErrorResponse("/api/tasks/task-review/claim", 500, "Server Error");

    render(<TasksTab projectId="proj-1" chatSessionId="main-chat-1" />);

    await waitFor(() => {
      expect(screen.getByText("Review approved task")).toBeTruthy();
    });

    await openReviewTaskMenu();
    fireEvent.click(
      await screen.findByRole("button", { name: "Assign to Main Chat" }),
    );

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(
      "Failed to assign task to main chat: Failed to claim task (500)",
    );
  });

  it("shows resume build for paused task evidence and disables assign without chat", async () => {
    render(<TasksTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("Review approved task")).toBeTruthy();
    });
    await openReviewTaskMenu();

    expect(screen.getByRole("button", { name: "Assign to Main Chat" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Resume Build" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Build" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Build Quick" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Stop Build" })).toBeNull();
    expect(screen.getByRole("button", { name: "Close..." })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Release Claim" })).toBeNull();
  });

  it("sends build payloads from the quick menu when no build evidence exists", async () => {
    const startableTask = {
      ...taskList[1],
      id: "task-build",
      ref: "#501",
      title: "Startable build task",
      state: { current_stage: null },
      current_stage: null,
      stages: [],
      seq_num: 501,
      path_cache: "501",
    };
    setupTaskRoutes([startableTask]);
    mockFetch.mockJsonResponse("/api/build/stop", { success: true });
    mockFetch.mockJsonResponse("/api/build", { success: true });

    render(<TasksTab projectId="proj-1" chatSessionId="main-chat-1" />);

    await waitFor(() => {
      expect(screen.getByText("Startable build task")).toBeTruthy();
    });

    await openTaskMenu("Startable build task");
    expect(screen.getByRole("button", { name: "Build" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Build Quick" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Stop Build" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Resume Build" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Build" }));
    await waitFor(() => {
      expect(findPostCall("/api/build")).toBeTruthy();
    });
    expect(findPostCall("/api/build")?.[1]).toMatchObject({
      body: JSON.stringify({ input_ref: "#501" }),
    });

    await openTaskMenu("Startable build task");
    fireEvent.click(screen.getByRole("button", { name: "Build Quick" }));
    await waitFor(() => {
      const buildCalls = mockFetch.fn.mock.calls.filter(
        ([url, init]) =>
          String(url).includes("/api/build") &&
          !String(url).includes("/api/build/") &&
          (init as RequestInit | undefined)?.method === "POST",
      );
      expect(buildCalls.length).toBeGreaterThanOrEqual(2);
    });
    const buildCalls = mockFetch.fn.mock.calls.filter(
      ([url, init]) =>
        String(url).includes("/api/build") &&
        !String(url).includes("/api/build/") &&
        (init as RequestInit | undefined)?.method === "POST",
    );
    expect(buildCalls[buildCalls.length - 1]?.[1]).toMatchObject({
      body: JSON.stringify({
        input_ref: "#501",
        quick: true,
        stage: [],
      }),
    });
    expect(findPostCall("/api/build/stop")?.[1]).toMatchObject({
      body: JSON.stringify({ input_ref: "#501" }),
    });
  });

  it("resumes paused task-scoped build automation from the quick menu", async () => {
    mockFetch.mockJsonResponse("/api/build/resume", { success: true });

    render(<TasksTab projectId="proj-1" chatSessionId="main-chat-1" />);

    await waitFor(() => {
      expect(screen.getByText("Review approved task")).toBeTruthy();
    });
    await openReviewTaskMenu();
    expect(screen.getByRole("button", { name: "Resume Build" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Resume Build" }));

    await waitFor(() => {
      expect(findPostCall("/api/build/resume")).toBeTruthy();
    });
    expect(findPostCall("/api/build/resume")?.[1]).toMatchObject({
      body: JSON.stringify({ input_ref: "#401" }),
    });
  });

  it("stops task-scoped build automation from the quick menu", async () => {
    const activeBuildTask = {
      ...taskList[0],
      allow_automation: true,
    };
    setupTaskRoutes([activeBuildTask]);
    mockFetch.mockJsonResponse("/api/build/stop", { success: true });

    render(<TasksTab projectId="proj-1" chatSessionId="main-chat-1" />);

    await waitFor(() => {
      expect(screen.getByText("Review approved task")).toBeTruthy();
    });
    await openReviewTaskMenu();
    expect(screen.getByRole("button", { name: "Stop Build" })).toBeEnabled();
    expect(screen.queryByRole("button", { name: "Build" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Build Quick" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Resume Build" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Stop Build" }));

    await waitFor(() => {
      expect(findPostCall("/api/build/stop")).toBeTruthy();
    });
    expect(findPostCall("/api/build/stop")?.[1]).toMatchObject({
      body: JSON.stringify({ input_ref: "#401" }),
    });
  });

  it("releases a claimed task from the quick menu", async () => {
    const claimedTask = {
      ...taskList[0],
      claimed_by_session_id: "main-chat-1",
      assignee: "main-chat-1",
      state: taskStatePayload("review_approved", {
        is_claimed: true,
        owner_session_id: "main-chat-1",
      }),
    };
    mockFetch.resetRoutes();
    mockFetch.mockJsonResponse("/api/stages/registry", {
      stages: [
        {
          name: "development",
          display_label: "Development",
          category: "implementation",
          review_policy: "required",
          position_hint: 10,
        },
      ],
    });
    mockFetch.mockJsonResponse(/\/api\/tasks\?/, { tasks: [claimedTask] });
    mockFetch.mockJsonResponse(/\/api\/tasks\/[^/]+$/, {
      task: {
        ...claimedTask,
        description: "Review approved task detail",
        category: null,
        validation_criteria: null,
        closed_at: null,
      },
    });
    mockFetch.mockJsonResponse("/api/tasks/task-review/release-claim", {
      task: {
        ...claimedTask,
        claimed_by_session_id: null,
        assignee: null,
        state: taskStatePayload("review_approved", { is_claimed: false }),
      },
    });

    render(<TasksTab projectId="proj-1" chatSessionId="main-chat-1" />);

    await waitFor(() => {
      expect(screen.getByText("Review approved task")).toBeTruthy();
    });
    await openReviewTaskMenu();
    fireEvent.click(screen.getByRole("button", { name: "Release Claim" }));

    await waitFor(() => {
      expect(findPostCall("/api/tasks/task-review/release-claim")).toBeTruthy();
    });
    expect(findPostCall("/api/tasks/task-review/release-claim")?.[1]).toMatchObject({
      body: JSON.stringify({}),
    });
  });

  it("requires a close reason before posting the close action", async () => {
    mockFetch.mockJsonResponse("/api/tasks/task-review/close", {
      task: {
        ...taskList[0],
        closed_at: "2026-04-14T00:00:00Z",
        state: taskStatePayload("done", {
          is_closed: true,
          closed_at: "2026-04-14T00:00:00Z",
        }),
      },
    });

    render(<TasksTab projectId="proj-1" chatSessionId="main-chat-1" />);

    await waitFor(() => {
      expect(screen.getByText("Review approved task")).toBeTruthy();
    });
    await openReviewTaskMenu();
    fireEvent.click(screen.getByRole("button", { name: "Close..." }));

    const dialog = await screen.findByRole("dialog", { name: "Close task" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Reason is required.",
    );
    expect(findPostCall("/api/tasks/task-review/close")).toBeUndefined();

    fireEvent.change(within(dialog).getByLabelText("Reason"), {
      target: { value: "Finished implementation" },
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));

    await waitFor(() => {
      expect(findPostCall("/api/tasks/task-review/close")).toBeTruthy();
    });
    expect(findPostCall("/api/tasks/task-review/close")?.[1]).toMatchObject({
      body: JSON.stringify({ reason: "Finished implementation" }),
    });
  });

  it("reopens a closed task from the quick menu", async () => {
    mockFetch.mockJsonResponse("/api/tasks/task-closed/reopen", {
      task: {
        ...taskList.find((task) => task.id === "task-closed"),
        closed_at: null,
        state: taskStatePayload("ready", { is_closed: false }),
        current_stage: stagePayload("ready"),
      },
    });

    render(<TasksTab projectId="proj-1" chatSessionId="main-chat-1" />);

    fireEvent.click(await screen.findByRole("button", { name: "Filter tasks" }));
    const filterDialog = await screen.findByRole("dialog", { name: "Task filters" });
    fireEvent.click(within(filterDialog).getByLabelText("Closed"));
    fireEvent.click(within(filterDialog).getByRole("button", { name: "Apply" }));

    const closedRow = await screen.findByText("Closed task");
    fireEvent.click(
      within(closedRow.closest('[role="treeitem"]') as HTMLElement).getByRole("button", {
        name: "Task actions",
      }),
    );
    expect(screen.queryByRole("button", { name: "Build" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Build Quick" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Stop Build" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Resume Build" })).toBeNull();
    fireEvent.click(await screen.findByRole("button", { name: "Reopen" }));

    await waitFor(() => {
      expect(findPostCall("/api/tasks/task-closed/reopen")).toBeTruthy();
    });
    expect(findPostCall("/api/tasks/task-closed/reopen")?.[1]).toMatchObject({
      body: JSON.stringify({}),
    });
  });
});
