/**
 * §5.2 resilient monitoring acceptance (5.2.4): monitoring never depends on
 * the WebSocket alone — a 10s polling fallback runs while a run is live or
 * the WebSocket is disconnected (and stays off when idle with a healthy
 * socket); a RUNNING execution with no progress for the bounded stall window
 * surfaces a recovery state with Refresh and Dismiss; restart-orphaned runs
 * (the §1.6 startup sweep marks them FAILED with the daemon-restart marker in
 * outputs_json) surface the recovery state; the composer re-enables whenever
 * no live execution remains.
 */

import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WikiTab } from "../../WikiTab";
import {
  backlinksEnvelope,
  browseGraphEnvelope,
  browseReadGobbyEnvelope,
  healthEnvelope,
  pagesEnvelope,
  sourcesEnvelope,
  statusEnvelope,
} from "./fixtures";

const wsState = vi.hoisted(() => ({ connected: true }));

vi.mock("../../../../hooks/useWebSocketEvent", () => ({
  useWebSocketEvent: () => undefined,
  useWebSocketConnected: () => wsState.connected,
}));

class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

class MockIntersectionObserver {
  constructor(private callback: IntersectionObserverCallback) {}
  observe() {
    this.callback(
      [{ isIntersecting: true } as IntersectionObserverEntry],
      this as unknown as IntersectionObserver,
    );
  }
  unobserve() {}
  disconnect() {}
}

vi.mock("mermaid", () => ({
  default: { initialize: vi.fn(), render: vi.fn(async () => ({ svg: "<svg />" })) },
}));

vi.mock("react-syntax-highlighter", () => ({
  Prism: ({ children }: { children: string }) => <pre>{children}</pre>,
}));

vi.mock("react-syntax-highlighter/dist/esm/styles/prism", () => ({
  oneDark: {},
  oneLight: {},
}));

vi.mock("react-virtuoso", () => ({
  Virtuoso: () => <div data-testid="virtuoso" />,
}));

// ── Fixtures ────────────────────────────────────────────────────

interface ExecutionSeed {
  id: string;
  status: string;
  outputs?: Record<string, unknown> | null;
  stepStatus?: string;
}

function execution(seed: ExecutionSeed) {
  const created = new Date(Date.now() - 65_000).toISOString();
  return {
    id: seed.id,
    pipeline_name: "wiki-research",
    project_id: "p-1",
    status: seed.status,
    created_at: created,
    updated_at: created,
    completed_at: null,
    inputs_json: JSON.stringify({ question: "How do watchers work?" }),
    outputs_json: seed.outputs ? JSON.stringify(seed.outputs) : null,
    steps: [
      {
        id: 1,
        step_id: "create_research_task",
        status: "completed",
        started_at: null,
        completed_at: null,
        output_json: null,
        error: null,
        approval_token: null,
      },
      {
        id: 2,
        step_id: "spawn_researcher",
        status: seed.stepStatus ?? "running",
        started_at: null,
        completed_at: null,
        output_json: null,
        error: null,
        approval_token: null,
      },
    ],
  };
}

// ── Harness ─────────────────────────────────────────────────────

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status < 400,
    status,
    json: async () => body,
  } as Response;
}

function stubRecoveryFetch(executions: () => unknown[]) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://localhost");
    const route = url.pathname;
    if (route.includes("/api/pipelines/executions")) {
      const rows = executions();
      return jsonResponse({ executions: rows, total: rows.length, status_summary: {} });
    }
    if (route.includes("/api/providers/models")) return jsonResponse({ providers: [] });
    if (route.includes("/api/wiki/status")) return jsonResponse(statusEnvelope);
    if (route.includes("/api/wiki/health")) return jsonResponse(healthEnvelope);
    if (route.includes("/api/wiki/sources")) return jsonResponse(sourcesEnvelope);
    if (route.includes("/api/wiki/pages")) return jsonResponse(pagesEnvelope);
    if (route.includes("/api/wiki/graph")) return jsonResponse(browseGraphEnvelope);
    if (route.includes("/api/wiki/backlinks")) return jsonResponse(backlinksEnvelope);
    if (route.includes("/api/wiki/read")) return jsonResponse(browseReadGobbyEnvelope);
    return jsonResponse({ ok: true, payload: {} });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function executionCalls(fetchMock: ReturnType<typeof stubRecoveryFetch>): URL[] {
  return fetchMock.mock.calls
    .map((call) => new URL(String(call[0]), "http://localhost"))
    .filter((url) => url.pathname.includes("/api/pipelines/executions"));
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.stubGlobal("ResizeObserver", MockResizeObserver);
  vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);
  window.localStorage.clear();
  window.sessionStorage.clear();
  window.localStorage.setItem("gobby:wiki-tab:mode", "research");
  wsState.connected = true;
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

