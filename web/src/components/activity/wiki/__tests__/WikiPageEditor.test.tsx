/**
 * §3.2 editing/create/delete acceptance (3.2.1–3.2.3): edit toggle with draft
 * state, dirty guard, Cmd+S, and save-await-reindex; create validation, seeded
 * frontmatter, and inline 409; destructive delete with history-back; code
 * pages read-only. The 3.2.4 conflict/revision contract lives in the sibling
 * WikiPageEditor.conflict suite.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { KeyboardEvent, ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WikiTab } from "../../WikiTab";
import { DirtyGuardProvider } from "../../DirtyGuardContext";
import { useDirtyGuardController } from "../../dirtyGuard";
import {
  alreadyExistsBody,
  backlinksEnvelope,
  browseGraphEnvelope,
  browseReadCodeEnvelope,
  browseReadGobbyEnvelope,
  browseReadGwikiEnvelope,
  healthEnvelope,
  notFoundReadEnvelope,
  pagesEnvelope,
  sourcesEnvelope,
  statusEnvelope,
  writeSuccessEnvelope,
} from "./fixtures";

const confirmMock = vi.hoisted(() => vi.fn(async (_opts: unknown) => true));

vi.mock("../../../../hooks/useConfirmDialog", () => ({
  useConfirmDialog: () => ({ confirm: confirmMock, ConfirmDialogElement: null }),
}));

vi.mock("../../../shared/CodeMirrorEditor", () => ({
  CodeMirrorEditor: ({
    content,
    onChange,
    onSave,
    ariaLabel,
  }: {
    content: string;
    onChange?: (content: string) => void;
    onSave?: () => void;
    ariaLabel?: string;
  }) => (
    <textarea
      aria-label={ariaLabel ?? "Editor"}
      value={content}
      onChange={(event) => onChange?.(event.target.value)}
      onKeyDown={(event: KeyboardEvent<HTMLTextAreaElement>) => {
        if ((event.metaKey || event.ctrlKey) && event.key === "s") {
          event.preventDefault();
          onSave?.();
        }
      }}
    />
  ),
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

interface WikiFetchStub {
  fetchMock: ReturnType<typeof vi.fn>;
  /** Mutable per-path read overrides, consulted at call time. */
  readByPath: Record<string, () => Response>;
  writes: Array<Record<string, unknown>>;
  deletes: Array<Record<string, unknown>>;
}

function stubWikiFetch(
  options: {
    write?: (body: Record<string, unknown>, index: number) => Response;
  } = {},
): WikiFetchStub {
  const readByPath: Record<string, () => Response> = {};
  const writes: Array<Record<string, unknown>> = [];
  const deletes: Array<Record<string, unknown>> = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const route = url.pathname;
    if (route.includes("/api/wiki/status")) return jsonResponse(statusEnvelope);
    if (route.includes("/api/wiki/health")) return jsonResponse(healthEnvelope);
    if (route.includes("/api/wiki/sources")) return jsonResponse(sourcesEnvelope);
    if (route.includes("/api/wiki/pages")) return jsonResponse(pagesEnvelope);
    if (route.includes("/api/wiki/graph")) return jsonResponse(browseGraphEnvelope);
    if (route.includes("/api/wiki/backlinks")) return jsonResponse(backlinksEnvelope);
    if (route.includes("/api/wiki/write")) {
      const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
      writes.push(body);
      return options.write?.(body, writes.length - 1) ?? jsonResponse(writeSuccessEnvelope);
    }
    if (route.includes("/api/wiki/delete")) {
      const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
      deletes.push(body);
      return jsonResponse({
        ok: true,
        command: "page-delete",
        stderr: "",
        payload: { path: body.path, deleted: true },
      });
    }
    if (route.includes("/api/wiki/read")) {
      const path = url.searchParams.get("path") ?? "";
      const override = readByPath[path];
      if (override) return override();
      if (path === "knowledge/concepts/gwiki.md") return jsonResponse(browseReadGwikiEnvelope);
      if (path === "code/_architecture.md") return jsonResponse(browseReadCodeEnvelope);
      return jsonResponse(browseReadGobbyEnvelope);
    }
    return jsonResponse({ ok: true, payload: {} });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, readByPath, writes, deletes };
}

