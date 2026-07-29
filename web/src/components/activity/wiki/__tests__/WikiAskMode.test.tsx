/**
 * §5.1 ask mode acceptance (5.1.1–5.1.3): the composer runs grounded asks
 * with the extractive/synthesized toggle, staged progress, cancel, and
 * error/retry; every citation chip either navigates to a vault page or is
 * explicitly marked unresolved with a "Search vault" fallback (never a
 * silent dead link); grounding warnings render as the ungrounded-claims
 * callout; history persists to sessionStorage with restore/rerun/delete.
 */

import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WikiTab } from "../../WikiTab";
import {
  askRetrievalEnvelope,
  askSynthesisEnvelope,
  backlinksEnvelope,
  browseGraphEnvelope,
  browseReadGobbyEnvelope,
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

type AskHandler = (url: URL, init?: RequestInit) => Response | Promise<Response>;

/** Default ask routing mirrors gwiki: llm=true → synthesis, else retrieval. */
function stubAskFetch(overrides: { ask?: AskHandler; status?: Response } = {}) {
  const defaultAsk: AskHandler = (url) =>
    jsonResponse(
      url.searchParams.get("llm") === "true" ? askSynthesisEnvelope : askRetrievalEnvelope,
    );
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const route = url.pathname;
    if (route.includes("/api/wiki/ask")) return (overrides.ask ?? defaultAsk)(url, init);
    if (route.includes("/api/wiki/status")) {
      return overrides.status ?? jsonResponse(statusEnvelope);
    }
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

function askCalls(fetchMock: ReturnType<typeof stubAskFetch>): URL[] {
  return fetchMock.mock.calls
    .map((call) => new URL(String(call[0]), "http://localhost"))
    .filter((url) => url.pathname.includes("/api/wiki/ask"));
}

function seedAskMode() {
  window.localStorage.setItem("gobby:wiki-tab:mode", "ask");
}

const ASK_HISTORY_KEY = "gobby:wiki-tab:ask-history";

function storedHistory(): Array<{ question: string }> {
  return JSON.parse(window.sessionStorage.getItem(ASK_HISTORY_KEY) ?? "[]") as Array<{
    question: string;
  }>;
}

/** Pre-normalized envelope, as WikiAskMode persists it after a live ask. */
function historyEntry(question: string, overrides: Record<string, unknown> = {}) {
  return {
    id: `seed-${question.replace(/\s+/g, "-")}`,
    question,
    llm: true,
    ts: Date.now() - 5 * 60_000,
    envelope: {
      status: "answered",
      degraded: false,
      degradedSources: [],
      answer: `Stored answer for ${question} citing [[knowledge/concepts/gobby|Gobby]].`,
      model: null,
      citations: [{ target: "knowledge/concepts/gobby", title: "Gobby", resolvedPath: null }],
      groundingWarnings: [],
      hits: [],
      codeCitations: [],
      warnings: [],
      hint: null,
      aiStatus: null,
      aiError: null,
    },
    ...overrides,
  };
}

function seedHistory(entries: unknown[]) {
  window.sessionStorage.setItem(ASK_HISTORY_KEY, JSON.stringify(entries));
}

function deferredResponse() {
  let resolve!: (value: Response) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<Response>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

async function composerInput() {
  return await screen.findByRole("textbox", { name: /ask the wiki/i });
}

async function submitQuestion(user: ReturnType<typeof userEvent.setup>, text: string) {
  const composer = await composerInput();
  // The composer starts disabled until the first status fetch lands.
  await waitFor(() => expect(composer).toBeEnabled());
  await user.type(composer, `${text}{Enter}`);
  return composer;
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

describe("ask flow (5.1.1)", () => {
  it("runs an extractive ask and renders the retrieved hits", async () => {
    const fetchMock = stubAskFetch();
    seedAskMode();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    const composer = await submitQuestion(user, "how does the wiki watcher work");

    await screen.findByText("Session: c1c0c073");
    const calls = askCalls(fetchMock);
    expect(calls).toHaveLength(1);
    expect(calls[0]?.searchParams.get("query")).toBe("how does the wiki watcher work");
    expect(calls[0]?.searchParams.get("llm")).toBeNull();
    expect(composer).toBeEnabled();
  });

  it("runs a synthesized ask and renders the markdown answer", async () => {
    const fetchMock = stubAskFetch();
    seedAskMode();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    await user.click(await screen.findByRole("radio", { name: "Synthesized" }));
    await submitQuestion(user, "how does the wiki watcher work");

    await screen.findByText(/The watcher polls/);
    expect(askCalls(fetchMock)[0]?.searchParams.get("llm")).toBe("true");
  });

  it("surfaces envelope errors inline and retries", async () => {
    let attempts = 0;
    const fetchMock = stubAskFetch({
      ask: () => {
        attempts += 1;
        return attempts === 1
          ? jsonResponse({ detail: "gwiki ask timed out (timeout)" }, 500)
          : jsonResponse(askRetrievalEnvelope);
      },
    });
    seedAskMode();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    await submitQuestion(user, "slow question");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/timed out/);
    await user.click(screen.getByRole("button", { name: "Retry" }));

    await screen.findByText("Session: c1c0c073");
    expect(askCalls(fetchMock)).toHaveLength(2);
  });
});

describe("staged progress and cancel (5.1.1)", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("stages the hint, counts elapsed time, disables the composer, and cancels", async () => {
    const pendingAsk = deferredResponse();
    stubAskFetch({
      ask: (_url, init) => {
        init?.signal?.addEventListener("abort", () =>
          pendingAsk.reject(new DOMException("Aborted", "AbortError")),
        );
        return pendingAsk.promise;
      },
    });
    seedAskMode();
    const user = userEvent.setup({ advanceTimers: (ms) => vi.advanceTimersByTime(ms) });
    render(<WikiTab projectId="p1" />);

    await user.click(await screen.findByRole("radio", { name: "Synthesized" }));
    const composer = await submitQuestion(user, "long synthesis");

    const progress = await screen.findByRole("status", { name: /ask progress/i });
    expect(progress).toHaveTextContent(/Searching vault…/);
    expect(progress).toHaveTextContent(/can take a few minutes/);
    expect(composer).toBeDisabled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(9_000);
    });
    await waitFor(() => expect(progress).toHaveTextContent(/Synthesizing…/));
    expect(progress).toHaveTextContent(/0:0\d|0:1\d/);

    const cancel = screen.getByRole("button", { name: "Cancel" });
    expect(cancel).toHaveAttribute("title", expect.stringMatching(/server may keep working/i));
    await user.click(cancel);

    await waitFor(() => expect(composer).toBeEnabled());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("citations (5.1.2)", () => {
  it("navigates to the vault page for a resolved citation chip", async () => {
    stubAskFetch();
    seedAskMode();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    await user.click(await screen.findByRole("radio", { name: "Synthesized" }));
    await submitQuestion(user, "how does the wiki watcher work");

    const citations = await screen.findByRole("list", { name: /citations/i });
    await user.click(within(citations).getByRole("button", { name: "Gobby" }));

    await screen.findByRole("heading", { name: "Gobby" });
    expect(window.localStorage.getItem("gobby:wiki-tab:mode")).toBe("wiki");
  });

  it("marks unresolved citations and falls back to vault search", async () => {
    stubAskFetch();
    seedAskMode();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    await user.click(await screen.findByRole("radio", { name: "Synthesized" }));
    await submitQuestion(user, "how does the wiki watcher work");

    const citations = await screen.findByRole("list", { name: /citations/i });
    const broken = within(citations).getByText(/watcher\.py/);
    expect(broken.closest("li")).toHaveTextContent(/unresolved/i);

    await user.click(
      within(citations).getByRole("button", { name: /search vault for watcher\.py/i }),
    );

    const search = await screen.findByRole("searchbox", { name: /filter wiki/i });
    expect(search).toHaveValue("code/files/src/gobby/wiki/watcher.py");
    expect(window.localStorage.getItem("gobby:wiki-tab:mode")).toBe("wiki");
  });
});

describe("grounding warnings (5.1.3)", () => {
  it("renders the ungrounded-claims callout when warnings are present", async () => {
    stubAskFetch();
    seedAskMode();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    await user.click(await screen.findByRole("radio", { name: "Synthesized" }));
    await submitQuestion(user, "how does the wiki watcher work");

    const callout = await screen.findByRole("status", { name: /grounding warnings/i });
    expect(callout).toHaveTextContent(
      "Unsupported claim: The watcher restarts the daemon on every write.",
    );
    expect(callout).toHaveTextContent("semantic search degraded");
  });

  it("renders no callout when the envelope carries no warnings", async () => {
    stubAskFetch();
    seedAskMode();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    await submitQuestion(user, "how does the wiki watcher work");

    await screen.findByText("Session: c1c0c073");
    expect(screen.queryByRole("status", { name: /grounding warnings/i })).not.toBeInTheDocument();
  });
});

describe("history", () => {
  it("persists asks to sessionStorage newest-first", async () => {
    stubAskFetch();
    seedAskMode();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    await submitQuestion(user, "first question");
    await screen.findByText("Session: c1c0c073");

    await waitFor(() => expect(storedHistory()).toHaveLength(1));
    expect(storedHistory()[0]?.question).toBe("first question");
  });

  it("restores a stored entry on click with mode chip and age", async () => {
    seedHistory([historyEntry("earlier question")]);
    stubAskFetch();
    seedAskMode();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    const history = await screen.findByRole("list", { name: /ask history/i });
    const row = within(history).getByRole("button", { name: /^earlier question/ });
    expect(within(history).getByText("Synthesized")).toBeInTheDocument();
    expect(within(history).getByText("5m")).toBeInTheDocument();

    await user.click(row);
    await screen.findByText(/Stored answer for earlier question/);
  });

  it("reruns a stored entry through its kebab", async () => {
    seedHistory([historyEntry("earlier question")]);
    const fetchMock = stubAskFetch();
    seedAskMode();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    const history = await screen.findByRole("list", { name: /ask history/i });
    await user.click(
      within(history).getByRole("button", { name: /actions for earlier question/i }),
    );
    await user.click(await screen.findByRole("menuitem", { name: "Rerun" }));

    await screen.findByText(/The watcher polls/);
    const calls = askCalls(fetchMock);
    expect(calls).toHaveLength(1);
    expect(calls[0]?.searchParams.get("query")).toBe("earlier question");
    expect(calls[0]?.searchParams.get("llm")).toBe("true");
  });

  it("deletes a stored entry through its kebab", async () => {
    seedHistory([historyEntry("keep me"), historyEntry("drop me")]);
    stubAskFetch();
    seedAskMode();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    const history = await screen.findByRole("list", { name: /ask history/i });
    await user.click(within(history).getByRole("button", { name: /actions for drop me/i }));
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));

    await waitFor(() =>
      expect(within(history).queryByRole("button", { name: /drop me/ })).not.toBeInTheDocument(),
    );
    expect(storedHistory().map((entry) => entry.question)).toEqual(["keep me"]);
  });

  it("caps history at 20 entries", async () => {
    seedHistory(Array.from({ length: 20 }, (_, index) => historyEntry(`question ${index}`)));
    stubAskFetch();
    seedAskMode();
    const user = userEvent.setup();
    render(<WikiTab projectId="p1" />);

    await submitQuestion(user, "the twenty-first question");
    await screen.findByText("Session: c1c0c073");

    await waitFor(() => {
      const stored = storedHistory();
      expect(stored).toHaveLength(20);
      expect(stored[0]?.question).toBe("the twenty-first question");
    });
  });
});

describe("gateway down", () => {
  it("disables the composer with an info banner", async () => {
    stubAskFetch({ status: jsonResponse({ detail: "daemon offline" }, 500) });
    seedAskMode();
    render(<WikiTab projectId="p1" />);

    await screen.findByText(/gateway is unreachable/i);
    expect(await composerInput()).toBeDisabled();
  });
});
