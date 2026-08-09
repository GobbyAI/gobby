/**
 * §4.2 codewiki mode acceptance (4.2.1–4.2.3): the code tree browses the
 * mirror with promoted roots and collapsed-by-default folders, code pages
 * render mermaid + highlighted fences and expose Copy source path while
 * staying read-only, while the status strip explains that generation is
 * paused pending the wiki redesign.
 */

import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WikiTab } from "../../WikiTab";
import {
  backlinksEnvelope,
  browseGraphEnvelope,
  browseReadCodeEnvelope,
  browseReadGobbyEnvelope,
  browseReadRunnerEnvelope,
  healthEnvelope,
  pagesEnvelope,
  sourcesEnvelope,
  statusEnvelope,
} from "./fixtures";

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
  Prism: ({ children, language }: { children: string; language: string }) => (
    <pre data-testid="syntax-highlighter" data-language={language}>
      {children}
    </pre>
  ),
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

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status < 400,
    status,
    json: async () => body,
  } as Response;
}

/** Dormant snapshot as served by GET /api/wiki/code/status. */
function codewikiStatusBody() {
  return {
    enabled: false,
    state: "disabled",
    reason: "pending_wiki_redesign",
  };
}

interface CodeModeFetchOverrides {
  pages?: Response;
  codewikiStatus?: Response;
}

function stubCodeModeFetch(overrides: CodeModeFetchOverrides = {}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const route = url.pathname;
    if (route.includes("/api/wiki/code/status")) {
      return overrides.codewikiStatus ?? jsonResponse(codewikiStatusBody());
    }
    if (route.includes("/api/wiki/status")) return jsonResponse(statusEnvelope);
    if (route.includes("/api/wiki/health")) return jsonResponse(healthEnvelope);
    if (route.includes("/api/wiki/sources")) return jsonResponse(sourcesEnvelope);
    if (route.includes("/api/wiki/pages")) {
      return overrides.pages ?? jsonResponse(pagesEnvelope);
    }
    if (route.includes("/api/wiki/graph")) return jsonResponse(browseGraphEnvelope);
    if (route.includes("/api/wiki/backlinks")) return jsonResponse(backlinksEnvelope);
    if (route.includes("/api/wiki/read")) {
      const path = url.searchParams.get("path");
      if (path === "code/files/src/gobby/runner.py.md") {
        return jsonResponse(browseReadRunnerEnvelope);
      }
      if (path === "code/_architecture.md") return jsonResponse(browseReadCodeEnvelope);
      return jsonResponse(browseReadGobbyEnvelope);
    }
    return jsonResponse({ ok: true, payload: {} });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function seedCodeMode() {
  window.localStorage.setItem("gobby:wiki-tab:mode", "code");
}

/** Bulk code pages for the at-scale search-list assertion. */
function bulkCodePagesEnvelope(count: number) {
  const pages = Array.from({ length: count }, (_, index) => ({
    content_hash: `bulk${index}`,
    path: `code/files/src/mod_${index}.py.md`,
    tags: [],
    title: `src/mod_${index}.py`,
    updated_at: "2026-07-10T00:00:00+00:00",
  }));
  return {
    ok: true,
    command: "pages",
    stderr: "",
    payload: { command: "pages", scope: { kind: "project", id: "p1" }, pages, outputs: [] },
  };
}

async function openRunnerPage(user: ReturnType<typeof userEvent.setup>) {
  const tree = await screen.findByRole("tree", { name: /wiki pages/i });
  await user.click(within(tree).getByRole("treeitem", { name: /^files$/i }));
  await user.click(await within(tree).findByRole("treeitem", { name: /^src$/i }));
  await user.click(await within(tree).findByRole("treeitem", { name: /^gobby$/i }));
  await user.click(await within(tree).findByRole("treeitem", { name: /runner\.py/i }));
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", MockResizeObserver);
  vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.localStorage.clear();
  window.sessionStorage.clear();
});

