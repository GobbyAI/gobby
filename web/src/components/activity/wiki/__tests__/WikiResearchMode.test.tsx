/**
 * §5.2 research mode acceptance (5.2.1–5.2.3): the composer launches
 * wiki-research pipeline runs detached (202) with the Options disclosure
 * feeding the pipeline inputs; a live run streams per-step progress with the
 * Pipelines-tab escape hatch; the single-flight guard disables the composer
 * while a run is live; completion flips the success strip whose report and
 * topic shortcuts open in the shared reader; past runs merge executions
 * history with `*-run-report.md` vault outputs; report wikilinks navigate to
 * topic pages with the mode auto-flip.
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
  type EnvelopeFixture,
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

// ── Fixtures ────────────────────────────────────────────────────

const REPORT_PATH = "outputs/research/2026-07-09-contract-guardrails-run-report.md";

/** pagesEnvelope plus a research run report in the outputs listing. */
const researchPagesEnvelope: EnvelopeFixture = {
  ...pagesEnvelope,
  payload: {
    ...pagesEnvelope.payload,
    outputs: [
      ...(pagesEnvelope.payload.outputs as unknown[]),
      { path: REPORT_PATH, size: 2048, modified: "2026-07-10T18:00:00+00:00" },
    ],
  },
};

const reportReadEnvelope: EnvelopeFixture = {
  ok: true,
  command: "read",
  stderr: "",
  payload: {
    command: "read",
    status: "found",
    requested: { kind: "path", value: REPORT_PATH },
    wiki_path: REPORT_PATH,
    title: "Contract guardrails — run report",
    content:
      "# Contract guardrails — run report\n\nCompiled topic: [[knowledge/topics/contract-guardrails|Contract guardrails]].\n\nBudgets respected.\n",
    content_format: "markdown",
    content_hash: "report-hash",
    byte_len: 140,
    truncated: false,
    candidates: [],
    degradations: [],
  },
};

const topicReadEnvelope: EnvelopeFixture = {
  ok: true,
  command: "read",
  stderr: "",
  payload: {
    command: "read",
    status: "found",
    requested: { kind: "path", value: "knowledge/topics/contract-guardrails.md" },
    wiki_path: "knowledge/topics/contract-guardrails.md",
    title: "Contract guardrails",
    content: "# Contract guardrails\n\nCompiled topic page.\n",
    content_format: "markdown",
    content_hash: "topic-hash",
    byte_len: 44,
    truncated: false,
    candidates: [],
    degradations: [],
  },
};

const providersResponse = {
  providers: [
    {
      provider: "claude",
      available: true,
      source: "live",
      display_name: "Claude",
      models: [
        {
          canonical_model: "claude-sonnet-5",
          display_name: "Sonnet 5",
          aliases: [],
          available: true,
          hidden: false,
          is_default: false,
          context_length: { value: null, source: "unknown" },
          max_output_tokens: { value: null, source: "unknown" },
          latency_class: null,
          reasoning: {
            status: "unknown",
            supported_efforts: null,
            default_effort: null,
          },
          input_modalities: null,
          supports_tools: null,
          routes: {},
          provenance: {},
        },
      ],
      refresh: { generation: 1, sources: [] },
    },
    {
      provider: "codex",
      available: true,
      source: "live",
      display_name: "Codex",
      models: [
        {
          canonical_model: "gpt-5.1-codex",
          display_name: "GPT-5.1 Codex",
          aliases: [],
          available: true,
          hidden: false,
          is_default: false,
          context_length: { value: null, source: "unknown" },
          max_output_tokens: { value: null, source: "unknown" },
          latency_class: null,
          reasoning: {
            status: "unknown",
            supported_efforts: null,
            default_effort: null,
          },
          input_modalities: null,
          supports_tools: null,
          routes: {},
          provenance: {},
        },
      ],
      refresh: { generation: 1, sources: [] },
    },
  ],
};

interface StepSeed {
  step_id: string;
  status: string;
}

interface ExecutionSeed {
  id: string;
  status: string;
  created_at?: string;
  updated_at?: string;
  completed_at?: string | null;
  inputs?: Record<string, unknown>;
  outputs?: Record<string, unknown> | null;
  steps?: StepSeed[];
}

