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
  fireEvent,
  render,
  screen,
  waitFor,
  act,
  within,
} from "@testing-library/react";
import { TasksTab } from "../TasksTab";
import {
  createMockFetch,
  type MockFetchInstance,
} from "../../../test/mocks/fetch";

// Capture the handler passed to useWebSocketEvent so tests can simulate events
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
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
});

vi.mock("../../chat/artifacts/ResizeHandle", () => ({
  ResizeHandle: () => <div data-testid="resize-handle" />,
}));

let mockFetch: MockFetchInstance;

const taskList = [
  {
    id: "task-review",
    ref: "#401",
    title: "Review approved task",
    status: "review_approved",
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
    status: "open",
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
    status: "closed",
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

describe("TasksTab", () => {
  beforeEach(() => {
    mockFetch = createMockFetch();
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
  });

  afterEach(() => {
    mockFetch.restore();
    vi.restoreAllMocks();
    // Clear the captured WebSocket handler so it doesn't leak between tests
    wsHandler = null;
  });

  it("includes review-approved tasks by default and shows all active tasks without pagination", async () => {
    render(<TasksTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("Review approved task")).toBeTruthy();
      expect(screen.getByText("Open task 2")).toBeTruthy();
      expect(screen.getByText("Open task 10")).toBeTruthy();
    });

    expect(screen.queryByText("Closed task")).toBeNull();
    expect(screen.queryByText("Load more")).toBeNull();

    const tasksPane = screen.getByTestId("task-tree");
    expect(tasksPane).toHaveClass("activity-tasks-pane", "overflow-y-auto");
    expect(tasksPane.firstElementChild).toHaveAttribute("role", "treeitem");
    expect(screen.getAllByRole("treeitem")).toHaveLength(11);
  });

  it("limits closed tasks to the 20 most recently closed entries", async () => {
    mockFetch.resetRoutes();
    mockFetch.mockJsonResponse(/\/api\/tasks\?/, {
      tasks: Array.from({ length: 25 }, (_, index) => ({
        id: `closed-${index + 1}`,
        ref: `#${700 + index}`,
        title: `Closed task ${index + 1}`,
        status: "closed",
        priority: 2,
        task_type: "task",
        parent_task_id: null,
        created_at: `2026-03-${String(25 - index).padStart(2, "0")}T00:00:00Z`,
        updated_at: `2026-03-${String(25 - index).padStart(2, "0")}T00:00:00Z`,
        seq_num: 700 + index,
        path_cache: String(700 + index),
        requires_user_review: false,
        assignee: null,
        agent_name: null,
        sequence_order: null,
        start_date: null,
        due_date: null,
        project_id: "proj-1",
      })),
    });

    render(<TasksTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("No tasks match filters")).toBeTruthy();
    });

    fireEvent.click(screen.getByTitle("Filter by task state"));
    fireEvent.click(screen.getByLabelText("Closed"));

    await waitFor(() => {
      expect(screen.getByText("Closed task 1")).toBeTruthy();
      expect(screen.getByText("Closed task 20")).toBeTruthy();
      expect(screen.queryByText("Closed task 21")).toBeNull();
      expect(screen.queryByText("Closed task 25")).toBeNull();
      expect(screen.getAllByRole("treeitem")).toHaveLength(20);
    });
  });

  it("orders task roots and siblings by priority then seq_num", async () => {
    mockFetch.resetRoutes();
    const orderedTasks = [
      {
        id: "root-medium",
        ref: "#701",
        title: "Root medium",
        status: "open",
        priority: 2,
        task_type: "task",
        parent_task_id: null,
        created_at: "2026-04-05T00:00:00Z",
        updated_at: "2026-04-05T00:00:00Z",
        seq_num: 701,
        path_cache: "701",
        requires_user_review: false,
        assignee: null,
        agent_name: null,
        sequence_order: null,
        start_date: null,
        due_date: null,
        project_id: "proj-1",
      },
      {
        id: "root-high-late",
        ref: "#702",
        title: "Root high late",
        status: "open",
        priority: 1,
        task_type: "task",
        parent_task_id: null,
        created_at: "2026-04-04T00:00:00Z",
        updated_at: "2026-04-04T00:00:00Z",
        seq_num: 702,
        path_cache: "702",
        requires_user_review: false,
        assignee: null,
        agent_name: null,
        sequence_order: null,
        start_date: null,
        due_date: null,
        project_id: "proj-1",
      },
      {
        id: "root-high-early",
        ref: "#703",
        title: "Root high early",
        status: "open",
        priority: 1,
        task_type: "task",
        parent_task_id: null,
        created_at: "2026-04-01T00:00:00Z",
        updated_at: "2026-04-01T00:00:00Z",
        seq_num: 703,
        path_cache: "703",
        requires_user_review: false,
        assignee: null,
        agent_name: null,
        sequence_order: null,
        start_date: null,
        due_date: null,
        project_id: "proj-1",
      },
      {
        id: "parent-root",
        ref: "#704",
        title: "Parent root",
        status: "open",
        priority: 2,
        task_type: "epic",
        parent_task_id: null,
        created_at: "2026-04-02T00:00:00Z",
        updated_at: "2026-04-02T00:00:00Z",
        seq_num: 704,
        path_cache: "704",
        requires_user_review: false,
        assignee: null,
        agent_name: null,
        sequence_order: null,
        start_date: null,
        due_date: null,
        project_id: "proj-1",
      },
      {
        id: "child-medium-new",
        ref: "#705",
        title: "Child medium new",
        status: "open",
        priority: 2,
        task_type: "task",
        parent_task_id: "parent-root",
        created_at: "2026-04-06T00:00:00Z",
        updated_at: "2026-04-06T00:00:00Z",
        seq_num: 705,
        path_cache: "704/705",
        requires_user_review: false,
        assignee: null,
        agent_name: null,
        sequence_order: null,
        start_date: null,
        due_date: null,
        project_id: "proj-1",
      },
      {
        id: "child-critical",
        ref: "#706",
        title: "Child critical",
        status: "open",
        priority: 0,
        task_type: "bug",
        parent_task_id: "parent-root",
        created_at: "2026-04-07T00:00:00Z",
        updated_at: "2026-04-07T00:00:00Z",
        seq_num: 706,
        path_cache: "704/706",
        requires_user_review: false,
        assignee: null,
        agent_name: null,
        sequence_order: null,
        start_date: null,
        due_date: null,
        project_id: "proj-1",
      },
      {
        id: "child-medium-old",
        ref: "#707",
        title: "Child medium old",
        status: "open",
        priority: 2,
        task_type: "task",
        parent_task_id: "parent-root",
        created_at: "2026-04-03T00:00:00Z",
        updated_at: "2026-04-03T00:00:00Z",
        seq_num: 707,
        path_cache: "704/707",
        requires_user_review: false,
        assignee: null,
        agent_name: null,
        sequence_order: null,
        start_date: null,
        due_date: null,
        project_id: "proj-1",
      },
    ];
    mockFetch.mockJsonResponse(/\/api\/tasks\?/, { tasks: orderedTasks });
    mockFetch.mockJsonResponse(/\/api\/tasks\/[^/]+$/, {
      task: {
        ...orderedTasks[0],
        description: null,
        category: null,
        validation_criteria: null,
        closed_at: null,
      },
    });

    render(<TasksTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("Child medium new")).toBeTruthy();
    });

    const titles = screen.getAllByRole("treeitem").map((node) => {
      const titleNode = node.querySelector(".activity-task-row-title");
      return titleNode?.textContent ?? node.textContent;
    });

    expect(titles).toEqual([
      "Root high late",
      "Root high early",
      "Root medium",
      "Parent root",
      "Child critical",
      "Child medium new",
      "Child medium old",
    ]);
  });

  it("uses accessible subtree toggles and preserves tree depth semantics", async () => {
    mockFetch.resetRoutes();
    mockFetch.mockJsonResponse(/\/api\/tasks\?/, {
      tasks: [
        {
          id: "parent-task",
          ref: "#801",
          title: "Expandable parent",
          status: "open",
          priority: 2,
          task_type: "task",
          parent_task_id: null,
          created_at: "2026-04-01T00:00:00Z",
          updated_at: "2026-04-01T00:00:00Z",
          seq_num: 801,
          path_cache: "801",
          requires_user_review: false,
          assignee: null,
          agent_name: null,
          sequence_order: null,
          start_date: null,
          due_date: null,
          project_id: "proj-1",
        },
        {
          id: "child-task",
          ref: "#802",
          title: "Nested child",
          status: "open",
          priority: 2,
          task_type: "task",
          parent_task_id: "parent-task",
          created_at: "2026-04-02T00:00:00Z",
          updated_at: "2026-04-02T00:00:00Z",
          seq_num: 802,
          path_cache: "801/802",
          requires_user_review: false,
          assignee: null,
          agent_name: null,
          sequence_order: null,
          start_date: null,
          due_date: null,
          project_id: "proj-1",
        },
      ],
    });
    mockFetch.mockJsonResponse(/\/api\/tasks\/[^/]+$/, {
      task: {
        id: "parent-task",
        ref: "#801",
        title: "Expandable parent",
        status: "open",
        priority: 2,
        task_type: "task",
        parent_task_id: null,
        created_at: "2026-04-01T00:00:00Z",
        updated_at: "2026-04-01T00:00:00Z",
        seq_num: 801,
        path_cache: "801",
        requires_user_review: false,
        assignee: null,
        agent_name: null,
        sequence_order: null,
        start_date: null,
        due_date: null,
        project_id: "proj-1",
        description: null,
        category: null,
        validation_criteria: null,
        closed_at: null,
      },
    });

    render(<TasksTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("Nested child")).toBeTruthy();
    });

    const parentRow = screen
      .getAllByText("Expandable parent")[0]
      .closest('[role="treeitem"]');
    const childRow = screen.getByText("Nested child").closest('[role="treeitem"]');
    expect(parentRow).toHaveAttribute("aria-expanded", "true");
    expect(childRow).toHaveAttribute("aria-level", "2");

    fireEvent.click(
      screen.getByRole("button", {
        name: "Collapse subtasks for Expandable parent",
      }),
    );

    await waitFor(() => {
      expect(screen.queryByText("Nested child")).toBeNull();
    });

    expect(
      screen
        .getAllByText("Expandable parent")[0]
        .closest('[role="treeitem"]'),
    ).toHaveAttribute("aria-expanded", "false");
    expect(
      screen.getByRole("button", {
        name: "Expand subtasks for Expandable parent",
      }),
    ).toBeTruthy();
  });

  it("auto-selects the first visible task and keeps the detail pane open", async () => {
    render(<TasksTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("Review approved task")).toBeTruthy();
    });

    fireEvent.click(screen.getAllByText("Review approved task")[0]);

    await waitFor(() => {
      expect(screen.getByText("Review approved task detail")).toBeTruthy();
      expect(screen.queryByText("Task not found")).toBeNull();
      expect(screen.queryByText("Close")).toBeNull();
    });
  });

  it("renders canonical state tasks and groups the filter menu by lifecycle and status", async () => {
    mockFetch.resetRoutes();
    mockFetch.mockJsonResponse(/\/api\/tasks\?/, {
      tasks: [
        {
          id: "task-needs-review",
          ref: "#601",
          title: "Canonical needs review task",
          priority: 2,
          task_type: "task",
          parent_task_id: null,
          created_at: "2026-04-13T00:00:00Z",
          updated_at: "2026-04-13T00:00:00Z",
          seq_num: 601,
          path_cache: "601",
          project_id: "proj-1",
          state: {
            owner_session_id: "session-1",
            lifecycle_stage: "needs_review",
            is_claimed: true,
            is_closed: false,
            is_escalated: false,
            is_blocked: false,
            is_merge_ready: false,
            closed_at: null,
            closed_reason: null,
            closed_in_session_id: null,
            closed_commit_sha: null,
            escalated_at: null,
            escalation_reason: null,
          },
        },
      ],
    });

    render(<TasksTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("Canonical needs review task")).toBeTruthy();
    });

    fireEvent.click(screen.getByTitle("Filter by task state"));

    expect(screen.getByText("Lifecycle")).toBeTruthy();
    expect(screen.getByText("Status")).toBeTruthy();
    expect(screen.getByText("Needs Review")).toBeTruthy();
    expect(screen.getByText("Merge Ready")).toBeTruthy();
    expect(screen.getByText("Closed")).toBeTruthy();
  });

  it("hides the active-filter badge while statusFilters matches the default set", async () => {
    render(<TasksTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("Review approved task")).toBeTruthy();
    });

    const funnel = screen.getByLabelText("Filter tasks");
    expect(funnel.querySelector(".activity-filter-badge")).toBeNull();
  });

  it("shows the active-filter badge with a symmetric-difference count", async () => {
    render(<TasksTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("Review approved task")).toBeTruthy();
    });

    const funnel = screen.getByLabelText("Filter tasks");
    fireEvent.click(funnel);

    // Toggle a default off (Blocked is in DEFAULT_FILTERS).
    fireEvent.click(screen.getByLabelText("Blocked"));
    expect(funnel.querySelector(".activity-filter-badge")?.textContent).toBe("1");

    // Toggle a non-default on (Closed is not in DEFAULT_FILTERS) → diff = 2.
    fireEvent.click(screen.getByLabelText("Closed"));
    expect(funnel.querySelector(".activity-filter-badge")?.textContent).toBe("2");

    // Restore Blocked → diff = 1 (only Closed is non-default).
    fireEvent.click(screen.getByLabelText("Blocked"));
    expect(funnel.querySelector(".activity-filter-badge")?.textContent).toBe("1");

    // Restore defaults completely.
    fireEvent.click(screen.getByLabelText("Closed"));
    expect(funnel.querySelector(".activity-filter-badge")).toBeNull();
  });

  it("renders detail metadata in the lower pane without the old inline summary line", async () => {
    mockFetch.resetRoutes();
    const detailTask = {
      id: "task-detail",
      ref: "#510",
      title: "Detail pane task",
      status: "open",
      priority: 2,
      task_type: "bug",
      parent_task_id: null,
      created_at: "2026-04-10T10:00:00Z",
      updated_at: "2026-04-11T11:30:00Z",
      seq_num: 510,
      path_cache: "510/ui",
      requires_user_review: false,
      assignee: "session-123",
      agent_name: "Agent Delta",
      sequence_order: null,
      start_date: null,
      due_date: null,
      project_id: "proj-1",
    };
    mockFetch.mockJsonResponse(/\/api\/tasks\?/, {
      tasks: [detailTask],
    });
    mockFetch.mockJsonResponse(/\/api\/tasks\/[^/]+$/, {
      task: {
        ...detailTask,
        description: "Detail task description",
        category: "UI",
        validation_criteria: "Verify the lower pane layout",
        closed_at: null,
      },
    });

    render(<TasksTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("Detail task description")).toBeTruthy();
    });

    expect(screen.getByText("Claimed by")).toBeTruthy();
    expect(screen.getByText("State")).toBeTruthy();
    expect(screen.getByText("Created")).toBeTruthy();
    expect(screen.getByText("Updated")).toBeTruthy();
    expect(screen.getByText("Category")).toBeTruthy();
    expect(screen.getByText("Path")).toBeTruthy();
    expect(screen.getByText("Agent Delta")).toBeTruthy();
    expect(screen.getByText("UI")).toBeTruthy();
    expect(screen.getByText("Validation")).toBeTruthy();
    expect(screen.queryByText("Ready · Medium · bug")).toBeNull();
  });

  it("shows a filtered empty state when tasks exist but none match the default filters", async () => {
    mockFetch.resetRoutes();
    mockFetch.mockJsonResponse(/\/api\/tasks\?/, {
      tasks: [
        {
          id: "task-closed-only",
          ref: "#777",
          title: "Closed only task",
          status: "closed",
          priority: 2,
          task_type: "task",
          parent_task_id: null,
          created_at: "2026-04-13T00:00:00Z",
          updated_at: "2026-04-13T00:00:00Z",
          seq_num: 777,
          path_cache: "777",
          requires_user_review: false,
          assignee: null,
          agent_name: null,
          sequence_order: null,
          start_date: null,
          due_date: null,
          project_id: "proj-1",
        },
      ],
    });

    render(<TasksTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("No tasks match filters")).toBeTruthy();
      expect(
        screen.getByText(
          "Tasks exist, but none match the current task-state filters.",
        ),
      ).toBeTruthy();
    });
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
          status: "open",
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
          status: "open",
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