describe("code mode tree (4.2.1)", () => {
  it("promotes the codewiki mirror's top level to tree roots", async () => {
    stubCodeModeFetch();
    seedCodeMode();
    render(<WikiTab projectId="p1" />);

    const tree = await screen.findByRole("tree", { name: /wiki pages/i });
    expect(within(tree).getByRole("treeitem", { name: /^files$/i })).toBeInTheDocument();
    expect(
      within(tree).getByRole("treeitem", { name: /architecture overview/i }),
    ).toBeInTheDocument();
    // The mirror's own top level is the root set — no "code" wrapper folder,
    // no wiki-mode roots.
    expect(within(tree).queryByRole("treeitem", { name: /^code$/i })).not.toBeInTheDocument();
    expect(
      within(tree).queryByRole("treeitem", { name: /knowledge/i }),
    ).not.toBeInTheDocument();
    expect(within(tree).queryByRole("treeitem", { name: /recaps/i })).not.toBeInTheDocument();
  });

  it("collapses mirror folders by default and expands on demand", async () => {
    stubCodeModeFetch();
    seedCodeMode();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    const tree = await screen.findByRole("tree", { name: /wiki pages/i });
    const files = within(tree).getByRole("treeitem", { name: /^files$/i });
    expect(files).toHaveAttribute("aria-expanded", "false");
    expect(within(tree).queryByRole("treeitem", { name: /^src$/i })).not.toBeInTheDocument();

    await user.click(files);
    expect(await within(tree).findByRole("treeitem", { name: /^src$/i })).toBeInTheDocument();
    expect(within(tree).getByRole("treeitem", { name: /^crates$/i })).toBeInTheDocument();
    // Grandchildren stay unrendered until their folder expands.
    expect(
      within(tree).queryByRole("treeitem", { name: /runner\.py/i }),
    ).not.toBeInTheDocument();
  });

  it("switches search to the flat virtualized list at scale", async () => {
    stubCodeModeFetch({ pages: jsonResponse(bulkCodePagesEnvelope(150)) });
    seedCodeMode();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    await screen.findByRole("tree", { name: /wiki pages/i });
    await user.type(screen.getByRole("searchbox", { name: /filter wiki/i }), "mod_");

    expect(await screen.findByTestId("virtuoso")).toBeInTheDocument();
    expect(screen.queryByRole("tree", { name: /wiki pages/i })).not.toBeInTheDocument();
    const matchList = screen.getByRole("list", { name: /matching pages/i });
    expect(within(matchList).getAllByText(/mod_1\d\.py/).length).toBeGreaterThan(0);
  });
});

describe("code page reader affordances (4.2.2)", () => {
  it("renders mermaid fences as diagrams and other fences highlighted", async () => {
    stubCodeModeFetch();
    seedCodeMode();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);
    await openRunnerPage(user);

    expect(await screen.findByRole("img", { name: /mermaid diagram/i })).toBeInTheDocument();
    const highlighted = screen.getByTestId("syntax-highlighter");
    expect(highlighted).toHaveAttribute("data-language", "python");
  });

  it("exposes Copy source path and stays read-only", async () => {
    stubCodeModeFetch();
    seedCodeMode();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);
    await openRunnerPage(user);

    await screen.findByRole("img", { name: /mermaid diagram/i });
    expect(screen.queryByRole("button", { name: /edit page/i })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Page actions" }));
    expect(
      await screen.findByRole("menuitem", { name: /copy source path/i }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /^delete$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /new page/i })).not.toBeInTheDocument();

    await user.click(screen.getByRole("menuitem", { name: /copy source path/i }));
    expect(await window.navigator.clipboard.readText()).toBe("src/gobby/runner.py");
  });
});

describe("dormant codewiki status", () => {
  it("renders the paused badge and reason above the code tree", async () => {
    const fetchMock = stubCodeModeFetch();
    seedCodeMode();
    render(<WikiTab projectId="p1" />);

    const strip = await screen.findByRole("status", { name: /codewiki status/i });
    expect(within(strip).getByText("Paused")).toBeInTheDocument();
    expect(strip).toHaveTextContent(/paused pending wiki redesign/i);
    expect(
      fetchMock.mock.calls.filter((call) =>
        String(call[0]).includes("/api/wiki/code/status"),
      ),
    ).toHaveLength(1);
    expect(
      fetchMock.mock.calls.some((call) => String(call[0]).includes("/api/code-index/")),
    ).toBe(false);
  });

  it("does not poll while codewiki is disabled", async () => {
    const intervalSpy = vi.spyOn(window, "setInterval");
    const fetchMock = stubCodeModeFetch();
    seedCodeMode();
    render(<WikiTab projectId="p1" />);

    await screen.findByRole("status", { name: /codewiki status/i });
    expect(intervalSpy).not.toHaveBeenCalledWith(expect.any(Function), 30_000);
    expect(
      fetchMock.mock.calls.filter((call) =>
        String(call[0]).includes("/api/wiki/code/status"),
      ),
    ).toHaveLength(1);
  });

  it("degrades quietly when the dormant status route is unavailable", async () => {
    stubCodeModeFetch({
      codewikiStatus: jsonResponse({ detail: "Codewiki status unavailable" }, 503),
    });
    seedCodeMode();
    render(<WikiTab projectId="p1" />);

    const strip = await screen.findByRole("status", { name: /codewiki status/i });
    expect(strip).toHaveTextContent(/status unavailable/i);
  });

  it("stays out of wiki mode along with the refresh action", async () => {
    const fetchMock = stubCodeModeFetch();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    await screen.findByRole("tree", { name: /wiki pages/i });
    expect(
      screen.queryByRole("status", { name: /codewiki status/i }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Wiki actions" }));
    await screen.findByRole("menuitem", { name: /refresh index/i });
    expect(
      screen.queryByRole("menuitem", { name: /refresh codewiki/i }),
    ).not.toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some((call) => String(call[0]).includes("/api/code-index/")),
    ).toBe(false);
  });

  it("offers no codewiki refresh action", async () => {
    stubCodeModeFetch();
    seedCodeMode();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    await screen.findByRole("tree", { name: /wiki pages/i });
    await user.click(screen.getByRole("button", { name: "Wiki actions" }));
    expect(
      screen.queryByRole("menuitem", { name: /refresh codewiki/i }),
    ).not.toBeInTheDocument();
  });
});