function readRequests(fetchMock: ReturnType<typeof vi.fn>): string[] {
  return fetchMock.mock.calls
    .map((call) => String(call[0]))
    .filter((url) => url.includes("/api/wiki/read"))
    .map((url) => new URL(url, "http://localhost").searchParams.get("path") ?? "");
}

function pagesRequestCount(fetchMock: ReturnType<typeof vi.fn>): number {
  return fetchMock.mock.calls.filter((call) => String(call[0]).includes("/api/wiki/pages")).length;
}

function WikiHarness() {
  const guard = useDirtyGuardController();
  return (
    <DirtyGuardProvider value={guard}>
      <WikiTab projectId="p1" />
    </DirtyGuardProvider>
  );
}

function renderWiki() {
  return render(<WikiHarness />);
}

async function openGobbyPage(user: ReturnType<typeof userEvent.setup>) {
  const tree = await screen.findByRole("tree", { name: /wiki pages/i });
  await user.click(within(tree).getByRole("treeitem", { name: /knowledge/i }));
  await user.click(await within(tree).findByRole("treeitem", { name: /concepts/i }));
  await user.click(await within(tree).findByRole("treeitem", { name: "Gobby" }));
  await screen.findByRole("heading", { name: "Gobby", level: 1 });
  return tree;
}

async function openEditor(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Edit" }));
  return await screen.findByRole("textbox", { name: "Page editor" });
}

async function openCreateForm(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Page actions" }));
  await user.click(await screen.findByRole("menuitem", { name: "New page" }));
  return await screen.findByRole("textbox", { name: /page path/i });
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", MockResizeObserver);
  vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);
  confirmMock.mockClear();
  confirmMock.mockImplementation(async () => true);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.localStorage.clear();
  window.sessionStorage.clear();
});

