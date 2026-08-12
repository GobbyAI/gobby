import type { ReactElement, ReactNode } from "react";
import {
  fireEvent,
  render as baseRender,
  screen,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi, beforeEach } from "vitest";

import {
  ActivityActionButtons,
  ActivityActionsProvider,
} from "../ActivityActionsContext";
import { TracesTab } from "../TracesTab";
import type { TraceRecord, SpanRecord } from "../../../hooks/useTraces";

// The tab's status selector renders in the shared panel header in the real
// layout; mount it alongside the tab so the control is reachable in tests.
function HeaderHarness({ children }: { children: ReactNode }) {
  return (
    <ActivityActionsProvider>
      <ActivityActionButtons />
      {children}
    </ActivityActionsProvider>
  );
}

const render = (ui: ReactElement) => baseRender(ui, { wrapper: HeaderHarness });

const tracesMock = vi.hoisted(() => ({
  traces: [] as TraceRecord[],
  isLoading: false,
  error: null as string | null,
  filters: {},
  setFilters: vi.fn(),
  fetchTraces: vi.fn(),
  selectedTraceId: null as string | null,
  setSelectedTraceId: vi.fn(),
}));

const detailMock = vi.hoisted(() => ({
  spans: [] as SpanRecord[],
  isLoading: false,
  error: null as string | null,
  fetchDetail: vi.fn(),
}));

vi.mock("../../../hooks/useTraces", () => ({
  useTraces: () => tracesMock,
  useTraceDetail: () => detailMock,
}));

vi.mock("../../shared/ResizeHandle", () => ({
  ResizeHandle: () => null,
}));

function makeTrace(overrides: Partial<TraceRecord> = {}): TraceRecord {
  return {
    id: "r-1",
    project_id: "p",
    trace_id: "trace-1",
    root_span_name: "GET /api",
    status: "OK",
    start_time_ns: 0,
    end_time_ns: 0,
    duration_ms: 12.34,
    timestamp: "2026-05-01T12:00:00Z",
    ...overrides,
  };
}

function makeSpan(overrides: Partial<SpanRecord> = {}): SpanRecord {
  return {
    id: "s-1",
    trace_id: "trace-1",
    span_id: "span-1",
    parent_id: null,
    name: "span-name",
    kind: "internal",
    status: "OK",
    start_time_ns: 0,
    end_time_ns: 1_000_000,
    attributes_json: null,
    events_json: null,
    ...overrides,
  };
}

beforeEach(() => {
  tracesMock.traces = [];
  tracesMock.isLoading = false;
  tracesMock.error = null;
  tracesMock.selectedTraceId = null;
  tracesMock.setSelectedTraceId = vi.fn();
  detailMock.spans = [];
  detailMock.isLoading = false;
  detailMock.error = null;
});

describe("TracesTab", () => {
  it("renders an empty state when no traces are loaded", () => {
    render(<TracesTab projectId="p" />);
    expect(
      screen.getByText(/tool-call traces appear here as agents work/i),
    ).toBeInTheDocument();
  });

  it("renders a fetch error without hiding loaded traces", () => {
    tracesMock.traces = [makeTrace({ root_span_name: "loaded-trace" })];
    tracesMock.error = "Failed to fetch traces (500)";

    render(<TracesTab projectId="p" />);

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Failed to fetch traces (500)",
    );
    expect(screen.getByText("loaded-trace")).toBeInTheDocument();
  });

  it("sorts traces newest-first and selects a trace from the keyboard", async () => {
    tracesMock.traces = [
      makeTrace({
        trace_id: "t-old",
        root_span_name: "old-span",
        timestamp: "2026-04-01T00:00:00Z",
      }),
      makeTrace({
        trace_id: "t-new",
        root_span_name: "new-span",
        timestamp: "2026-05-01T00:00:00Z",
      }),
    ];
    render(<TracesTab projectId="p" />);
    const buttons = screen.getAllByTestId("trace-row-button");
    expect(buttons[0]).toHaveTextContent("new-span");

    buttons[1].focus();
    await userEvent.keyboard("{Enter}");
    expect(tracesMock.setSelectedTraceId).toHaveBeenCalledWith("t-old");
  });

  it("shows a Load more button when more traces are available than the page size", () => {
    tracesMock.traces = Array.from({ length: 25 }, (_, i) =>
      makeTrace({
        trace_id: `t-${i}`,
        root_span_name: `span-${i}`,
        timestamp: new Date(2026, 0, 25 - i).toISOString(),
      }),
    );
    render(<TracesTab projectId="p" />);
    expect(screen.getByText("Load more")).toBeInTheDocument();
    expect(screen.getByText("span-0")).toBeInTheDocument();
    expect(screen.queryByText("span-20")).toBeNull();

    fireEvent.click(screen.getByText("Load more"));
    expect(screen.getByText("span-20")).toBeInTheDocument();
    expect(screen.queryByText("Load more")).toBeNull();
  });

  it("resets pagination when the status filter changes", () => {
    tracesMock.traces = Array.from({ length: 25 }, (_, i) =>
      makeTrace({
        trace_id: `ok-${i}`,
        root_span_name: `ok-span-${i}`,
        status: "OK",
        timestamp: new Date(2026, 0, 25 - i).toISOString(),
      }),
    );
    render(<TracesTab projectId="p" />);

    fireEvent.click(screen.getByText("Load more"));
    expect(screen.getByText("ok-span-20")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("radio", { name: "OK" }));

    expect(screen.queryByText("ok-span-20")).toBeNull();
    expect(screen.getByText("Load more")).toBeInTheDocument();
  });

  it("renders the spans list inside the detail pane when a trace is selected", () => {
    const trace = makeTrace({
      trace_id: "sel",
      root_span_name: "selected-trace",
    });
    tracesMock.traces = [trace];
    tracesMock.selectedTraceId = "sel";
    detailMock.spans = [makeSpan({ id: "s-a", name: "inner-span" })];
    render(<TracesTab projectId="p" />);
    expect(screen.getByText("inner-span")).toBeInTheDocument();
  });

  it("renders a detail fetch error in the selected trace pane", () => {
    tracesMock.traces = [makeTrace({ trace_id: "sel" })];
    tracesMock.selectedTraceId = "sel";
    detailMock.error = "Failed to fetch trace detail (500)";

    render(<TracesTab projectId="p" />);

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Failed to fetch trace detail (500)",
    );
  });
});