// ── Tests ───────────────────────────────────────────────────────

describe("WikiResearchMode polling fallback", () => {
  it("polls executions on the 10s fallback while the WebSocket is down", async () => {
    wsState.connected = false;
    const fetchMock = stubRecoveryFetch(() => []);
    render(<WikiTab />);

    await waitFor(() => expect(executionCalls(fetchMock).length).toBeGreaterThanOrEqual(1));
    const before = executionCalls(fetchMock).length;
    await advance(10_500);
    expect(executionCalls(fetchMock).length).toBeGreaterThan(before);
  });

  it("polls while a run is live even with the WebSocket connected", async () => {
    const fetchMock = stubRecoveryFetch(() => [execution({ id: "exec-1", status: "running" })]);
    render(<WikiTab />);

    await waitFor(() => expect(executionCalls(fetchMock).length).toBeGreaterThanOrEqual(1));
    const before = executionCalls(fetchMock).length;
    await advance(10_500);
    expect(executionCalls(fetchMock).length).toBeGreaterThan(before);
  });

  it("does not poll when idle with a healthy WebSocket", async () => {
    const fetchMock = stubRecoveryFetch(() => []);
    render(<WikiTab />);

    await waitFor(() => expect(executionCalls(fetchMock).length).toBe(1));
    await advance(30_500);
    expect(executionCalls(fetchMock).length).toBe(1);
  });
});

describe("WikiResearchMode recovery states", () => {
  it("surfaces a stalled run with Refresh and Dismiss", async () => {
    const user = userEvent.setup({ advanceTimers: (ms) => vi.advanceTimersByTime(ms) });
    // The same running execution on every poll — no progress signature change.
    const frozen = execution({ id: "exec-1", status: "running" });
    const fetchMock = stubRecoveryFetch(() => [frozen]);
    render(<WikiTab />);

    await screen.findByRole("region", { name: "Live research run" });
    await advance(70_000);

    const recovery = await screen.findByRole("status", { name: "Research run recovery" });
    expect(recovery.textContent).toMatch(/no progress/i);

    const before = executionCalls(fetchMock).length;
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(executionCalls(fetchMock).length).toBeGreaterThan(before));

    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(
      screen.queryByRole("status", { name: "Research run recovery" }),
    ).not.toBeInTheDocument();
  });

  it("marks restart-orphaned runs and re-enables the composer", async () => {
    stubRecoveryFetch(() => [
      execution({
        id: "exec-1",
        status: "failed",
        outputs: { error: "Daemon restarted while execution was in progress" },
        stepStatus: "failed",
      }),
    ]);
    render(<WikiTab />);

    const recovery = await screen.findByRole("status", { name: "Research run recovery" });
    expect(recovery.textContent).toMatch(/daemon restart/i);

    // No live execution remains → the composer re-enables.
    const composer = screen.getByRole("textbox", { name: "Research question" });
    await waitFor(() => expect(composer).toBeEnabled());
  });

  it("re-enables the composer when the live run completes", async () => {
    let rows: unknown[] = [execution({ id: "exec-1", status: "running" })];
    stubRecoveryFetch(() => rows);
    render(<WikiTab />);

    expect(await screen.findByText("A research run is in progress")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Research question" })).toBeDisabled();

    rows = [
      {
        ...execution({ id: "exec-1", status: "completed", stepStatus: "completed" }),
        completed_at: new Date().toISOString(),
      },
    ];
    await advance(10_500);

    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: "Research question" })).toBeEnabled(),
    );
    expect(screen.queryByText("A research run is in progress")).not.toBeInTheDocument();
  });
});
