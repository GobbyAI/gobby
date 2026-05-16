import {
  describe,
  it,
  expect,
  vi,
  beforeAll,
  beforeEach,
  afterEach,
} from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { TasksTab } from "../TasksTab";
import {
  createMockFetch,
  type MockFetchInstance,
} from "../../../test/mocks/fetch";
import {
  installResizeObserverMock,
  setupDefaultFetchRoutes,
  stagePayload,
  taskStatePayload,
} from "./TasksTab.setup";

vi.mock("../../../hooks/useWebSocketEvent", () => ({
  useWebSocketEvent: () => {},
}));

beforeAll(() => {
  installResizeObserverMock();
});

vi.mock("../../chat/artifacts/ResizeHandle", () => ({
  ResizeHandle: () => <div data-testid="resize-handle" />,
}));

let mockFetch: MockFetchInstance;

function taskListRequestUrls(): string[] {
  return mockFetch.fn.mock.calls
    .map(([url]) => String(url))
    .filter((url) => url.includes("/api/tasks?") && !url.includes("parent_task_id="));
}

describe("TasksTab", () => {
  beforeEach(() => {
    mockFetch = createMockFetch();
    setupDefaultFetchRoutes(mockFetch);
  });

  afterEach(() => {
    mockFetch.restore();
    vi.restoreAllMocks();
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
    const taskRequest = taskListRequestUrls()[0];
    expect(taskRequest).toContain("include_stages=1");
    expect(taskRequest).toContain("closed=false");
    expect(taskRequest).not.toMatch(/[?&]stage=/);

    const tasksPane = screen.getByTestId("task-tree");
    expect(tasksPane).toHaveClass("activity-tasks-pane", "overflow-y-auto");
    expect(tasksPane.firstElementChild).toHaveAttribute("role", "treeitem");
    expect(screen.getAllByRole("treeitem")).toHaveLength(11);
  });

  it("checks all stage filters by default and narrows by deselection", async () => {
    render(<TasksTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("Review approved task")).toBeTruthy();
    });

    fireEvent.click(screen.getByTitle("Filter by task state"));

    await waitFor(() => {
      expect(screen.getByLabelText("Development")).toBeChecked();
      expect(screen.getByLabelText("Operator Review")).toBeChecked();
    });

    mockFetch.fn.mockClear();
    fireEvent.click(screen.getByLabelText("Development"));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => {
      const taskRequest = taskListRequestUrls().find((url) =>
        url.includes("stage=operator_review"),
      );
      expect(taskRequest).toBeTruthy();
      expect(taskRequest).not.toContain("stage=development");
    });
  });

  it("separates review-rejected tasks from ordinary ready tasks", async () => {
    mockFetch.resetRoutes();
    const rejectedStage = {
      ...stagePayload("ready"),
      review_round_count: 1,
    };
    const plainReadyStage = stagePayload("ready");
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
    mockFetch.mockJsonResponse(/\/api\/tasks\?/, {
      tasks: [
        {
          id: "task-review-rejected",
          ref: "#601",
          title: "Review rejected task",
          state: taskStatePayload("ready", { current_stage: rejectedStage }),
          current_stage: rejectedStage,
          stages: [rejectedStage],
          priority: 2,
          task_type: "task",
          parent_task_id: null,
          created_at: "2026-04-05T00:00:00Z",
          updated_at: "2026-04-05T00:00:00Z",
          seq_num: 601,
          path_cache: "601",
          requires_user_review: false,
          assignee: null,
          agent_name: null,
          sequence_order: null,
          start_date: null,
          due_date: null,
          project_id: "proj-1",
        },
        {
          id: "task-ready",
          ref: "#602",
          title: "Plain ready task",
          state: taskStatePayload("ready", { current_stage: plainReadyStage }),
          current_stage: plainReadyStage,
          stages: [plainReadyStage],
          priority: 2,
          task_type: "task",
          parent_task_id: null,
          created_at: "2026-04-06T00:00:00Z",
          updated_at: "2026-04-06T00:00:00Z",
          seq_num: 602,
          path_cache: "602",
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
        id: "task-review-rejected",
        ref: "#601",
        title: "Review rejected task",
        state: taskStatePayload("ready", { current_stage: rejectedStage }),
        current_stage: rejectedStage,
        stages: [rejectedStage],
        priority: 2,
        task_type: "task",
        parent_task_id: null,
        created_at: "2026-04-05T00:00:00Z",
        updated_at: "2026-04-05T00:00:00Z",
        seq_num: 601,
        path_cache: "601",
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
      expect(screen.getByText("Review rejected task")).toBeTruthy();
      expect(screen.getByText("Plain ready task")).toBeTruthy();
    });

    fireEvent.click(screen.getByTitle("Filter by task state"));
    expect(screen.getByLabelText("Review Rejected")).toBeChecked();

    fireEvent.click(screen.getByLabelText("Ready"));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => {
      expect(screen.getByText("Review rejected task")).toBeTruthy();
      expect(screen.queryByText("Plain ready task")).toBeNull();
    });

    fireEvent.click(screen.getByTitle("Filter by task state"));
    fireEvent.click(screen.getByLabelText("Review Rejected"));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => {
      expect(screen.queryByText("Review rejected task")).toBeNull();
    });
  });

  it("limits closed tasks to the 20 most recently closed entries", async () => {
    mockFetch.resetRoutes();
    mockFetch.mockJsonResponse(/\/api\/tasks\?/, {
      tasks: Array.from({ length: 25 }, (_, index) => ({
        id: `closed-${index + 1}`,
        ref: `#${700 + index}`,
        title: `Closed task ${index + 1}`,
        state: taskStatePayload("done", {
          is_closed: true,
          closed_at: `2026-03-${String(25 - index).padStart(2, "0")}T00:00:00Z`,
        }),
        current_stage: stagePayload("done"),
        closed_at: `2026-03-${String(25 - index).padStart(2, "0")}T00:00:00Z`,
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
      expect(
        screen.getByText("Tasks exist, but none match the current filters"),
      ).toBeTruthy();
    });

    fireEvent.click(screen.getByTitle("Filter by task state"));
    fireEvent.click(screen.getByLabelText("Closed"));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => {
      const closedRequest = taskListRequestUrls().find((url) =>
        url.includes("closed=true"),
      );
      expect(closedRequest).toContain("limit=20");
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
        state: taskStatePayload("ready"),
        current_stage: stagePayload("ready"),
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
        state: taskStatePayload("ready"),
        current_stage: stagePayload("ready"),
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
        state: taskStatePayload("ready"),
        current_stage: stagePayload("ready"),
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
        state: taskStatePayload("ready"),
        current_stage: stagePayload("ready"),
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
        state: taskStatePayload("ready"),
        current_stage: stagePayload("ready"),
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
        state: taskStatePayload("ready"),
        current_stage: stagePayload("ready"),
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
        state: taskStatePayload("ready"),
        current_stage: stagePayload("ready"),
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
          state: taskStatePayload("ready"),
          current_stage: stagePayload("ready"),
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
          state: taskStatePayload("ready"),
          current_stage: stagePayload("ready"),
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
        state: taskStatePayload("ready"),
        current_stage: stagePayload("ready"),
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

  it("loads missing ancestors and renders children under their root context", async () => {
    mockFetch.resetRoutes();
    const childTask = {
      id: "orphan-child",
      ref: "#902",
      title: "Visible child task",
      state: taskStatePayload("ready"),
      current_stage: stagePayload("ready"),
      priority: 2,
      task_type: "task",
      parent_task_id: "missing-root-epic",
      created_at: "2026-04-02T00:00:00Z",
      updated_at: "2026-04-02T00:00:00Z",
      seq_num: 902,
      path_cache: "901/902",
      requires_user_review: false,
      assignee: null,
      agent_name: null,
      sequence_order: null,
      start_date: null,
      due_date: null,
      project_id: "proj-1",
    };
    const rootEpic = {
      id: "missing-root-epic",
      ref: "#901",
      title: "Fetched root epic",
      state: taskStatePayload("done", {
        is_closed: true,
        closed_at: "2026-04-01T00:00:00Z",
      }),
      current_stage: stagePayload("done"),
      closed_at: "2026-04-01T00:00:00Z",
      priority: 2,
      task_type: "epic",
      parent_task_id: null,
      created_at: "2026-04-01T00:00:00Z",
      updated_at: "2026-04-01T00:00:00Z",
      seq_num: 901,
      path_cache: "901",
      requires_user_review: false,
      assignee: null,
      agent_name: null,
      sequence_order: null,
      start_date: null,
      due_date: null,
      project_id: "proj-1",
    };

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
    mockFetch.mockJsonResponse(/\/api\/tasks\?.*parent_task_id=/, { tasks: [] });
    mockFetch.mockJsonResponse(/\/api\/tasks\?.*closed=false/, {
      tasks: [childTask],
    });
    mockFetch.mockJsonResponse(/\/api\/tasks\/missing-root-epic$/, {
      task: {
        ...rootEpic,
        description: null,
        category: null,
        validation_criteria: null,
      },
    });
    mockFetch.mockJsonResponse(/\/api\/tasks\/orphan-child$/, {
      task: {
        ...childTask,
        description: "Visible child detail",
        category: null,
        validation_criteria: null,
        closed_at: null,
      },
    });

    render(<TasksTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("Fetched root epic")).toBeTruthy();
      expect(screen.getByText("Visible child task")).toBeTruthy();
    });

    const parentRow = screen.getByText("Fetched root epic").closest('[role="treeitem"]');
    const childRow = screen.getByText("Visible child task").closest('[role="treeitem"]');

    expect(parentRow).toHaveAttribute("aria-level", "1");
    expect(parentRow).toHaveAttribute("aria-expanded", "true");
    expect(childRow).toHaveAttribute("aria-level", "2");
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

  it("renders detail metadata in the lower pane without the old inline summary line", async () => {
    mockFetch.resetRoutes();
    const detailStage = {
      stage_name: "development",
      position: 10,
      state: "needs_review",
      review_policy: "required",
      updated_at: "2026-04-11T11:30:00Z",
    };
    const detailTask = {
      id: "task-detail",
      ref: "#510",
      title: "Detail pane task",
      state: {
        ...taskStatePayload("needs_review"),
        owner_session_id: "session-123",
        is_claimed: true,
        current_stage: { name: "development", state: "needs_review" },
      },
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
      current_stage: { name: "development", state: "needs_review" },
      stages: [detailStage],
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
    expect(screen.getAllByText("Agent Delta").length).toBeGreaterThan(0);
    expect(screen.getByText("UI")).toBeTruthy();
    expect(screen.getAllByText("Development: Needs Review").length).toBeGreaterThan(0);
    expect(screen.getByText("Validation criteria")).toBeTruthy();
    expect(screen.queryByText("Ready · Medium · bug")).toBeNull();
  });
});
