import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WikiTab } from "../../WikiTab";
import { DirtyGuardProvider } from "../../DirtyGuardContext";
import type { DirtyGuard, DirtyGuardContextValue } from "../../dirtyGuard";
import {
  backlinksEnvelope,
  browseAmbiguousReadEnvelope,
  browseGraphEnvelope,
  browseReadGobbyEnvelope,
  browseReadGwikiEnvelope,
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
  default: {
    initialize: vi.fn(),
    render: vi.fn(async () => ({ svg: "<svg />" })),
  },
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

interface BrowseFetchOverrides {
  pages?: Response;
  graph?: Response;
  readByPath?: Record<string, Response>;
}

function stubBrowseFetch(overrides: BrowseFetchOverrides = {}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://localhost");
    const route = url.pathname;
    if (route.includes("/api/wiki/status")) return jsonResponse(statusEnvelope);
    if (route.includes("/api/wiki/health")) return jsonResponse(healthEnvelope);
    if (route.includes("/api/wiki/sources"))
      return jsonResponse(sourcesEnvelope);
    if (route.includes("/api/wiki/pages")) {
      return overrides.pages ?? jsonResponse(pagesEnvelope);
    }
    if (route.includes("/api/wiki/graph")) {
      return overrides.graph ?? jsonResponse(browseGraphEnvelope);
    }
    if (route.includes("/api/wiki/backlinks")) {
      return jsonResponse(backlinksEnvelope);
    }
    if (route.includes("/api/wiki/read")) {
      const path = url.searchParams.get("path");
      if (path && overrides.readByPath?.[path])
        return overrides.readByPath[path];
      if (path === "knowledge/concepts/gwiki.md") {
        return jsonResponse(browseReadGwikiEnvelope);
      }
      return jsonResponse(browseReadGobbyEnvelope);
    }
    return jsonResponse({ ok: true, payload: {} });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function readRequests(fetchMock: ReturnType<typeof vi.fn>): string[] {
  return fetchMock.mock.calls
    .map((call) => String(call[0]))
    .filter((url) => url.includes("/api/wiki/read"))
    .map((url) => {
      const parsed = new URL(url, "http://localhost");
      return (
        parsed.searchParams.get("path") ??
        parsed.searchParams.get("title") ??
        ""
      );
    });
}

function makeGuardValue(guards: DirtyGuard[]): DirtyGuardContextValue {
  const registered = new Set<DirtyGuard>(guards);
  return {
    registerDirtyGuard: (guard) => {
      registered.add(guard);
      return () => {
        registered.delete(guard);
      };
    },
    guardedRun: async (action) => {
      for (const guard of registered) {
        if (guard.isDirty() && !(await guard.confirmLeave())) return;
      }
      await action();
    },
  };
}

async function expandToConcepts(user: ReturnType<typeof userEvent.setup>) {
  const tree = await screen.findByRole("tree", { name: /wiki pages/i });
  await user.click(within(tree).getByRole("treeitem", { name: /knowledge/i }));
  await user.click(
    await within(tree).findByRole("treeitem", { name: /concepts/i }),
  );
  return tree;
}

async function openPageFromTree(
  user: ReturnType<typeof userEvent.setup>,
  name: RegExp,
) {
  const row = await screen.findByRole("treeitem", { name });
  await user.click(row);
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

describe("WikiPageTree (3.1.1)", () => {
  it("renders the vault structure from the pages listing without code pages in wiki mode", async () => {
    stubBrowseFetch();
    render(<WikiTab projectId="p1" />);

    const tree = await screen.findByRole("tree", { name: /wiki pages/i });
    expect(
      within(tree).getByRole("treeitem", { name: /knowledge/i }),
    ).toBeInTheDocument();
    expect(
      within(tree).getByRole("treeitem", { name: /recaps/i }),
    ).toBeInTheDocument();
    expect(
      within(tree).getByRole("treeitem", { name: /outputs/i }),
    ).toBeInTheDocument();
    expect(
      within(tree).getByRole("treeitem", { name: /wiki index/i }),
    ).toBeInTheDocument();
    expect(
      within(tree).queryByRole("treeitem", { name: /^code$/i }),
    ).not.toBeInTheDocument();
  });

  it("collapses the sources folder by default and expands folders on click", async () => {
    stubBrowseFetch();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    const tree = await expandToConcepts(user);
    expect(
      await within(tree).findByRole("treeitem", { name: /sources/i }),
    ).toHaveAttribute("aria-expanded", "false");
    expect(
      await within(tree).findByRole("treeitem", { name: /^gobby$/i }),
    ).toBeInTheDocument();
  });

  it("colors page icons by kind from design tokens", async () => {
    stubBrowseFetch();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    const tree = await expandToConcepts(user);
    const conceptRow = await within(tree).findByRole("treeitem", {
      name: /^gobby$/i,
    });
    expect(
      within(conceptRow).getByTestId("wiki-kind-icon").style.color,
    ).toContain("--accent");

    const folderRow = within(tree).getByRole("treeitem", { name: /recaps/i });
    expect(
      within(folderRow).getByTestId("wiki-kind-icon").style.color,
    ).toContain("--lang-folder");
  });

  it("supports keyboard navigation with Enter opening the focused page", async () => {
    stubBrowseFetch();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    const tree = await screen.findByRole("tree", { name: /wiki pages/i });
    const knowledge = within(tree).getByRole("treeitem", {
      name: /knowledge/i,
    });
    knowledge.focus();
    await user.keyboard("{ArrowRight}");
    const concepts = await within(tree).findByRole("treeitem", {
      name: /concepts/i,
    });
    await user.keyboard("{ArrowDown}");
    // Roving focus lands via requestAnimationFrame — wait it out per key.
    await waitFor(() => expect(concepts).toHaveFocus());
    await user.keyboard("{ArrowRight}");
    const gobby = await within(tree).findByRole("treeitem", {
      name: /^gobby$/i,
    });
    await user.keyboard("{ArrowDown}");
    await waitFor(() => expect(gobby).toHaveFocus());
    await user.keyboard("{Enter}");

    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "Gobby", level: 1 }),
      ).toBeInTheDocument(),
    );
  });

  it("filters to a flat match list from the toolbar search", async () => {
    stubBrowseFetch();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    await screen.findByRole("tree", { name: /wiki pages/i });
    await user.type(screen.getByRole("searchbox"), "guardrails");

    const matches = await screen.findByRole("list", {
      name: /matching pages/i,
    });
    expect(
      within(matches).getByText("Contract guardrails"),
    ).toBeInTheDocument();
    expect(within(matches).queryByText("Gwiki")).not.toBeInTheDocument();
  });

  it("shows the empty state with retry when the pages fetch fails", async () => {
    const fetchMock = stubBrowseFetch({
      pages: jsonResponse({ detail: "boom" }, 500),
    });
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    const retry = await screen.findByRole("button", { name: /retry/i });
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/wiki/pages")) return jsonResponse(pagesEnvelope);
      if (url.includes("/api/wiki/status")) return jsonResponse(statusEnvelope);
      if (url.includes("/api/wiki/health")) return jsonResponse(healthEnvelope);
      if (url.includes("/api/wiki/sources"))
        return jsonResponse(sourcesEnvelope);
      return jsonResponse({ ok: true, payload: {} });
    });
    await user.click(retry);

    expect(
      await screen.findByRole("tree", { name: /wiki pages/i }),
    ).toBeInTheDocument();
  });
});

