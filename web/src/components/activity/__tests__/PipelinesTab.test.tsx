import type { ReactElement, ReactNode } from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  act,
  fireEvent,
  render as baseRender,
  screen,
  waitFor,
} from "@testing-library/react";
import {
  ActivityActionButtons,
  ActivityActionsProvider,
} from "../ActivityActionsContext";
import { PipelinesTab } from "../PipelinesTab";
import {
  createMockFetch,
  type MockFetchInstance,
} from "../../../test/mocks/fetch";

// The tab's segment selector and Filter trigger render in the shared panel
// header in the real layout; mount it alongside the tab so those controls are
// reachable in tests.
function HeaderHarness({ children }: { children: ReactNode }) {
  return (
    <ActivityActionsProvider>
      <ActivityActionButtons />
      {children}
    </ActivityActionsProvider>
  );
}

const render = (ui: ReactElement) => baseRender(ui, { wrapper: HeaderHarness });

vi.mock("../../shared/ResizeHandle", () => ({
  ResizeHandle: () => <div data-testid="resize-handle" />,
}));

vi.mock("../../shared/executions/execution-utils", () => ({
  PipelineStatusDot: ({ status }: { status: string }) => <span>{status}</span>,
  StepDisplay: () => null,
}));

vi.mock("../../shared/executions/executionFormatters", () => ({
  formatDateTime: (value: string) => value,
  formatDuration: () => "1m",
}));

let mockFetch: MockFetchInstance;

