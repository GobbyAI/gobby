/**
 * §6.1 keyboard-only operation acceptance (6.1.2): every interactive surface
 * in all four wiki modes stays operable without a pointer — the mode
 * radiogroup cycles with arrows/Home/End, the page tree supports
 * arrows + Home/End jumps, quick-open selects with arrows + Enter, the reader
 * kebab opens/navigates/closes from the keyboard and Escape hands focus back
 * to its trigger, code mode keeps the same tree contract alongside the
 * freshness strip, ask citations activate from the keyboard, and the research
 * composer's disclosure, switch, and launch button all drive by key.
 *
 * Complements (not duplicates) existing coverage: tree arrows + Enter
 * (WikiBrowse), Cmd+K open / Escape close (WikiBrowse), graph Escape + zoom
 * keys (WikiGraph), ask Enter submit (WikiAskMode), editor Cmd+S
 * (WikiPageEditor).
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { clearProviderModelCache } from "../../../../lib/providerModels";
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

vi.mock("../../../../hooks/useWebSocketEvent", () => ({
  useWebSocketEvent: () => undefined,
  useWebSocketConnected: () => true,
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
  Virtuoso: ({
    totalCount,
    itemContent,
  }: {
    totalCount: number;
    itemContent: (index: number) => ReactNode;
  }) => (
    <div data-testid="virtuoso">
      {Array.from({ length: totalCount }, (_, index) => (
        <div key={index}>{itemContent(index)}</div>
      ))}
    </div>
  ),
}));

// ── Harness ─────────────────────────────────────────────────────

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status < 400,
    status,
    json: async () => body,
  } as Response;
}

function codewikiStatusBody() {
  const finishedAt = new Date(Date.now() - 5 * 60_000).toISOString();
  return {
    pending_roots: [],
    running_roots: [],
    active_flush_tasks: 0,
    last_run: {
      outcome: "success",
      root_path: "/repo",
      project_id: "p1",
      changed_count: 12,
      indexed: true,
      error: null,
      started_at: finishedAt,
      finished_at: finishedAt,
    },
  };
}

/** One stub covering all four modes: wiki routes + pipelines + providers. */
function stubA11yFetch() {
  const runBodies: Array<Record<string, unknown>> = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const route = url.pathname;
    if (route.includes("/api/pipelines/run")) {
      runBodies.push(JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>);
      return jsonResponse({ execution_id: "exec-a11y", status: "pending" }, 202);
    }
    if (route.includes("/api/pipelines/executions")) {
      return jsonResponse({ executions: [], total: 0, status_summary: {} });
    }
    if (route.includes("/api/providers/models")) return jsonResponse({ providers: [] });
    if (route.includes("/api/code-index/codewiki/status")) {
      return jsonResponse(codewikiStatusBody());
    }
    if (route.includes("/api/projects/")) {
      return jsonResponse({ id: "p1", repo_path: "/repo" });
    }
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
  return { fetchMock, runBodies };
}

const ASK_HISTORY_KEY = "gobby:wiki-tab:ask-history";

/** Pre-normalized ask entry with one resolvable citation (Gobby). */
function seedAskHistory() {
  window.sessionStorage.setItem(
    ASK_HISTORY_KEY,
    JSON.stringify([
      {
        id: "seed-citations",
        question: "What is Gobby?",
        llm: true,
        ts: Date.now() - 5 * 60_000,
        envelope: {
          status: "answered",
          degraded: false,
          degradedSources: [],
          answer: "Gobby is the daemon; see [[knowledge/concepts/gobby|Gobby]].",
          model: null,
          citations: [
            { target: "knowledge/concepts/gobby", title: "Gobby", resolvedPath: null },
          ],
          groundingWarnings: [],
          hits: [],
          codeCitations: [],
          warnings: [],
          hint: null,
          aiStatus: null,
          aiError: null,
        },
      },
    ]),
  );
}

function seedMode(mode: string) {
  window.localStorage.setItem("gobby:wiki-tab:mode", mode);
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", MockResizeObserver);
  vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);
  clearProviderModelCache();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.localStorage.clear();
  window.sessionStorage.clear();
});