describe("WikiPageReader (3.1.2)", () => {
  it("renders the frontmatter header, tags, and markdown body", async () => {
    stubBrowseFetch();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    await expandToConcepts(user);
    await openPageFromTree(user, /^gobby$/i);

    expect(
      await screen.findByRole("heading", { name: "Gobby", level: 1 }),
    ).toBeInTheDocument();
    expect(screen.getByText("concept")).toBeInTheDocument();
    expect(screen.getByText("compiled")).toBeInTheDocument();
    expect(screen.getByText(/local-first daemon/)).toBeInTheDocument();
    expect(screen.getByText(/details/i)).toBeInTheDocument();
  });

  it("navigates resolved wikilinks and marks unresolved ones distinctly", async () => {
    const fetchMock = stubBrowseFetch();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    await expandToConcepts(user);
    await openPageFromTree(user, /^gobby$/i);
    await screen.findByRole("heading", { name: "Gobby", level: 1 });

    const unresolved = screen.getByRole("link", { name: "Missing" });
    expect(unresolved.className).toContain("wikilink--unresolved");

    await user.click(screen.getByRole("link", { name: "Gwiki" }));
    expect(
      await screen.findByRole("heading", { name: "Gwiki", level: 1 }),
    ).toBeInTheDocument();
    expect(readRequests(fetchMock)).toContain("knowledge/concepts/gwiki.md");
  });

  it("shows a missing-page notice when an unresolved wikilink is clicked", async () => {
    const fetchMock = stubBrowseFetch();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    await expandToConcepts(user);
    await openPageFromTree(user, /^gobby$/i);
    await screen.findByRole("heading", { name: "Gobby", level: 1 });

    const readsBefore = readRequests(fetchMock).length;
    await user.click(screen.getByRole("link", { name: "Missing" }));
    expect(await screen.findByText(/missing\/page/)).toBeInTheDocument();
    expect(screen.getByText(/not been created/i)).toBeInTheDocument();
    expect(readRequests(fetchMock)).toHaveLength(readsBefore);
  });

  it("offers a match picker for ambiguous reads", async () => {
    stubBrowseFetch({
      readByPath: {
        "knowledge/concepts/gobby.md": jsonResponse(
          browseAmbiguousReadEnvelope,
        ),
      },
    });
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    await expandToConcepts(user);
    await openPageFromTree(user, /^gobby$/i);

    expect(
      await screen.findByText(/multiple pages match/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /src\/gobby\/runner\.py/i }),
    ).toBeInTheDocument();
  });

  it("lists in-body citations as a sources strip", async () => {
    stubBrowseFetch();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    await expandToConcepts(user);
    await openPageFromTree(user, /^gobby$/i);
    await screen.findByRole("heading", { name: "Gobby", level: 1 });

    const sources = screen.getByRole("region", { name: /sources/i });
    expect(within(sources).getByText(/session: c1c0c073/i)).toBeInTheDocument();
  });
});