describe("PipelinesTab", () => {
  beforeEach(() => {
    // Fake timers prevent the component's 3s polling interval from firing
    // unpredictably while the test runs. We never advance time in this test
    // — we just want polls to be deterministic, not to fire spontaneously.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mockFetch = createMockFetch();
    mockFetch.mockJsonResponse(/\/api\/pipelines\/executions\?/, {
      executions: [
        {
          id: "exec-1",
          pipeline_name: "Nightly sync",
          status: "running",
          created_at: "2026-04-09T00:00:00Z",
        },
      ],
    });
    mockFetch.mockJsonResponse("/api/pipelines/exec-1", {
      execution: {
        id: "exec-1",
        pipeline_name: "Nightly sync",
        status: "running",
        created_at: "2026-04-09T00:00:00Z",
        steps: [],
      },
    });
  });

  afterEach(() => {
    vi.clearAllTimers();
    vi.useRealTimers();
    mockFetch.restore();
    vi.restoreAllMocks();
  });

  function openFilterDropdown(): void {
    fireEvent.click(screen.getByRole("button", { name: "Filter pipelines" }));
  }

  it("defaults the activity filter to All so transient runs remain visible", async () => {
    render(<PipelinesTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("Nightly sync")).toBeInTheDocument();
    });
    openFilterDropdown();
    expect(screen.getByRole("option", { name: "All" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await screen.findByTestId("resize-handle");

    const executionCalls = mockFetch.fn.mock.calls
      .map(([url]) => String(url))
      .filter((url) => url.includes("/api/pipelines/executions?"));

    expect(executionCalls.length).toBeGreaterThan(0);
    expect(executionCalls[0]).not.toContain("status=running");
  });

  it("renders dropdown filter options in the expected order", async () => {
    render(<PipelinesTab projectId="proj-1" />);
    await waitFor(() => {
      expect(screen.getByText("Nightly sync")).toBeInTheDocument();
    });
    openFilterDropdown();
    const options = screen.getAllByRole("option");
    expect(options.map((option) => option.textContent)).toEqual([
      "All",
      "Completed",
      "Failed",
      "Running",
    ]);
  });

  it("auto-selects the first execution and keeps the detail panel open", async () => {
    render(<PipelinesTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByTestId("resize-handle")).toBeInTheDocument();
      expect(screen.getByText("No steps available")).toBeInTheDocument();
      expect(screen.queryByText("Close")).toBeNull();
    });
  });

  it("switches filters through the dropdown", async () => {
    render(<PipelinesTab projectId="proj-1" />);
    await waitFor(() => {
      expect(screen.getByText("Nightly sync")).toBeInTheDocument();
    });
    openFilterDropdown();

    const failedOption = screen.getByRole("option", { name: "Failed" });
    fireEvent.click(failedOption);

    openFilterDropdown();
    await waitFor(() => {
      expect(screen.getByRole("option", { name: "Failed" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      expect(screen.getByRole("option", { name: "All" })).toHaveAttribute(
        "aria-selected",
        "false",
      );
    });
  });

  it("loads the next page of executions with a page-size offset", async () => {
    mockFetch.resetRoutes();
    const firstPage = Array.from({ length: 50 }, (_, index) => ({
      id: `exec-${index + 1}`,
      pipeline_name: `Pipeline ${index + 1}`,
      status: "completed",
      created_at: "2026-04-09T00:00:00Z",
    }));
    mockFetch.mockJsonResponse(/\/api\/pipelines\/executions\?.*offset=50/, {
      executions: [
        {
          id: "exec-51",
          pipeline_name: "Pipeline 51",
          status: "completed",
          created_at: "2026-04-09T01:00:00Z",
        },
      ],
    });
    mockFetch.mockJsonResponse(/\/api\/pipelines\/executions\?/, {
      executions: firstPage,
    });
    mockFetch.mockJsonResponse("/api/pipelines/exec-1", {
      execution: {
        id: "exec-1",
        pipeline_name: "Pipeline 1",
        status: "completed",
        created_at: "2026-04-09T00:00:00Z",
        steps: [],
      },
    });

    render(<PipelinesTab projectId="proj-1" />);

    await waitFor(() => {
      expect(screen.getByText("Pipeline 50")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));

    await waitFor(() => {
      expect(screen.getByText("Pipeline 51")).toBeInTheDocument();
    });

    const executionCalls = mockFetch.fn.mock.calls
      .map(([url]) => String(url))
      .filter((url) => url.includes("/api/pipelines/executions?"));

    expect(executionCalls.some((url) => url.includes("offset=50"))).toBe(true);
  });

  it("preserves loaded pages while polling and loads the next offset", async () => {
    mockFetch.resetRoutes();
    const executions = Array.from({ length: 101 }, (_, index) => ({
      id: `exec-${index + 1}`,
      pipeline_name: `Pipeline ${index + 1}`,
      status: index === 0 ? "running" : "completed",
      created_at: "2026-04-09T00:00:00Z",
    }));

    mockFetch.fn.mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/pipelines/executions") {
        const offset = Number(url.searchParams.get("offset") ?? 0);
        const limit = Number(url.searchParams.get("limit") ?? 50);
        return Response.json({
          executions: executions.slice(offset, offset + limit),
        });
      }
      const id = url.pathname.split("/").pop();
      return Response.json({
        execution: { ...executions.find((item) => item.id === id), steps: [] },
      });
    });

    render(<PipelinesTab projectId="proj-1" />);
    await screen.findByText("Pipeline 50");

    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    await screen.findByText("Pipeline 100");

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    await waitFor(() => {
      expect(screen.getByText("Pipeline 100")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    await screen.findByText("Pipeline 101");

    const executionCalls = mockFetch.fn.mock.calls
      .map(([url]) => String(url))
      .filter((url) => url.includes("/api/pipelines/executions?"));
    expect(
      executionCalls.some(
        (url) => url.includes("limit=100") && !url.includes("offset="),
      ),
    ).toBe(true);
    expect(executionCalls.some((url) => url.includes("offset=100"))).toBe(true);
    // Renders 101 rows across three page loads plus a poll; shared CI runners
    // need well over vitest's 5s default for this one.
  }, 20_000);

  it("discards a stale execution response after the filter changes", async () => {
    mockFetch.resetRoutes();
    let resolveAll: (response: Response) => void = () => undefined;
    const allResponse = new Promise<Response>((resolve) => {
      resolveAll = resolve;
    });
    let allRequestCount = 0;

    mockFetch.fn.mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/pipelines/executions") {
        if (!url.searchParams.has("status")) {
          allRequestCount += 1;
          if (allRequestCount > 1) return allResponse;
          return Response.json({
            executions: [
              {
                id: "running-exec",
                pipeline_name: "Initially running",
                status: "running",
                created_at: "2026-04-09T00:00:00Z",
              },
            ],
          });
        }
        return Response.json({
          executions: [
            {
              id: "failed-exec",
              pipeline_name: "Current failed run",
              status: "failed",
              created_at: "2026-04-09T00:00:00Z",
            },
          ],
        });
      }
      return Response.json({
        execution: {
          id: url.pathname.split("/").pop(),
          pipeline_name: url.pathname.endsWith("failed-exec")
            ? "Current failed run"
            : "Initially running",
          status: url.pathname.endsWith("failed-exec") ? "failed" : "running",
          created_at: "2026-04-09T00:00:00Z",
          steps: [],
        },
      });
    });

    render(<PipelinesTab projectId="proj-1" />);
    await screen.findByText("Initially running");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    await waitFor(() => {
      expect(allRequestCount).toBeGreaterThan(1);
    });

    openFilterDropdown();
    fireEvent.click(screen.getByRole("option", { name: "Failed" }));
    await screen.findAllByText("Current failed run");

    resolveAll(
      Response.json({
        executions: [
          {
            id: "stale-exec",
            pipeline_name: "Stale all-status run",
            status: "running",
            created_at: "2026-04-09T00:00:00Z",
          },
        ],
      }),
    );
    await act(async () => {
      await Promise.resolve();
    });

    expect(screen.queryByText("Stale all-status run")).toBeNull();
    expect(screen.getAllByText("Current failed run").length).toBeGreaterThan(0);
  });

  it("discards stale detail when selection changes rapidly", async () => {
    mockFetch.resetRoutes();
    let resolveFirstDetail: (response: Response) => void = () => undefined;
    const firstDetail = new Promise<Response>((resolve) => {
      resolveFirstDetail = resolve;
    });
    const list = [
      {
        id: "exec-1",
        pipeline_name: "First run",
        status: "completed",
        created_at: "2026-04-09T00:00:00Z",
      },
      {
        id: "exec-2",
        pipeline_name: "Second run",
        status: "completed",
        created_at: "2026-04-09T00:01:00Z",
      },
    ];

    mockFetch.fn.mockImplementation(async (input: RequestInfo | URL) => {
      const url = new URL(String(input), "http://localhost");
      if (url.pathname === "/api/pipelines/executions")
        return Response.json({ executions: list });
      if (url.pathname === "/api/pipelines/exec-1") return firstDetail;
      return Response.json({
        execution: { ...list[1], pipeline_name: "Second detail", steps: [] },
      });
    });

    render(<PipelinesTab projectId="proj-1" />);
    fireEvent.click(await screen.findByRole("button", { name: /Second run/ }));
    await screen.findByText("Second detail");

    resolveFirstDetail(
      Response.json({
        execution: {
          ...list[0],
          pipeline_name: "Stale first detail",
          steps: [],
        },
      }),
    );
    await act(async () => {
      await Promise.resolve();
    });

    expect(screen.queryByText("Stale first detail")).toBeNull();
    expect(screen.getByText("Second detail")).toBeInTheDocument();
  });
});
