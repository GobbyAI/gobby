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

    const initialTaskListFetches = mockFetch.fn.mock.calls.filter(([url]) =>
      String(url).includes("/api/tasks?"),
    ).length;

    const reviewTaskRow = screen
      .getAllByText("Review approved task")[0]
      .closest('[role="treeitem"]');
    fireEvent.click(
      within(reviewTaskRow as HTMLElement).getByRole("button", {
        name: "Task actions",
      }),
    );

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
      String(url).includes("/api/tasks?"),
    ).length;
    expect(finalTaskListFetches).toBe(initialTaskListFetches);
  });

  it("shows an inline error when assigning a task to the main chat fails", async () => {
    mockFetch.mockErrorResponse("/api/tasks/task-review/claim", 500, "Server Error");

    render(<TasksTab projectId="proj-1" chatSessionId="main-chat-1" />);

    await waitFor(() => {
      expect(screen.getByText("Review approved task")).toBeTruthy();
    });

    const reviewTaskRow = screen
      .getAllByText("Review approved task")[0]
      .closest('[role="treeitem"]');
    fireEvent.click(
      within(reviewTaskRow as HTMLElement).getByRole("button", {
        name: "Task actions",
      }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Assign to Main Chat" }),
    );

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe(
      "Failed to assign task to main chat: Failed to claim task (500)",
    );
  });
});