describe("WikiPageEditor edit toggle (3.2.1)", () => {
  it("opens the raw page in the editor and saves with the base hash via Cmd+S", async () => {
    const stub = stubWikiFetch();
    const user = userEvent.setup();
    renderWiki();

    await openGobbyPage(user);
    const editor = await openEditor(user);
    const rawContent = String(browseReadGobbyEnvelope.payload.content);
    expect(editor).toHaveValue(rawContent);
    expect(screen.queryByText(/unsaved/i)).not.toBeInTheDocument();

    await user.type(editor, " changed");
    expect(await screen.findByText(/unsaved/i)).toBeInTheDocument();

    const pagesBefore = pagesRequestCount(stub.fetchMock);
    await user.keyboard("{Meta>}s{/Meta}");

    await waitFor(() => expect(stub.writes).toHaveLength(1));
    expect(stub.writes[0]).toMatchObject({
      path: "knowledge/concepts/gobby.md",
      mode: "upsert",
      expected_hash: browseReadGobbyEnvelope.payload.content_hash,
    });
    expect(String(stub.writes[0]?.content)).toContain(" changed");

    // Save awaits the reindex-backed write, exits edit mode, and refetches.
    await waitFor(() =>
      expect(screen.queryByRole("textbox", { name: "Page editor" })).not.toBeInTheDocument(),
    );
    await screen.findByRole("heading", { name: "Gobby", level: 1 });
    await waitFor(() => expect(pagesRequestCount(stub.fetchMock)).toBeGreaterThan(pagesBefore));
  });

  it("discards edits back to the base content and closes without writing", async () => {
    const stub = stubWikiFetch();
    const user = userEvent.setup();
    renderWiki();

    await openGobbyPage(user);
    const editor = await openEditor(user);
    await user.type(editor, " scratch");

    await user.click(await screen.findByRole("button", { name: "Discard" }));
    expect(editor).toHaveValue(String(browseReadGobbyEnvelope.payload.content));
    expect(screen.queryByText(/unsaved/i)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Close" }));
    await screen.findByRole("heading", { name: "Gobby", level: 1 });
    expect(stub.writes).toHaveLength(0);
  });

  it("guards local navigation while the draft is dirty", async () => {
    stubWikiFetch();
    const user = userEvent.setup();
    renderWiki();

    const tree = await openGobbyPage(user);
    const editor = await openEditor(user);
    await user.type(editor, " draft");

    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    await user.click(within(tree).getByRole("treeitem", { name: "Gwiki" }));
    expect(confirmSpy).toHaveBeenCalled();
    expect(screen.getByRole("textbox", { name: "Page editor" })).toBeInTheDocument();

    confirmSpy.mockReturnValue(true);
    await user.click(within(tree).getByRole("treeitem", { name: "Gwiki" }));
    await screen.findByRole("heading", { name: "Gwiki", level: 1 });
    expect(screen.queryByRole("textbox", { name: "Page editor" })).not.toBeInTheDocument();
  });
});

describe("WikiPageEditor create flow (3.2.2)", () => {
  it("seeds a create form from the page kebab and navigates after a successful create", async () => {
    const stub = stubWikiFetch({
      write: (body) =>
        jsonResponse({
          ...writeSuccessEnvelope,
          payload: {
            ...writeSuccessEnvelope.payload,
            path: body.path,
            created: true,
          },
        }),
    });
    const user = userEvent.setup();
    renderWiki();

    await openGobbyPage(user);
    const pathInput = await openCreateForm(user);
    expect(pathInput).toHaveValue("knowledge/concepts/");

    const editor = screen.getByRole("textbox", { name: "Page editor" });
    const seeded = (editor as HTMLTextAreaElement).value;
    expect(seeded).toContain("title:");
    expect(seeded).toContain("tags: []");

    await user.type(pathInput, "new-idea.md");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(stub.writes).toHaveLength(1));
    expect(stub.writes[0]).toMatchObject({
      path: "knowledge/concepts/new-idea.md",
      mode: "create",
    });
    expect(stub.writes[0]).not.toHaveProperty("expected_hash");

    await waitFor(() =>
      expect(readRequests(stub.fetchMock)).toContain("knowledge/concepts/new-idea.md"),
    );
    expect(screen.queryByRole("textbox", { name: /page path/i })).not.toBeInTheDocument();
  });

  it("validates the path inline and blocks the write until it resolves under knowledge/", async () => {
    const stub = stubWikiFetch();
    const user = userEvent.setup();
    renderWiki();

    await openGobbyPage(user);
    const pathInput = await openCreateForm(user);

    await user.clear(pathInput);
    await user.type(pathInput, "knowledge/Bad Path.md");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/knowledge\//i);
    expect(stub.writes).toHaveLength(0);

    await user.clear(pathInput);
    await user.type(pathInput, "notes/elsewhere.md");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/knowledge\//i);
    expect(stub.writes).toHaveLength(0);
  });

  it("surfaces an already-exists conflict inline and keeps the form open", async () => {
    const stub = stubWikiFetch({
      write: () => jsonResponse(alreadyExistsBody, 409),
    });
    const user = userEvent.setup();
    renderWiki();

    await openGobbyPage(user);
    const pathInput = await openCreateForm(user);
    await user.clear(pathInput);
    await user.type(pathInput, "knowledge/concepts/gobby.md");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(stub.writes).toHaveLength(1));
    expect(await screen.findByText(/already exists/i)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /page path/i })).toBeInTheDocument();
  });

  it("seeds the folder prefix from the tree's New page here action", async () => {
    stubWikiFetch();
    const user = userEvent.setup();
    renderWiki();

    const tree = await screen.findByRole("tree", { name: /wiki pages/i });
    await user.click(within(tree).getByRole("treeitem", { name: /knowledge/i }));
    await within(tree).findByRole("treeitem", { name: /concepts/i });

    await user.click(screen.getByRole("button", { name: "Actions for concepts" }));
    await user.click(await screen.findByRole("menuitem", { name: "New page here" }));

    expect(await screen.findByRole("textbox", { name: /page path/i })).toHaveValue(
      "knowledge/concepts/",
    );
  });

  it("offers page creation from a not-found read seeded with the missing path", async () => {
    const stub = stubWikiFetch();
    stub.readByPath["knowledge/concepts/gwiki.md"] = () => jsonResponse(notFoundReadEnvelope);
    const user = userEvent.setup();
    renderWiki();

    const tree = await screen.findByRole("tree", { name: /wiki pages/i });
    await user.click(within(tree).getByRole("treeitem", { name: /knowledge/i }));
    await user.click(await within(tree).findByRole("treeitem", { name: /concepts/i }));
    await user.click(await within(tree).findByRole("treeitem", { name: "Gwiki" }));

    await screen.findByText(/has not been created yet/i);
    await user.click(screen.getByRole("button", { name: "Create this page" }));

    expect(await screen.findByRole("textbox", { name: /page path/i })).toHaveValue(
      "knowledge/concepts/gwiki.md",
    );
  });
});

