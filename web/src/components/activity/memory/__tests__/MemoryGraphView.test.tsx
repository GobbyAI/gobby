import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useIsMobile } from "../../../../hooks/useIsMobile";
import { MemoryTab } from "../../MemoryTab";
import { MemoryGraphView } from "../MemoryGraphView";

vi.mock("../../../../hooks/useIsMobile", () => ({
  useIsMobile: vi.fn(),
}));

vi.mock("../KnowledgeGraph", () => ({
  KnowledgeGraph: () => <div data-testid="knowledge-graph">Knowledge graph canvas</div>,
}));

vi.mock("../../../shared/ResizeHandle", () => ({
  ResizeHandle: () => <div data-testid="resize-handle" />,
}));

const originalFetch = globalThis.fetch;

function jsonResponse(data: unknown) {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function setupFetch() {
  const memory = {
    id: "mem-graph",
    memory_type: "fact",
    content: "Graph opens full width",
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    project_id: "project-1",
    source_type: "agent",
    source_session_id: null,
    importance: 0.5,
    access_count: 0,
    last_accessed_at: null,
    tags: ["graph"],
  };

  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;

    if (url.includes("/api/memories/stats")) {
      return jsonResponse({
        total_count: 1,
        by_type: { fact: 1, preference: 0, pattern: 0, context: 0 },
        recent_count: 1,
        avg_importance: 0.5,
        project_id: "project-1",
      });
    }
    if (url.includes("/api/memories?")) {
      return jsonResponse({ memories: [memory] });
    }
    return jsonResponse({ error: "no mock route matched" });
  });

  globalThis.fetch = fetchMock as unknown as typeof fetch;
  window.fetch = fetchMock as unknown as typeof fetch;
}

describe("Memory graph activity view", () => {
  afterEach(() => {
    globalThis.fetch = originalFetch;
    window.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("releases the panel override through one close path for button, Escape, and unmount", async () => {
    const releasePanelOverride = vi.fn();
    const onClose = vi.fn();

    const buttonClose = render(
      <MemoryGraphView
        fetchKnowledgeGraph={vi.fn().mockResolvedValue({ entities: [], relationships: [] })}
        fetchEntityNeighbors={vi.fn()}
        releasePanelOverride={releasePanelOverride}
        onClose={onClose}
      />,
    );

    expect(await screen.findByTestId("knowledge-graph")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Close graph" }));
    expect(releasePanelOverride).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
    buttonClose.unmount();

    const escapeClose = render(
      <MemoryGraphView
        fetchKnowledgeGraph={vi.fn().mockResolvedValue({ entities: [], relationships: [] })}
        fetchEntityNeighbors={vi.fn()}
        releasePanelOverride={releasePanelOverride}
        onClose={onClose}
      />,
    );
    fireEvent.keyDown(window, { key: "Escape" });
    expect(releasePanelOverride).toHaveBeenCalledTimes(2);
    expect(onClose).toHaveBeenCalledTimes(2);
    escapeClose.unmount();

    const unmountClose = render(
      <MemoryGraphView
        fetchKnowledgeGraph={vi.fn().mockResolvedValue({ entities: [], relationships: [] })}
        fetchEntityNeighbors={vi.fn()}
        releasePanelOverride={releasePanelOverride}
        onClose={vi.fn()}
      />,
    );
    unmountClose.unmount();
    expect(releasePanelOverride).toHaveBeenCalledTimes(3);
  });

  it("opens the Memory graph full-width from the desktop detail strip and restores on close", async () => {
    vi.mocked(useIsMobile).mockReturnValue(false);
    setupFetch();
    const requestPanelOverride = vi.fn();
    const releasePanelOverride = vi.fn();

    render(
      <MemoryTab
        projectId="project-1"
        requestPanelOverride={requestPanelOverride}
        releasePanelOverride={releasePanelOverride}
      />,
    );

    expect(await screen.findByRole("button", { name: "Graph" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Graph" }));

    expect(requestPanelOverride).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("heading", { name: "Memory graph" })).toBeInTheDocument();
    expect(await screen.findByTestId("knowledge-graph")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Memory content" })).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Close graph" }));

    expect(releasePanelOverride).toHaveBeenCalledTimes(1);
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: "Memory content" })).toBeInTheDocument(),
    );
  });

  it("hides the graph entry on mobile so the graph cannot mount in the narrow column", async () => {
    vi.mocked(useIsMobile).mockReturnValue(true);
    setupFetch();

    render(
      <MemoryTab
        projectId="project-1"
        requestPanelOverride={vi.fn()}
        releasePanelOverride={vi.fn()}
      />,
    );

    expect(await screen.findByText("Graph opens on desktop only.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Graph" })).not.toBeInTheDocument();
    expect(screen.queryByTestId("knowledge-graph")).not.toBeInTheDocument();
  });
});