function execution(seed: ExecutionSeed) {
  const created = seed.created_at ?? new Date(Date.now() - 65_000).toISOString();
  return {
    id: seed.id,
    pipeline_name: "wiki-research",
    project_id: "p-1",
    status: seed.status,
    created_at: created,
    updated_at: seed.updated_at ?? created,
    completed_at: seed.completed_at ?? null,
    inputs_json: JSON.stringify(seed.inputs ?? { question: "How do watchers work?" }),
    outputs_json: seed.outputs ? JSON.stringify(seed.outputs) : null,
    steps: (seed.steps ?? []).map((step, index) => ({
      id: index + 1,
      step_id: step.step_id,
      status: step.status,
      started_at: null,
      completed_at: null,
      output_json: null,
      error: null,
      approval_token: null,
    })),
  };
}

const RUNNING_STEPS: StepSeed[] = [
  { step_id: "reentry_check", status: "completed" },
  { step_id: "create_research_task", status: "completed" },
  { step_id: "spawn_researcher", status: "running" },
  { step_id: "wait_researcher", status: "pending" },
];

// ── Harness ─────────────────────────────────────────────────────

interface StubOptions {
  executions?: () => unknown[];
  status?: Response;
}

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status < 400,
    status,
    json: async () => body,
  } as Response;
}

/** Routes daemon endpoints; `executions` is read per fetch so tests can
 * mutate run state between polls. Launch bodies are captured on the mock. */
function stubResearchFetch(overrides: StubOptions = {}) {
  const runBodies: Array<Record<string, unknown>> = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const route = url.pathname;
    if (route.includes("/api/pipelines/run")) {
      runBodies.push(JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>);
      return jsonResponse({ execution_id: "exec-new", status: "running" }, 202);
    }
    if (route.includes("/api/pipelines/executions")) {
      const executions = overrides.executions?.() ?? [];
      return jsonResponse({
        executions,
        total: executions.length,
        status_summary: {},
      });
    }
    if (route.includes("/api/providers/models")) return jsonResponse(providersResponse);
    if (route.includes("/api/wiki/status")) {
      return overrides.status ?? jsonResponse(statusEnvelope);
    }
    if (route.includes("/api/wiki/health")) return jsonResponse(healthEnvelope);
    if (route.includes("/api/wiki/sources")) return jsonResponse(sourcesEnvelope);
    if (route.includes("/api/wiki/pages")) return jsonResponse(researchPagesEnvelope);
    if (route.includes("/api/wiki/graph")) return jsonResponse(browseGraphEnvelope);
    if (route.includes("/api/wiki/backlinks")) return jsonResponse(backlinksEnvelope);
    if (route.includes("/api/wiki/read")) {
      const path = url.searchParams.get("path") ?? "";
      if (path.endsWith("-run-report.md")) return jsonResponse(reportReadEnvelope);
      if (path === "knowledge/topics/contract-guardrails.md") {
        return jsonResponse(topicReadEnvelope);
      }
      return jsonResponse(browseReadGobbyEnvelope);
    }
    return jsonResponse({ ok: true, payload: {} });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, runBodies };
}

function seedResearchMode() {
  window.localStorage.setItem("gobby:wiki-tab:mode", "research");
}

async function findComposer(): Promise<HTMLElement> {
  const composer = await screen.findByRole("textbox", { name: "Research question" });
  await waitFor(() => expect(composer).toBeEnabled());
  return composer;
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", MockResizeObserver);
  vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);
  window.localStorage.clear();
  window.sessionStorage.clear();
  clearProviderModelCache();
  seedResearchMode();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

// ── Tests ───────────────────────────────────────────────────────

