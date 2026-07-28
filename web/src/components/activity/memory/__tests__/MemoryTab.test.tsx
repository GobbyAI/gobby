import type { ReactElement, ReactNode } from "react";
import {
  render as baseRender,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ActivityActionButtons,
  ActivityActionsProvider,
} from "../../ActivityActionsContext";
import { ACTIVITY_PANEL_TABS } from "../../ActivityPanelTabs";
import { MemoryTab } from "../../MemoryTab";

// The tab's toolbar (scope selector / Filter / Search) renders in the shared
// panel header in the real layout; mount it alongside the tab so those
// controls are reachable in tests.
function HeaderHarness({ children }: { children: ReactNode }) {
  return (
    <ActivityActionsProvider>
      <ActivityActionButtons />
      {children}
    </ActivityActionsProvider>
  );
}

const render = (ui: ReactElement) =>
  baseRender(ui, { wrapper: HeaderHarness });

// The search bar is hidden until the header Search toggle opens it.
async function openSearch(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Search memories" }));
}

vi.mock("../../../shared/ResizeHandle", () => ({
  ResizeHandle: () => <div data-testid="resize-handle" />,
}));

const originalFetch = globalThis.fetch;

type MemoryRecord = {
  id: string;
  memory_type: string;
  content: string;
  created_at: string;
  updated_at: string;
  project_id: string;
  is_global: boolean;
  source_type: string | null;
  source_session_id: string | null;
  importance: number;
  access_count: number;
  last_accessed_at: string | null;
  tags: string[] | null;
  deleted_at: string | null;
  dream_action: string | null;
  last_dreamed_at: string | null;
};

const recentIso = new Date(Date.now() - 60 * 60 * 1000).toISOString();
const oldIso = new Date(Date.now() - 72 * 60 * 60 * 1000).toISOString();

function makeMemory(overrides: Partial<MemoryRecord>): MemoryRecord {
  return {
    id: "mem-default",
    memory_type: "fact",
    content: "Default memory",
    created_at: recentIso,
    updated_at: recentIso,
    project_id: "project-1",
    is_global: false,
    source_type: "agent",
    source_session_id: null,
    importance: 0.5,
    access_count: 0,
    last_accessed_at: null,
    tags: [],
    deleted_at: null,
    dream_action: null,
    last_dreamed_at: null,
    ...overrides,
  };
}