describe("WikiBacklinks (3.1.3)", () => {
  it("lists linked mentions and unresolved mentions with navigation", async () => {
    const fetchMock = stubBrowseFetch();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    await expandToConcepts(user);
    await openPageFromTree(user, /^gobby$/i);
    await screen.findByRole("heading", { name: "Gobby", level: 1 });

    await user.click(screen.getByRole("button", { name: /linked mentions/i }));
    const backlinks = await screen.findByRole("region", {
      name: /linked mentions/i,
    });
    expect(
      await within(backlinks).findByText("Contract guardrails"),
    ).toBeInTheDocument();
    expect(within(backlinks).getByText("2026-07-07")).toBeInTheDocument();

    expect(await screen.findByText(/unresolved mentions/i)).toBeInTheDocument();
    expect(
      within(backlinks).getByRole("button", { name: "Gwiki" }),
    ).toBeInTheDocument();

    await user.click(within(backlinks).getByText("Contract guardrails"));
    await waitFor(() =>
      expect(readRequests(fetchMock)).toContain(
        "knowledge/topics/contract-guardrails.md",
      ),
    );
  });
});

describe("Quick-open and history (3.1.4)", () => {
  it("fuzzy-jumps to a page via the panel-scoped overlay", async () => {
    const fetchMock = stubBrowseFetch();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    const tree = await screen.findByRole("tree", { name: /wiki pages/i });
    within(tree)
      .getByRole("treeitem", { name: /knowledge/i })
      .focus();
    await user.keyboard("{Meta>}k{/Meta}");

    const dialog = await screen.findByRole("dialog", { name: /quick open/i });
    await user.type(within(dialog).getByRole("combobox"), "gwi");
    const resultCard = await within(dialog).findByRole("option", {
      name: /gwiki/i,
    });
    expect(resultCard).toHaveClass("border-border", "bg-background");
    await user.click(resultCard);

    await waitFor(() =>
      expect(readRequests(fetchMock)).toContain("knowledge/concepts/gwiki.md"),
    );
    expect(
      screen.queryByRole("dialog", { name: /quick open/i }),
    ).not.toBeInTheDocument();
  });

  it("closes quick-open on Escape without navigating", async () => {
    const fetchMock = stubBrowseFetch();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    const tree = await screen.findByRole("tree", { name: /wiki pages/i });
    within(tree)
      .getByRole("treeitem", { name: /knowledge/i })
      .focus();
    await user.keyboard("{Meta>}k{/Meta}");
    await screen.findByRole("dialog", { name: /quick open/i });
    await user.keyboard("{Escape}");

    expect(
      screen.queryByRole("dialog", { name: /quick open/i }),
    ).not.toBeInTheDocument();
    expect(readRequests(fetchMock)).toHaveLength(0);
  });

  it("retraces history with back and forward", async () => {
    stubBrowseFetch();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    await expandToConcepts(user);
    await openPageFromTree(user, /^gobby$/i);
    await screen.findByRole("heading", { name: "Gobby", level: 1 });
    await openPageFromTree(user, /^gwiki$/i);
    await screen.findByRole("heading", { name: "Gwiki", level: 1 });

    await user.click(screen.getByRole("button", { name: /^back$/i }));
    expect(
      await screen.findByRole("heading", { name: "Gobby", level: 1 }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^forward$/i }));
    expect(
      await screen.findByRole("heading", { name: "Gwiki", level: 1 }),
    ).toBeInTheDocument();
  });

  it("blocks history transitions while a dirty guard declines", async () => {
    stubBrowseFetch();
    const user = userEvent.setup();
    const confirmLeave = vi.fn(async () => false);
    const guard: DirtyGuard = { isDirty: () => false, confirmLeave };
    const guardValue = makeGuardValue([guard]);
    render(
      <DirtyGuardProvider value={guardValue}>
        <WikiTab projectId="p1" />
      </DirtyGuardProvider>,
    );

    await expandToConcepts(user);
    await openPageFromTree(user, /^gobby$/i);
    await screen.findByRole("heading", { name: "Gobby", level: 1 });
    await openPageFromTree(user, /^gwiki$/i);
    await screen.findByRole("heading", { name: "Gwiki", level: 1 });

    guard.isDirty = () => true;
    await user.click(screen.getByRole("button", { name: /^back$/i }));
    expect(confirmLeave).toHaveBeenCalledTimes(1);
    expect(
      screen.getByRole("heading", { name: "Gwiki", level: 1 }),
    ).toBeInTheDocument();
  });
});