describe("WikiPageEditor delete and read-only pages (3.2.3)", () => {
  it("deletes after a destructive confirmation and navigates back in history", async () => {
    const stub = stubWikiFetch();
    const user = userEvent.setup();
    renderWiki();

    const tree = await openGobbyPage(user);
    await user.click(within(tree).getByRole("treeitem", { name: "Gwiki" }));
    await screen.findByRole("heading", { name: "Gwiki", level: 1 });

    await user.click(screen.getByRole("button", { name: "Page actions" }));
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));

    expect(confirmMock).toHaveBeenCalledWith(expect.objectContaining({ destructive: true }));
    await waitFor(() =>
      expect(stub.deletes).toEqual([{ path: "knowledge/concepts/gwiki.md" }]),
    );
    await screen.findByRole("heading", { name: "Gobby", level: 1 });
  });

  it("does not delete when the confirmation is declined", async () => {
    const stub = stubWikiFetch();
    confirmMock.mockImplementation(async () => false);
    const user = userEvent.setup();
    renderWiki();

    await openGobbyPage(user);
    await user.click(screen.getByRole("button", { name: "Page actions" }));
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));

    await waitFor(() => expect(confirmMock).toHaveBeenCalled());
    expect(stub.deletes).toHaveLength(0);
    expect(screen.getByRole("heading", { name: "Gobby", level: 1 })).toBeInTheDocument();
  });

  // code pages read-only: no edit, delete, or create affordances for code/**.
  it("renders code pages read-only", async () => {
    stubWikiFetch();
    const user = userEvent.setup();
    renderWiki();

    await screen.findByRole("tree", { name: /wiki pages/i });
    await user.click(screen.getByRole("radio", { name: "Code" }));

    const tree = await screen.findByRole("tree", { name: /wiki pages/i });
    await user.click(await within(tree).findByRole("treeitem", { name: /^code$/i }));
    await user.click(await within(tree).findByRole("treeitem", { name: "Architecture Overview" }));
    await screen.findByRole("heading", { name: "Architecture Overview", level: 1 });

    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Page actions" }));
    const menu = await screen.findByRole("menu", { name: "Page actions" });
    expect(within(menu).queryByRole("menuitem", { name: "Delete" })).not.toBeInTheDocument();
    expect(within(menu).queryByRole("menuitem", { name: "New page" })).not.toBeInTheDocument();
    expect(within(menu).getByRole("menuitem", { name: "Copy path" })).toBeInTheDocument();
  });
});