function jsonResponse(data: unknown, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

interface FetchRouteOptions {
  promote?: Set<string>;
  restore?: Set<string>;
  searchResults?: MemoryRecord[];
}

function setupFetch(initialMemories: MemoryRecord[], options: FetchRouteOptions = {}) {
  let memories = [...initialMemories];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const method = init?.method ?? "GET";

    if (url.endsWith("/api/config/values") && method === "GET") {
      return jsonResponse({
        values: {
          memory: {
            dream: {
              purge_review_after_days: 90,
              purge_delete_after_days: 30,
            },
          },
        },
      });
    }
    if (url.includes("/api/memories/stats")) {
      return jsonResponse({
        total_count: memories.length,
        by_type: { fact: 1, preference: 1, pattern: 0, context: 0 },
        recent_count: 1,
        avg_importance: 0.5,
        project_id: "project-1",
      });
    }
    if (url.includes("/api/memories/search?") && method === "GET") {
      const query = new URL(url, "http://localhost").searchParams.get("q")?.toLowerCase() ?? "";
      const results = options.searchResults ?? memories.filter((memory) =>
        memory.content.toLowerCase().includes(query) ||
        memory.memory_type.toLowerCase().includes(query) ||
        (memory.tags ?? []).some((tag) => tag.toLowerCase().includes(query))
      );
      return jsonResponse({ results });
    }
    if (url.includes("/api/memories?") && method === "GET") {
      return jsonResponse({ memories });
    }
    if (url.endsWith("/api/memories/mem-recent") && method === "PUT") {
      return jsonResponse({
        ...memories[0],
        ...JSON.parse(String(init?.body)),
        updated_at: new Date().toISOString(),
      });
    }
    if (url.endsWith("/api/memories/mem-recent") && method === "DELETE") {
      return jsonResponse({ ok: true });
    }
    if (url.endsWith("/promote") && method === "POST") {
      const memoryId = url.split("/").slice(-2, -1)[0];
      if (options.promote?.has(memoryId)) return jsonResponse({ error: "not found" }, 404);
      const index = memories.findIndex((memory) => memory.id === memoryId);
      if (index === -1) return jsonResponse({ error: "not found" }, 404);
      memories = memories.map((memory, memoryIndex) =>
        memoryIndex === index ? { ...memory, is_global: true } : memory,
      );
      return jsonResponse(memories[index]);
    }
    if (url.includes("/restore") && method === "POST") {
      const memoryId = url.split("/").slice(-2, -1)[0];
      if (options.restore?.has(memoryId)) return jsonResponse({ error: "not found" }, 404);
      const index = memories.findIndex((memory) => memory.id === memoryId);
      if (index === -1) return jsonResponse({ error: "not found" }, 404);
      memories = memories.map((memory, memoryIndex) =>
        memoryIndex === index
          ? { ...memory, deleted_at: null, dream_action: null }
          : memory,
      );
      return jsonResponse(memories[index]);
    }
    return jsonResponse({ error: "no mock route matched" }, 404);
  });

  globalThis.fetch = fetchMock as unknown as typeof fetch;
  window.fetch = fetchMock as unknown as typeof fetch;
  return fetchMock;
}

function lastJsonBody(fetchMock: ReturnType<typeof setupFetch>) {
  const call = fetchMock.mock.calls
    .slice()
    .reverse()
    .find(([url, init]) => String(url).includes("/api/memories/mem-recent") && Boolean(init?.body));
  return call?.[1]?.body ? JSON.parse(String(call[1].body)) : null;
}

