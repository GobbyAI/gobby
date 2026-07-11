/**
 * §3.2.4 revision contract: concurrent modification surfaces the
 * reload/overwrite conflict flow and silent overwrite is impossible. The
 * editor revalidates the base content hash on window focus and immediately
 * before save; a mismatch (or a 412 from the write route) opens the conflict
 * panel with Reload / Overwrite / Keep editing.
 */

import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { KeyboardEvent, ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WikiTab } from "../../WikiTab";
import { DirtyGuardProvider } from "../../DirtyGuardContext";
import { useDirtyGuardController } from "../../dirtyGuard";
import {
  backlinksEnvelope,
  browseGraphEnvelope,
  browseReadGobbyChangedEnvelope,
  browseReadGobbyEnvelope,
  browseReadGwikiEnvelope,
  healthEnvelope,
  pagesEnvelope,
  sourcesEnvelope,
  statusEnvelope,
  writeConflictBody,
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
}

function stubWikiFetch(
  options: {
    write?: (body: Record<string, unknown>, index: number) => Response;
  } = {},
): WikiFetchStub {
  const readByPath: Record<string, () => Response> = {};
  const writes: Array<Record<string, unknown>> = [];
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
    if (route.includes("/api/wiki/read")) {
      const path = url.searchParams.get("path") ?? "";
      const override = readByPath[path];
      if (override) return override();
      if (path === "knowledge/concepts/gwiki.md") return jsonResponse(browseReadGwikiEnvelope);
      return jsonResponse(browseReadGobbyEnvelope);
    }
    return jsonResponse({ ok: true, payload: {} });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, readByPath, writes };
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

async function openDirtyGobbyEditor(user: ReturnType<typeof userEvent.setup>) {
  const tree = await screen.findByRole("tree", { name: /wiki pages/i });
  await user.click(within(tree).getByRole("treeitem", { name: /knowledge/i }));
  await user.click(await within(tree).findByRole("treeitem", { name: /concepts/i }));
  await user.click(await within(tree).findByRole("treeitem", { name: "Gobby" }));
  await screen.findByRole("heading", { name: "Gobby", level: 1 });
  await user.click(await screen.findByRole("button", { name: "Edit" }));
  const editor = await screen.findByRole("textbox", { name: "Page editor" });
  await user.type(editor, " local-draft");
  await screen.findByText(/unsaved/i);
  return editor;
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

describe("WikiPageEditor revision contract (3.2.4)", () => {
  it("pre-save revalidation catches a server change and never writes silently", async () => {
    const stub = stubWikiFetch();
    const user = userEvent.setup();
    renderWiki();

    const editor = await openDirtyGobbyEditor(user);
    stub.readByPath["knowledge/concepts/gobby.md"] = () =>
      jsonResponse(browseReadGobbyChangedEnvelope);

    await user.click(screen.getByRole("button", { name: "Save" }));

    await screen.findByText(/changed on disk/i);
    expect(stub.writes).toHaveLength(0);
    expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Overwrite" })).toBeInTheDocument();

    // Keep editing dismisses the panel and preserves the local draft.
    await user.click(screen.getByRole("button", { name: "Keep editing" }));
    await waitFor(() => expect(screen.queryByText(/changed on disk/i)).not.toBeInTheDocument());
    expect((editor as HTMLTextAreaElement).value).toContain(" local-draft");
    expect(screen.getByText(/unsaved/i)).toBeInTheDocument();
  });

  it("a 412 write conflict opens the panel and Reload adopts the server content", async () => {
    const stub = stubWikiFetch({
      write: () => jsonResponse(writeConflictBody, 412),
    });
    const user = userEvent.setup();
    renderWiki();

    const editor = await openDirtyGobbyEditor(user);
    await user.click(screen.getByRole("button", { name: "Save" }));

    await screen.findByText(/changed on disk/i);
    expect(stub.writes).toHaveLength(1);

    stub.readByPath["knowledge/concepts/gobby.md"] = () =>
      jsonResponse(browseReadGobbyChangedEnvelope);
    await user.click(screen.getByRole("button", { name: "Reload" }));

    await waitFor(() =>
      expect((editor as HTMLTextAreaElement).value).toBe(
        String(browseReadGobbyChangedEnvelope.payload.content),
      ),
    );
    expect(screen.queryByText(/changed on disk/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/unsaved/i)).not.toBeInTheDocument();
    expect(stub.writes).toHaveLength(1);
  });

  it("Overwrite requires a destructive confirmation and resaves against the fresh hash", async () => {
    const stub = stubWikiFetch({
      write: (_body, index) =>
        index === 0 ? jsonResponse(writeConflictBody, 412) : jsonResponse(writeSuccessEnvelope),
    });
    const user = userEvent.setup();
    renderWiki();

    await openDirtyGobbyEditor(user);
    await user.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText(/changed on disk/i);

    stub.readByPath["knowledge/concepts/gobby.md"] = () =>
      jsonResponse(browseReadGobbyChangedEnvelope);
    await user.click(screen.getByRole("button", { name: "Overwrite" }));

    expect(confirmMock).toHaveBeenCalledWith(expect.objectContaining({ destructive: true }));
    await waitFor(() => expect(stub.writes).toHaveLength(2));
    expect(stub.writes[1]).toMatchObject({
      path: "knowledge/concepts/gobby.md",
      expected_hash: browseReadGobbyChangedEnvelope.payload.content_hash,
    });
    expect(String(stub.writes[1]?.content)).toContain(" local-draft");

    // Successful overwrite exits edit mode back to the reader.
    await waitFor(() =>
      expect(screen.queryByRole("textbox", { name: "Page editor" })).not.toBeInTheDocument(),
    );
  });

  it("declining the overwrite confirmation keeps the conflict panel and draft", async () => {
    const stub = stubWikiFetch({
      write: () => jsonResponse(writeConflictBody, 412),
    });
    confirmMock.mockImplementation(async () => false);
    const user = userEvent.setup();
    renderWiki();

    const editor = await openDirtyGobbyEditor(user);
    await user.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText(/changed on disk/i);

    await user.click(screen.getByRole("button", { name: "Overwrite" }));
    await waitFor(() => expect(confirmMock).toHaveBeenCalled());

    expect(stub.writes).toHaveLength(1);
    expect(screen.getByText(/changed on disk/i)).toBeInTheDocument();
    expect((editor as HTMLTextAreaElement).value).toContain(" local-draft");
  });

  it("window focus revalidation flags server changes while editing", async () => {
    const stub = stubWikiFetch();
    const user = userEvent.setup();
    renderWiki();

    const editor = await openDirtyGobbyEditor(user);
    expect(screen.queryByText(/changed on server/i)).not.toBeInTheDocument();

    stub.readByPath["knowledge/concepts/gobby.md"] = () =>
      jsonResponse(browseReadGobbyChangedEnvelope);
    act(() => {
      window.dispatchEvent(new Event("focus"));
    });

    expect(await screen.findByText(/changed on server/i)).toBeInTheDocument();
    expect((editor as HTMLTextAreaElement).value).toContain(" local-draft");
    expect(stub.writes).toHaveLength(0);
  });
});