describe("WikiResearchMode launch", () => {
  it("launches a research run with pipeline defaults and shows live progress", async () => {
    const user = userEvent.setup();
    let live: unknown[] = [];
    const { runBodies } = stubResearchFetch({ executions: () => live });
    render(<WikiTab />);

    const composer = await findComposer();
    await user.type(composer, "How do watchers work?");
    live = [execution({ id: "exec-1", status: "running", steps: RUNNING_STEPS })];
    await user.click(screen.getByRole("button", { name: "Run research" }));

    await waitFor(() => expect(runBodies).toHaveLength(1));
    expect(runBodies[0]).toMatchObject({
      name: "wiki-research",
      background: true,
      inputs: {
        question: "How do watchers work?",
        topic_slug: "",
        max_sources: 12,
        max_items: 8,
        create_tasks: "false",
        provider: "claude",
        model: "",
      },
    });

    // The 202 triggers an immediate executions refetch → live run card
    // ("running" appears on the header status and the running step).
    const card = await screen.findByRole("region", { name: "Live research run" });
    expect(within(card).getAllByText(/running/i).length).toBeGreaterThan(0);
  });

  it("sends option overrides from the Options disclosure", async () => {
    const user = userEvent.setup();
    const { runBodies } = stubResearchFetch({ executions: () => [] });
    render(<WikiTab />);

    const composer = await findComposer();
    await user.type(composer, "Audit the dispatch rules");
    await user.click(screen.getByRole("button", { name: "Options" }));

    const topicSlug = screen.getByRole("textbox", { name: "Topic slug" });
    await user.type(topicSlug, "dispatch-rules");
    const maxSources = screen.getByRole("spinbutton", { name: "Max sources" });
    await user.clear(maxSources);
    await user.type(maxSources, "5");
    const maxItems = screen.getByRole("spinbutton", { name: "Max items" });
    await user.clear(maxItems);
    await user.type(maxItems, "3");
    await user.click(screen.getByRole("switch", { name: "Create follow-up tasks" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Provider" }), "codex");
    await user.selectOptions(screen.getByRole("combobox", { name: "Model" }), "gpt-5.1-codex");

    await user.click(screen.getByRole("button", { name: "Run research" }));

    await waitFor(() => expect(runBodies).toHaveLength(1));
    expect(runBodies[0]?.inputs).toMatchObject({
      question: "Audit the dispatch rules",
      topic_slug: "dispatch-rules",
      max_sources: 5,
      max_items: 3,
      create_tasks: "true",
      provider: "codex",
      model: "gpt-5.1-codex",
    });
  });

  it("disables the composer while a run is in progress (single-flight)", async () => {
    stubResearchFetch({
      executions: () => [execution({ id: "exec-1", status: "running", steps: RUNNING_STEPS })],
    });
    render(<WikiTab />);

    expect(await screen.findByText("A research run is in progress")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Research question" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Run research" })).toBeDisabled();
  });

  it("disables the composer when the wiki gateway is down", async () => {
    stubResearchFetch({
      executions: () => [],
      status: jsonResponse({ ok: false, error: "gateway down" }, 500),
    });
    render(<WikiTab />);

    expect(
      await screen.findByText(
        "The wiki gateway is unreachable — the composer is disabled until it recovers.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Research question" })).toBeDisabled();
  });
});

describe("WikiResearchMode live monitoring", () => {
  it("shows per-step progress and the pipelines escape hatch", async () => {
    const user = userEvent.setup();
    stubResearchFetch({
      executions: () => [execution({ id: "exec-1", status: "running", steps: RUNNING_STEPS })],
    });
    render(<WikiTab />);

    const card = await screen.findByRole("region", { name: "Live research run" });
    const steps = within(card).getByRole("list", { name: "Run steps" });
    const items = within(steps).getAllByRole("listitem");
    expect(items.map((item) => item.textContent)).toEqual([
      expect.stringContaining("Create research task"),
      expect.stringContaining("Spawn researcher"),
      expect.stringContaining("Wait for researcher"),
    ]);
    expect(items[0]?.textContent).toMatch(/completed/i);
    expect(items[1]?.textContent).toMatch(/running/i);
    expect(items[2]?.textContent).toMatch(/pending/i);
    expect(within(card).getByText(/elapsed/i)).toBeInTheDocument();

    const events: CustomEvent[] = [];
    const listener = (event: Event) => events.push(event as CustomEvent);
    window.addEventListener("gobby:show-activity-tab", listener);
    try {
      await user.click(within(card).getByRole("button", { name: "View in Pipelines tab" }));
    } finally {
      window.removeEventListener("gobby:show-activity-tab", listener);
    }
    expect(events).toHaveLength(1);
    expect(events[0]?.detail).toEqual({ tab: "pipelines" });
  });
});

