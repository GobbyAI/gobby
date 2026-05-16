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

describe("TasksTab — filters", () => {
  beforeEach(() => {
    mockFetch = createMockFetch();
    setupDefaultFetchRoutes(mockFetch);
  });

  afterEach(() => {
    mockFetch.restore();
    vi.restoreAllMocks();
  });

  it("renders stage chips, canonical status labels, and grouped filters", async () => {
    const reviewStage = {
      stage_name: "development",
      position: 10,
      category: "delivery",
      state: "needs_review",
      review_policy: "required",
      updated_at: "2026-04-13T00:00:00Z",
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
        {
          name: "operator_review",
          display_label: "Operator Review",
          category: "verification",
          review_policy: "required",
          position_hint: 20,
        },
      ],
    });
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
          current_stage: { name: "development", state: "needs_review" },
          stages: [reviewStage],
          state: {
            owner_session_id: "session-1",
            current_stage: { name: "development", state: "needs_review" },
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

    expect(screen.getByText("Development")).toBeTruthy();
    expect(
      screen.getByLabelText("Status: Development: Needs Review · Claimed"),
    ).toBeTruthy();

    fireEvent.click(screen.getByTitle("Filter by task state"));

    expect(screen.getByText("Stage")).toBeTruthy();
    expect(screen.getByText("Stage state")).toBeTruthy();
    expect(screen.getByText("Status")).toBeTruthy();
    expect(screen.getAllByText("Development").length).toBeGreaterThan(0);
    expect(await screen.findByText("Operator Review")).toBeTruthy();
    expect(screen.getByRole("checkbox", { name: "Development" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Operator Review" })).toBeChecked();
    expect(screen.getByText("Needs Review")).toBeTruthy();
    expect(screen.getByText("Review Approved")).toBeTruthy();
    expect(screen.getByText("Closed")).toBeTruthy();
  });

  it("deselecting a stage filter and applying fetches only remaining stages", async () => {
    render(<TasksTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("Review approved task")).toBeTruthy();
    });

    mockFetch.fn.mockClear();
    fireEvent.click(screen.getByTitle("Filter by task state"));
    fireEvent.click(await screen.findByRole("checkbox", { name: "Development" }));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => {
      const taskFetch = taskListRequestUrls().find((url) =>
        url.includes("stage=operator_review"),
      );
      expect(taskFetch).toContain("include_stages=1");
      expect(taskFetch).not.toContain("stage=development");
    });
  });

  it("deselecting every stage shows a filtered empty state without stage params", async () => {
    render(<TasksTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("Review approved task")).toBeTruthy();
    });

    mockFetch.fn.mockClear();
    fireEvent.click(screen.getByTitle("Filter by task state"));
    fireEvent.click(await screen.findByRole("checkbox", { name: "Development" }));
    fireEvent.click(await screen.findByRole("checkbox", { name: "Operator Review" }));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => {
      expect(
        screen.getByText("Tasks exist, but none match the current filters"),
      ).toBeTruthy();
    });

    expect(taskListRequestUrls().every((url) => !/[?&]stage=/.test(url))).toBe(true);
  });

  it("hides retired lifecycle stages from the stage filter", async () => {
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
          name: "test_arch",
          display_label: "Test Architecture",
          category: "verification",
          review_policy: "required",
          position_hint: 20,
        },
      ],
    });
    mockFetch.mockJsonResponse(/\/api\/tasks\?/, { tasks: [] });

    render(<TasksTab projectId="proj-1" />);

    fireEvent.click(await screen.findByTitle("Filter by task state"));

    expect(await screen.findByRole("checkbox", { name: "Development" })).toBeTruthy();
    expect(screen.queryByRole("checkbox", { name: "Test Architecture" })).toBeNull();
  });

  it("hides the active-filter badge while statusFilters matches the default set", async () => {
    render(<TasksTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("Review approved task")).toBeTruthy();
    });

    const funnel = screen.getByLabelText("Filter tasks");
    expect(funnel.querySelector(".activity-filter-badge")).toBeNull();
  });

  it("shows the active-filter badge with a symmetric-difference count after Apply", async () => {
    render(<TasksTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("Review approved task")).toBeTruthy();
    });

    const getBadgeText = () =>
      screen
        .getByLabelText("Filter tasks")
        .querySelector(".activity-filter-badge")?.textContent ?? null;
    const openAndApply = async (toggleLabels: string[]) => {
      const funnel = await screen.findByLabelText("Filter tasks");
      fireEvent.click(funnel);
      for (const label of toggleLabels) {
        fireEvent.click(await screen.findByLabelText(label));
      }
      fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    };

    // Toggle a default off (Blocked is in DEFAULT_FILTERS).
    await openAndApply(["Blocked"]);
    expect(getBadgeText()).toBe("1");

    // Toggle a non-default on (Closed is not in DEFAULT_FILTERS) → diff = 2.
    await openAndApply(["Closed"]);
    await waitFor(() => {
      expect(getBadgeText()).toBe("2");
    });

    // Restore Blocked → diff = 1 (only Closed is non-default).
    await openAndApply(["Blocked"]);
    expect(getBadgeText()).toBe("1");

    // Restore defaults completely.
    await openAndApply(["Closed"]);
    await waitFor(() => {
      expect(getBadgeText()).toBeNull();
    });
  });

  it("reset restores all checked stages and clears the filter badge", async () => {
    render(<TasksTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("Review approved task")).toBeTruthy();
    });

    fireEvent.click(screen.getByLabelText("Filter tasks"));
    fireEvent.click(screen.getByLabelText("Development"));
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => {
      expect(
        screen
          .getByLabelText("Filter tasks")
          .querySelector(".activity-filter-badge")?.textContent,
      ).toBe("1");
    });

    mockFetch.fn.mockClear();
    fireEvent.click(screen.getByLabelText("Filter tasks"));
    expect(await screen.findByLabelText("Development")).not.toBeChecked();
    expect(screen.getByLabelText("Operator Review")).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    expect(screen.getByLabelText("Development")).toBeChecked();
    expect(screen.getByLabelText("Operator Review")).toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));

    await waitFor(() => {
      expect(
        screen.getByLabelText("Filter tasks").querySelector(".activity-filter-badge"),
      ).toBeNull();
      const taskFetch = taskListRequestUrls()[0];
      expect(taskFetch).toContain("include_stages=1");
      expect(taskFetch).not.toMatch(/[?&]stage=/);
    });
  });

  it("shows a filtered empty state when tasks exist but none match the default filters", async () => {
    mockFetch.resetRoutes();
    mockFetch.mockJsonResponse(/\/api\/tasks\?/, {
      tasks: [
        {
          id: "task-closed-only",
          ref: "#777",
          title: "Closed only task",
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
      expect(screen.getByText("Tasks")).toBeTruthy();
      expect(
        screen.getByText("Tasks exist, but none match the current filters"),
      ).toBeTruthy();
    });
  });
});