describe("Memory activity tab", () => {
  afterEach(() => {
    globalThis.fetch = originalFetch;
    window.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it("registers the tab, filters rows, and saves drafts", async () => {
    const fetchMock = setupFetch([
      makeMemory({
        id: "mem-recent",
        memory_type: "fact",
        content: "Persist panel width override",
        created_at: recentIso,
        updated_at: recentIso,
        tags: ["activity"],
      }),
      makeMemory({
        id: "mem-old",
        memory_type: "preference",
        content: "Use a quiet palette for dashboards",
        created_at: oldIso,
        updated_at: oldIso,
        tags: ["design"],
      }),
    ]);
    const user = userEvent.setup();

    expect(ACTIVITY_PANEL_TABS.some((tab) => tab.id === "memory")).toBe(true);
    render(<MemoryTab projectId="project-1" />);

    expect(
      await screen.findByRole("button", { name: "Select Persist panel width override" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Select Use a quiet palette for dashboards" }))
      .toBeInTheDocument();
    await openSearch(user);
    expect(screen.getByRole("searchbox", { name: "Search memories" })).toBeInTheDocument();
    // Manual refresh is gone — the list stays current via live updates (#19152).
    expect(screen.queryByRole("button", { name: "Refresh memories" })).not.toBeInTheDocument();
    // Scope selector defaults to Project.
    expect(screen.getByRole("radio", { name: "Project" })).toHaveAttribute(
      "aria-checked",
      "true",
    );

    const paletteMemory = screen.getByRole("button", {
      name: "Select Use a quiet palette for dashboards",
    });
    paletteMemory.focus();
    await user.keyboard(" ");
    expect(paletteMemory.parentElement).toHaveClass("activity-list-row--selected");

    await user.click(screen.getByRole("button", { name: "Filter memories" }));
    await user.click(screen.getByRole("checkbox", { name: "Last 24 hours" }));
    expect(screen.queryByText("Use a quiet palette for dashboards")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Select Persist panel width override" }))
      .toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Filter memories" }));
    await user.click(screen.getByRole("checkbox", { name: "Last 24 hours" }));
    await user.type(screen.getByRole("searchbox", { name: "Search memories" }), "palette");
    expect(screen.queryByText("Persist panel width override")).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Select Use a quiet palette for dashboards" }))
      .toBeInTheDocument();

    await user.clear(screen.getByRole("searchbox", { name: "Search memories" }));

    await user.click(screen.getByRole("button", { name: "Select Persist panel width override" }));
    await user.clear(screen.getByRole("textbox", { name: "Memory content" }));
    await user.type(screen.getByRole("textbox", { name: "Memory content" }), "Draft should be discarded");
    await user.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.getByRole("textbox", { name: "Memory content" }))
      .toHaveValue("Persist panel width override");

    await user.clear(screen.getByRole("textbox", { name: "Memory content" }));
    await user.type(screen.getByRole("textbox", { name: "Memory content" }), "Panel override is transient");
    await user.selectOptions(screen.getByRole("combobox", { name: "Memory type" }), "pattern");
    await user.type(screen.getByLabelText("Add Tags"), "panel{Enter}");
    expect(screen.getByRole("button", { name: "Discard" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(lastJsonBody(fetchMock)).toMatchObject({
        content: "Panel override is transient",
        memory_type: "pattern",
        tags: ["activity", "panel"],
      }),
    );

    await user.click(screen.getByRole("button", { name: "Open actions for Persist panel width override" }));
    const menu = screen.getByRole("menu", { name: "Actions for Persist panel width override" });
    expect(within(menu).getByRole("menuitem", { name: "Copy content" })).toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: "Delete" })).toBeInTheDocument();
  }, 10_000);

  it("uses server search results beyond the 100-row list cap", async () => {
    const listedMemories = Array.from({ length: 100 }, (_, index) =>
      makeMemory({
        id: `mem-${index}`,
        content: `Listed memory ${index}`,
      }),
    );
    const serverOnlyMemory = makeMemory({
      id: "mem-server-only",
      content: "Server-only memory beyond list cap",
    });
    const fetchMock = setupFetch(listedMemories, { searchResults: [serverOnlyMemory] });
    const user = userEvent.setup();

    render(<MemoryTab projectId="project-1" />);
    expect(await screen.findByRole("button", { name: "Select Listed memory 0" }))
      .toBeInTheDocument();

    await openSearch(user);
    await user.type(screen.getByRole("searchbox", { name: "Search memories" }), "server-only");

    expect(
      await screen.findByRole("button", { name: "Select Server-only memory beyond list cap" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Listed memory 0")).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([url]) => {
        const requestUrl = String(url);
        return requestUrl.includes("/api/memories/search?") && requestUrl.includes("q=server-only");
      }),
    ).toBe(true);
  });

  it("shows memory scope and promotes a project memory to global", async () => {
    const fetchMock = setupFetch([
      makeMemory({
        id: "mem-recent",
        content: "Universal review checklist",
        project_id: "project-1",
      }),
    ]);
    const user = userEvent.setup();

    render(<MemoryTab projectId="project-1" />);

    expect(
      await screen.findByRole("button", { name: "Select Universal review checklist" }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("Project").length).toBeGreaterThan(0);
    expect(screen.getByText("Current project")).toBeInTheDocument();

    // View All scopes so the row stays visible after promotion (#19152).
    await user.click(screen.getByRole("radio", { name: "All" }));
    const globalSwitch = screen.getByRole("switch", { name: "Global memory" });
    expect(globalSwitch).not.toBeChecked();
    await user.click(globalSwitch);

    await waitFor(() => {
      const promoted = fetchMock.mock.calls.some(
        ([reqUrl, init]) =>
          String(reqUrl).includes("/api/memories/mem-recent/promote") &&
          (init?.method ?? "GET") === "POST",
      );
      expect(promoted).toBe(true);
    });
    await waitFor(() => {
      expect(screen.getByRole("switch", { name: "Global memory" })).toBeChecked();
    });
    expect(screen.getByRole("switch", { name: "Global memory" })).toBeDisabled();
    expect(screen.getAllByText("Global").length).toBeGreaterThan(0);
    expect(screen.getByText("Available across projects")).toBeInTheDocument();
  });

  it("surfaces promote failures in the memory tab", async () => {
    setupFetch(
      [
        makeMemory({
          id: "mem-recent",
          content: "Project-only checklist",
          project_id: "project-1",
        }),
      ],
      { promote: new Set(["mem-recent"]) },
    );
    const user = userEvent.setup();

    render(<MemoryTab projectId="project-1" />);

    expect(
      await screen.findByRole("button", { name: "Select Project-only checklist" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("switch", { name: "Global memory" }));

    expect(
      await screen.findByRole("button", { name: "Dismiss error: Failed to promote memory" }),
    ).toBeInTheDocument();
  });

  it("filters by visibility, badges hidden rows, and restores them", async () => {
    const fetchMock = setupFetch([
      makeMemory({ id: "mem-recent", content: "Active fact", created_at: recentIso }),
      makeMemory({
        id: "mem-hidden",
        content: "Stale flagged fact",
        created_at: recentIso,
        deleted_at: recentIso,
        dream_action: "review",
      }),
    ]);
    const user = userEvent.setup();
    render(<MemoryTab projectId="project-1" />);

    // Active (default) hides dream-flagged rows.
    expect(await screen.findByRole("button", { name: "Select Active fact" })).toBeInTheDocument();
    expect(screen.queryByText("Stale flagged fact")).not.toBeInTheDocument();

    // Switch the visibility scope to Hidden.
    await user.click(screen.getByRole("button", { name: "Filter memories" }));
    await user.click(screen.getByRole("radio", { name: "Hidden" }));

    expect(
      await screen.findByRole("button", { name: "Select Stale flagged fact" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Active fact")).not.toBeInTheDocument();
    // Text-and-icon badge — label carries the meaning, never hue alone.
    expect(screen.getAllByText("Flagged for review").length).toBeGreaterThan(0);

    // Restore via the row action menu.
    await user.click(screen.getByRole("button", { name: "Open actions for Stale flagged fact" }));
    const hiddenMenu = screen.getByRole("menu", { name: "Actions for Stale flagged fact" });
    await user.click(within(hiddenMenu).getByRole("menuitem", { name: "Restore" }));

    await waitFor(() => {
      const restored = fetchMock.mock.calls.some(
        ([reqUrl, init]) =>
          String(reqUrl).includes("/api/memories/mem-hidden/restore") &&
          (init?.method ?? "GET") === "POST",
      );
      expect(restored).toBe(true);
    });
  });

  it("surfaces restore failures in the memory tab", async () => {
    setupFetch(
      [
        makeMemory({
          id: "mem-hidden",
          content: "Restore failure fact",
          created_at: recentIso,
          deleted_at: recentIso,
          dream_action: "review",
        }),
      ],
      { restore: new Set(["mem-hidden"]) },
    );
    const user = userEvent.setup();
    render(<MemoryTab projectId="project-1" />);

    await user.click(await screen.findByRole("button", { name: "Filter memories" }));
    await user.click(screen.getByRole("radio", { name: "Hidden" }));
    expect(
      await screen.findByRole("button", { name: "Select Restore failure fact" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Open actions for Restore failure fact" }));
    const hiddenMenu = screen.getByRole("menu", { name: "Actions for Restore failure fact" });
    await user.click(within(hiddenMenu).getByRole("menuitem", { name: "Restore" }));

    expect(
      await screen.findByRole("button", { name: "Dismiss error: Failed to restore memory" }),
    ).toBeInTheDocument();
  });
});
