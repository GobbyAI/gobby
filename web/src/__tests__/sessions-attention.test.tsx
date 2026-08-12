import type { ReactElement, ReactNode } from "react";
import {
  act,
  fireEvent,
  render as baseRender,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  ActivityActionButtons,
  ActivityActionsProvider,
} from "../components/activity/ActivityActionsContext";
import { SessionsTab } from "../components/activity/SessionsTab";

// The tab's toolbar (Filter included) renders in the shared panel header in
// the real layout; mount it alongside the tab so it is reachable in tests.
function HeaderHarness({ children }: { children: ReactNode }) {
  return (
    <ActivityActionsProvider>
      <ActivityActionButtons />
      {children}
    </ActivityActionsProvider>
  );
}

const render = (ui: ReactElement) => baseRender(ui, { wrapper: HeaderHarness });

type AgentEventHandler = (data: Record<string, unknown>) => void;

const websocket = vi.hoisted(() => ({
  handler: null as AgentEventHandler | null,
}));

vi.mock("../hooks/useWebSocketEvent", () => ({
  useWebSocketEvent: (eventType: string, handler: AgentEventHandler) => {
    if (eventType === "agent_event") websocket.handler = handler;
  },
  useWebSocketConnected: () => true,
}));

vi.mock("../components/activity/SessionsTab.entries", () => ({
  resolveSessionStatusMode: () => "live",
  statusesForMode: () => new Set(["active", "paused"]),
  useRunningAgents: () => ({
    agents: [],
    agentsLoading: false,
    fetchError: null,
  }),
  useWatchingSessionEntries: () => [
    {
      id: "session-1",
      type: "session",
      label: "Two-run session",
      provider: "codex",
      status: "active",
      sessionType: "web_chat",
      inputTokens: 0,
      outputTokens: 0,
      totalTokens: 0,
      hasTmux: false,
      sandboxEnabled: false,
      isLocal: false,
    },
  ],
}));

vi.mock("../hooks/useSessionDetail", () => ({
  useSessionDetail: () => ({
    session: null,
    sessionError: null,
    transcriptDownloadUrl: null,
    clearSessionError: vi.fn(),
    messages: [],
    isLoading: false,
    transcriptStatus: null,
    hasMore: false,
    loadMore: vi.fn(),
    hasNewer: false,
    loadNewer: vi.fn(),
    isLoadingOlder: false,
    isLoadingNewer: false,
    setTranscriptAtBottom: vi.fn(),
    firstItemIndex: 0,
    transcriptDegradedReason: null,
  }),
}));

vi.mock("../components/activity/SessionsTabDetail", () => ({
  SessionsTabDetailPane: () => null,
  SessionsTabResizeHandle: () => null,
}));

vi.mock("../components/activity/SessionsTabMenu", () => ({
  SessionsContextMenu: () => null,
  SessionsInteractionModalHost: () => null,
}));

vi.mock("../hooks/useIsMobile", () => ({
  useIsMobile: () => false,
}));

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function attentionEvent(
  entryId: string,
  seq: number,
  state: "blocked" | null,
  reason: string | null,
): Record<string, unknown> {
  const runId = entryId.replace("run:", "");
  return {
    type: "agent_event",
    event: "attention_changed",
    epoch: "epoch-a",
    seq,
    entry_id: entryId,
    run_id: runId,
    session_id: "session-1",
    attention_id: `attention-${runId}`,
    state,
    reason,
    kind: state === "blocked" ? "approval" : null,
    fingerprint: state === "blocked" ? `approval:${runId}` : null,
    payload: {},
    since: state === "blocked" ? "2026-07-22T09:00:00Z" : null,
    seen_at: null,
  };
}

describe("session attention", () => {
  beforeEach(() => {
    localStorage.clear();
    websocket.handler = null;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url = String(input);
        if (url === "/api/attention/roster") {
          return Promise.resolve(
            jsonResponse({
              epoch: "epoch-a",
              seq: 5,
              entries: [
                {
                  entry_id: "run:run-1",
                  run_id: "run-1",
                  session_id: "session-1",
                  attention: {
                    attention_id: "attention-run-1",
                    state: "blocked",
                    reason: "Approval required",
                  },
                },
                {
                  entry_id: "run:run-2",
                  run_id: "run-2",
                  session_id: "session-1",
                  attention: null,
                },
              ],
            }),
          );
        }
        if (url === "/api/providers") {
          return Promise.resolve(jsonResponse({ providers: [] }));
        }
        throw new Error(`Unexpected request: ${url}`);
      }),
    );
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("aggregates blocked runs per session and clears only after the final entry clears", async () => {
    render(<SessionsTab />);

    expect(websocket.handler).not.toBeNull();
    expect(
      await screen.findByLabelText("Blocked attention: 1; Approval required"),
    ).toHaveTextContent("blocked 1");

    act(() => {
      websocket.handler?.(
        attentionEvent("run:run-2", 6, "blocked", "Operator input required"),
      );
    });
    expect(
      await screen.findByLabelText(
        "Blocked attention: 2; Approval required; Operator input required",
      ),
    ).toHaveTextContent("blocked 2");

    fireEvent.click(screen.getByRole("button", { name: "Filter sessions" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Blocked" }));
    expect(screen.getByText("Two-run session")).toBeInTheDocument();

    act(() => {
      websocket.handler?.(attentionEvent("run:run-1", 7, null, null));
    });
    expect(
      await screen.findByLabelText(
        "Blocked attention: 1; Operator input required",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("Two-run session")).toBeInTheDocument();

    act(() => {
      websocket.handler?.(attentionEvent("run:run-2", 8, null, null));
    });
    await waitFor(() => {
      expect(screen.queryByLabelText(/Blocked attention:/)).toBeNull();
      expect(screen.queryByText("Two-run session")).toBeNull();
    });
  });
});