describe("WikiResearchMode completion strip", () => {
  const completedExecution = () =>
    execution({
      id: "exec-1",
      status: "completed",
      created_at: "2026-07-09T10:00:00+00:00",
      updated_at: "2026-07-09T10:04:32+00:00",
      completed_at: "2026-07-09T10:04:32+00:00",
      inputs: { question: "How are contracts guarded?", topic_slug: "contract-guardrails" },
      outputs: { status: "success" },
      steps: [
        { step_id: "create_research_task", status: "completed" },
        { step_id: "spawn_researcher", status: "completed" },
        { step_id: "wait_researcher", status: "completed" },
      ],
    });

  it("flips the completion strip and opens the report in the reader", async () => {
    const user = userEvent.setup();
    stubResearchFetch({ executions: () => [completedExecution()] });
    render(<WikiTab />);

    const strip = await screen.findByRole("status", { name: "Research run completed" });
    await user.click(within(strip).getByRole("button", { name: "Open report" }));

    expect(
      await screen.findByRole("heading", { name: "Contract guardrails — run report" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Back to research" }));
    expect(await findComposer()).toBeInTheDocument();
  });

  it("opens the compiled topic page from the completion strip", async () => {
    const user = userEvent.setup();
    stubResearchFetch({ executions: () => [completedExecution()] });
    render(<WikiTab />);

    const strip = await screen.findByRole("status", { name: "Research run completed" });
    await user.click(within(strip).getByRole("button", { name: "Open topic page" }));

    expect(
      await screen.findByRole("heading", { name: "Contract guardrails" }),
    ).toBeInTheDocument();
    expect(window.localStorage.getItem("gobby:wiki-tab:mode")).toBe("wiki");
  });
});

describe("WikiResearchMode past runs", () => {
  it("merges executions history and report outputs into past runs", async () => {
    stubResearchFetch({
      executions: () => [
        execution({
          id: "exec-2",
          status: "failed",
          created_at: "2026-07-08T09:00:00+00:00",
          updated_at: "2026-07-08T09:01:10+00:00",
          completed_at: "2026-07-08T09:01:10+00:00",
          inputs: { question: "Older failed question" },
        }),
        execution({
          id: "exec-1",
          status: "completed",
          created_at: "2026-07-09T10:00:00+00:00",
          updated_at: "2026-07-09T10:04:32+00:00",
          completed_at: "2026-07-09T10:04:32+00:00",
          inputs: { question: "How are contracts guarded?" },
        }),
      ],
    });
    render(<WikiTab />);

    const list = await screen.findByRole("list", { name: "Past research runs" });
    const rows = within(list).getAllByRole("listitem");
    // Report modified 2026-07-10 > exec-1 created 2026-07-09 > exec-2.
    expect(rows[0]?.textContent).toContain("2026-07-09-contract-guardrails-run-report");
    expect(rows[1]?.textContent).toContain("How are contracts guarded?");
    expect(rows[1]?.textContent).toMatch(/completed/i);
    expect(rows[1]?.textContent).toMatch(/4m 32s/);
    expect(rows[2]?.textContent).toContain("Older failed question");
    expect(rows[2]?.textContent).toMatch(/failed/i);
  });

  it("navigates report wikilinks to topic pages with the mode auto-flip", async () => {
    const user = userEvent.setup();
    stubResearchFetch({ executions: () => [] });
    render(<WikiTab />);

    const list = await screen.findByRole("list", { name: "Past research runs" });
    await user.click(
      within(list).getByRole("button", {
        name: /2026-07-09-contract-guardrails-run-report/,
      }),
    );
    expect(
      await screen.findByRole("heading", { name: "Contract guardrails — run report" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "Contract guardrails" }));
    expect(
      await screen.findByRole("heading", { name: "Contract guardrails" }),
    ).toBeInTheDocument();
    expect(window.localStorage.getItem("gobby:wiki-tab:mode")).toBe("wiki");
  });
});