// ── Mode switcher ───────────────────────────────────────────────

describe("mode switcher keyboard operation", () => {
  it("cycles through all four modes with arrows and jumps with Home/End", async () => {
    stubA11yFetch();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    const wikiRadio = await screen.findByRole("radio", { name: "Wiki" });
    expect(wikiRadio).toHaveAttribute("aria-checked", "true");
    wikiRadio.focus();

    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("radio", { name: "Code" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(
      await screen.findByRole("status", { name: /codewiki freshness/i }),
    ).toBeInTheDocument();

    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("radio", { name: "Ask" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(await screen.findByRole("textbox", { name: /ask the wiki/i })).toBeInTheDocument();

    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("radio", { name: "Research" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    expect(
      await screen.findByRole("textbox", { name: "Research question" }),
    ).toBeInTheDocument();

    await user.keyboard("{Home}");
    expect(screen.getByRole("radio", { name: "Wiki" })).toHaveAttribute(
      "aria-checked",
      "true",
    );

    await user.keyboard("{End}");
    expect(screen.getByRole("radio", { name: "Research" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
  });
});

// ── Browse mode ─────────────────────────────────────────────────

describe("browse mode keyboard operation", () => {
  it("jumps to the first and last tree rows with Home and End", async () => {
    stubA11yFetch();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    const tree = await screen.findByRole("tree", { name: /wiki pages/i });
    const rows = within(tree).getAllByRole("treeitem");
    expect(rows.length).toBeGreaterThan(1);

    // The wiki tree moves focus only (selectionFollowsFocus: false — selecting
    // opens pages), so Home/End roam the tabindex anchor without selecting.
    // DOM focus lands on the next animation frame, so wait for it between jumps.
    rows[0].focus();
    await user.keyboard("{End}");
    const rowsAfterEnd = within(tree).getAllByRole("treeitem");
    const lastRow = rowsAfterEnd[rowsAfterEnd.length - 1];
    expect(lastRow).toHaveAttribute("tabindex", "0");
    await waitFor(() => expect(document.activeElement).toBe(lastRow));

    await user.keyboard("{Home}");
    const rowsAfterHome = within(tree).getAllByRole("treeitem");
    expect(rowsAfterHome[0]).toHaveAttribute("tabindex", "0");
  });

  it("selects a quick-open match with arrows and Enter alone", async () => {
    stubA11yFetch();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    const tree = await screen.findByRole("tree", { name: /wiki pages/i });
    within(tree).getAllByRole("treeitem")[0].focus();
    await user.keyboard("{Meta>}k{/Meta}");

    const palette = await screen.findByRole("combobox", { name: /quick open/i });
    await user.type(palette, "gobby");
    await waitFor(() =>
      expect(screen.getAllByRole("option").length).toBeGreaterThanOrEqual(1),
    );
    await user.keyboard("{Enter}");

    expect(screen.queryByRole("dialog", { name: /quick open/i })).not.toBeInTheDocument();
    expect(await screen.findByRole("heading", { level: 1, name: /gobby/i })).toBeInTheDocument();
  });

  it("claims Cmd+K inside the pane so the app-level palette never sees it", async () => {
    stubA11yFetch();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    const tree = await screen.findByRole("tree", { name: /wiki pages/i });
    const seenAtWindow = vi.fn();
    window.addEventListener("keydown", seenAtWindow);

    within(tree).getAllByRole("treeitem")[0].focus();
    await user.keyboard("{Meta>}k{/Meta}");

    expect(await screen.findByRole("dialog", { name: /quick open/i })).toBeInTheDocument();
    const leakedK = seenAtWindow.mock.calls
      .map(([event]) => (event as KeyboardEvent).key)
      .filter((key) => key === "k");
    expect(leakedK).toHaveLength(0);
    window.removeEventListener("keydown", seenAtWindow);
  });

  it("opens the reader kebab by key, roves its items, and Escape returns focus to the trigger", async () => {
    stubA11yFetch();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    const tree = await screen.findByRole("tree", { name: /wiki pages/i });
    await user.click(within(tree).getByRole("treeitem", { name: /wiki index/i }));
    await screen.findByRole("heading", { level: 1 });

    const trigger = screen.getByRole("button", { name: "Page actions" });
    trigger.focus();
    await user.keyboard("{Enter}");

    const menu = await screen.findByRole("menu", { name: "Page actions" });
    const items = within(menu).getAllByRole("menuitem");
    await waitFor(() => expect(document.activeElement).toBe(items[0]));
    await user.keyboard("{ArrowDown}");
    expect(document.activeElement).toBe(items[1]);

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu", { name: "Page actions" })).not.toBeInTheDocument();
    expect(document.activeElement).toBe(trigger);
  });
});

// ── Code mode ───────────────────────────────────────────────────

describe("code mode keyboard operation", () => {
  it("keeps the promoted code tree operable with the same arrow/Home/End contract", async () => {
    stubA11yFetch();
    const user = userEvent.setup();
    seedMode("code");
    render(<WikiTab projectId="p1" />);

    expect(
      await screen.findByRole("status", { name: /codewiki freshness/i }),
    ).toBeInTheDocument();

    const tree = await screen.findByRole("tree", { name: /wiki pages/i });
    const rows = within(tree).getAllByRole("treeitem");
    expect(rows.length).toBeGreaterThan(1);

    rows[0].focus();
    await user.keyboard("{End}");
    const afterEnd = within(tree).getAllByRole("treeitem");
    const lastRow = afterEnd[afterEnd.length - 1];
    expect(lastRow).toHaveAttribute("tabindex", "0");
    await waitFor(() => expect(document.activeElement).toBe(lastRow));

    await user.keyboard("{Home}");
    expect(within(tree).getAllByRole("treeitem")[0]).toHaveAttribute("tabindex", "0");
  });
});

// ── Ask mode ────────────────────────────────────────────────────

describe("ask mode keyboard operation", () => {
  it("reopens a history answer and activates a citation chip by key", async () => {
    stubA11yFetch();
    const user = userEvent.setup();
    seedMode("ask");
    seedAskHistory();
    render(<WikiTab projectId="p1" />);

    const history = await screen.findByRole("list", { name: "Ask history" });
    const entry = within(history).getByRole("button", { name: /^what is gobby/i });
    entry.focus();
    await user.keyboard("{Enter}");

    const citations = await screen.findByRole("list", { name: "Citations" });
    const chip = within(citations).getByRole("button", { name: "Gobby" });
    chip.focus();
    await user.keyboard("{Enter}");

    expect(await screen.findByRole("heading", { level: 1, name: /gobby/i })).toBeInTheDocument();
    expect(window.localStorage.getItem("gobby:wiki-tab:mode")).toBe("wiki");
  });
});

// ── Research mode ───────────────────────────────────────────────

describe("research mode keyboard operation", () => {
  it("drives the options disclosure, switch, and launch button by key", async () => {
    const { runBodies } = stubA11yFetch();
    const user = userEvent.setup();
    seedMode("research");
    render(<WikiTab projectId="p1" />);

    const composer = await screen.findByRole("textbox", { name: "Research question" });
    await waitFor(() => expect(composer).toBeEnabled());
    await user.type(composer, "How do watchers work?");

    const disclosure = screen.getByRole("button", { name: "Options" });
    expect(disclosure).toHaveAttribute("aria-expanded", "false");
    disclosure.focus();
    await user.keyboard("{Enter}");
    expect(disclosure).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("textbox", { name: "Topic slug" })).toBeInTheDocument();

    const followUps = screen.getByRole("switch", { name: "Create follow-up tasks" });
    expect(followUps).toHaveAttribute("aria-checked", "false");
    followUps.focus();
    await user.keyboard(" ");
    expect(followUps).toHaveAttribute("aria-checked", "true");

    const run = screen.getByRole("button", { name: "Run research" });
    run.focus();
    await user.keyboard("{Enter}");

    await waitFor(() => expect(runBodies.length).toBe(1));
    const inputs = runBodies[0].inputs as Record<string, unknown>;
    expect(inputs.question).toBe("How do watchers work?");
    expect(inputs.create_tasks).toBe("true");
  });
});
