/**
 * §4.1 graph view acceptance (4.1.1–4.1.4): the 2D force graph takes the
 * panel override with working filters/layers/legend and persisted settings;
 * node click navigates to the page; unresolved nodes render hollow; reduced
 * motion pre-simulates the layout; scene building filters, caps, sizes, and
 * community-colors deterministically.
 */

import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WikiTab } from "../../WikiTab";
import { DirtyGuardProvider } from "../../DirtyGuardContext";
import { useDirtyGuardController } from "../../dirtyGuard";
import {
  buildGraphScene,
  MAX_GRAPH_NODES,
  type WikiGraphSceneNode,
  type WikiGraphSceneOptions,
} from "../WikiGraphScene";
import { normalizeGraph } from "../WikiTabData";
import { wikiNodeVal, type WikiGraphPayload } from "../WikiTabModel";
import {
  backlinksEnvelope,
  browseReadGobbyEnvelope,
  graphViewEnvelope,
  healthEnvelope,
  pagesEnvelope,
  sourcesEnvelope,
  statusEnvelope,
} from "./fixtures";

const GOBBY_ID = "document-knowledge-concepts-gobby-md-aa01";
const GWIKI_ID = "document-knowledge-concepts-gwiki-md-aa02";
const RUNNER_ID = "document-code-files-src-gobby-runner-py-md-aa03";
const WATCHER_ID = "document-code-files-src-gobby-watcher-py-md-aa04";
const SOURCE_ID = "source-src-8218-aa05";
const CITATION_ID = "citation-runner-aa06";
const UNRESOLVED_ID = "unresolved-code-modules-src-gobby-aa07";

interface CapturedGraphProps {
  graphData: { nodes: WikiGraphSceneNode[]; links: Array<Record<string, unknown>> };
  nodeCanvasObject?: (node: WikiGraphSceneNode, ctx: unknown, globalScale: number) => void;
  onNodeClick?: (node: WikiGraphSceneNode) => void;
  onNodeHover?: (node: WikiGraphSceneNode | null) => void;
  onEngineStop?: () => void;
  warmupTicks?: number;
  cooldownTicks?: number;
  autoPauseRedraw?: boolean;
}

const capturedRef = vi.hoisted(() => ({ current: null as CapturedGraphProps | null }));
const fgHandle = vi.hoisted(() => ({
  zoom: vi.fn(() => 1),
  centerAt: vi.fn(() => ({ x: 0, y: 0 })),
  zoomToFit: vi.fn(),
  d3Force: vi.fn(),
}));

vi.mock("react-force-graph-2d", async () => {
  const React = await import("react");
  return {
    default: React.forwardRef(function MockForceGraph2D(
      props: CapturedGraphProps,
      ref: React.Ref<unknown>,
    ) {
      React.useImperativeHandle(ref, () => fgHandle);
      React.useEffect(() => {
        capturedRef.current = props;
      });
      return (
        <div data-testid="force-graph-2d">
          {props.graphData.nodes.map((node) => (
            <button
              type="button"
              key={node.id}
              data-testid={`graph-node-${node.id}`}
              onClick={() => props.onNodeClick?.(node)}
            >
              {node.label}
            </button>
          ))}
        </div>
      );
    }),
  };
});

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

/** Canvas 2D stand-in recording alpha assignments and path calls. */
class MockCanvasCtx {
  alphas: number[] = [];
  font = "";
  fillStyle: unknown = "";
  strokeStyle: unknown = "";
  lineWidth = 0;
  textAlign = "";
  textBaseline = "";
  private alphaValue = 1;
  set globalAlpha(value: number) {
    this.alphaValue = value;
    this.alphas.push(value);
  }
  get globalAlpha() {
    return this.alphaValue;
  }
  beginPath = vi.fn();
  arc = vi.fn();
  fill = vi.fn();
  stroke = vi.fn();
  setLineDash = vi.fn();
  fillText = vi.fn();
  measureText = vi.fn(() => ({ width: 10 }));
  save = vi.fn();
  restore = vi.fn();
}

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status < 400,
    status,
    json: async () => body,
  } as Response;
}

function stubGraphFetch(options: { graph?: () => Response } = {}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://localhost");
    const route = url.pathname;
    if (route.includes("/api/wiki/status")) return jsonResponse(statusEnvelope);
    if (route.includes("/api/wiki/health")) return jsonResponse(healthEnvelope);
    if (route.includes("/api/wiki/sources")) return jsonResponse(sourcesEnvelope);
    if (route.includes("/api/wiki/pages")) return jsonResponse(pagesEnvelope);
    if (route.includes("/api/wiki/graph")) {
      return options.graph?.() ?? jsonResponse(graphViewEnvelope);
    }
    if (route.includes("/api/wiki/backlinks")) return jsonResponse(backlinksEnvelope);
    if (route.includes("/api/wiki/read")) return jsonResponse(browseReadGobbyEnvelope);
    return jsonResponse({ ok: true, payload: {} });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function graphRequestIncludes(fetchMock: ReturnType<typeof vi.fn>): string[] {
  return fetchMock.mock.calls
    .map((call) => String(call[0]))
    .filter((url) => url.includes("/api/wiki/graph"))
    .map((url) => new URL(url, "http://localhost").searchParams.get("include") ?? "");
}

const overrideSpies = { request: vi.fn(), release: vi.fn() };

function WikiHarness() {
  const guard = useDirtyGuardController();
  return (
    <DirtyGuardProvider value={guard}>
      <WikiTab
        projectId="p1"
        requestPanelOverride={overrideSpies.request}
        releasePanelOverride={overrideSpies.release}
      />
    </DirtyGuardProvider>
  );
}

async function openGraph(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Open graph" }));
  await screen.findByTestId("force-graph-2d");
}

function sceneNode(id: string): WikiGraphSceneNode {
  const node = capturedRef.current?.graphData.nodes.find((entry) => entry.id === id);
  if (!node) throw new Error(`node ${id} not in scene`);
  return { ...node, x: 0, y: 0 } as WikiGraphSceneNode;
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", MockResizeObserver);
  vi.stubGlobal("IntersectionObserver", MockIntersectionObserver);
  capturedRef.current = null;
  fgHandle.zoom.mockClear();
  fgHandle.centerAt.mockClear();
  fgHandle.zoomToFit.mockClear();
  overrideSpies.request.mockClear();
  overrideSpies.release.mockClear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.localStorage.clear();
  window.sessionStorage.clear();
});

describe("WikiGraphView shell (4.1.2)", () => {
  it("opens from the toolbar, takes the override, and hides sources/citations/unresolved by default", async () => {
    const fetchMock = stubGraphFetch();
    const user = userEvent.setup();
    render(<WikiHarness />);

    await openGraph(user);

    expect(overrideSpies.request).toHaveBeenCalledTimes(1);
    expect(graphRequestIncludes(fetchMock)).toContain("all");

    const ids = capturedRef.current?.graphData.nodes.map((node) => node.id) ?? [];
    expect(ids).toEqual(
      expect.arrayContaining([GOBBY_ID, GWIKI_ID, RUNNER_ID, WATCHER_ID]),
    );
    expect(ids).not.toContain(SOURCE_ID);
    expect(ids).not.toContain(CITATION_ID);
    expect(ids).not.toContain(UNRESOLVED_ID);

    // Persistent kind legend, deutan-safe companion to node colors.
    const legend = screen.getByRole("list", { name: "Graph legend" });
    expect(within(legend).getByText("Wiki page")).toBeInTheDocument();
    expect(within(legend).getByText("Code page")).toBeInTheDocument();
  });

  it("refetches with include=code when the scope filter changes", async () => {
    const fetchMock = stubGraphFetch();
    const user = userEvent.setup();
    render(<WikiHarness />);

    await openGraph(user);
    await user.click(screen.getByRole("radio", { name: "Code" }));

    await waitFor(() => expect(graphRequestIncludes(fetchMock)).toContain("code"));
  });

  it("persists toggles across close and reopen", async () => {
    stubGraphFetch();
    const user = userEvent.setup();
    render(<WikiHarness />);

    await openGraph(user);
    await user.click(screen.getByRole("checkbox", { name: "Sources & citations" }));
    await waitFor(() => {
      const ids = capturedRef.current?.graphData.nodes.map((node) => node.id) ?? [];
      expect(ids).toContain(SOURCE_ID);
      expect(ids).toContain(CITATION_ID);
    });

    await user.click(screen.getByRole("button", { name: "Close graph" }));
    await screen.findByRole("tree", { name: /wiki pages/i });

    await openGraph(user);
    expect(screen.getByRole("checkbox", { name: "Sources & citations" })).toBeChecked();
    const stored = JSON.parse(window.localStorage.getItem("gobby:wiki-tab:graph") ?? "{}") as {
      sources?: boolean;
    };
    expect(stored.sources).toBe(true);
  });

  it("closes on Escape, releases the override once, and restores the browsed page", async () => {
    stubGraphFetch();
    const user = userEvent.setup();
    render(<WikiHarness />);

    const tree = await screen.findByRole("tree", { name: /wiki pages/i });
    await user.click(within(tree).getByRole("treeitem", { name: /knowledge/i }));
    await user.click(await within(tree).findByRole("treeitem", { name: /concepts/i }));
    await user.click(await within(tree).findByRole("treeitem", { name: "Gobby" }));
    await screen.findByRole("heading", { name: "Gobby", level: 1 });

    await openGraph(user);
    await user.keyboard("{Escape}");

    await screen.findByRole("heading", { name: "Gobby", level: 1 });
    expect(overrideSpies.release).toHaveBeenCalledTimes(1);
  });

  it("shows the cap chip when the vault exceeds the node budget", async () => {
    const bigNodes = Array.from({ length: MAX_GRAPH_NODES + 100 }, (_, index) => ({
      id: `n${index}`,
      kind: "wiki_page",
      path: `knowledge/p${index}.md`,
      title: `P${index}`,
    }));
    stubGraphFetch({
      graph: () =>
        jsonResponse({
          ok: true,
          command: "graph",
          stderr: "",
          payload: {
            command: "graph",
            degraded: false,
            degraded_sources: [],
            nodes: bigNodes,
            edges: { links: [], imports: [], calls: [], callers: [], trust: [], audit: [] },
            analytics: null,
          },
        }),
    });
    const user = userEvent.setup();
    render(<WikiHarness />);

    await openGraph(user);

    expect(capturedRef.current?.graphData.nodes).toHaveLength(MAX_GRAPH_NODES);
    expect(screen.getByText(/top 1,500 of 1,600/i)).toBeInTheDocument();
  });
});

describe("WikiForceGraph interactions (4.1.1, 4.1.4)", () => {
  it("navigates to the clicked page and returns to browse", async () => {
    stubGraphFetch();
    const user = userEvent.setup();
    render(<WikiHarness />);

    await openGraph(user);
    await user.click(screen.getByTestId(`graph-node-${GOBBY_ID}`));

    await screen.findByRole("heading", { name: "Gobby", level: 1 });
    await waitFor(() => expect(overrideSpies.release).toHaveBeenCalledTimes(1));
  });

  it("renders unresolved targets hollow and documents solid", async () => {
    stubGraphFetch();
    const user = userEvent.setup();
    render(<WikiHarness />);

    await openGraph(user);
    await user.click(screen.getByRole("checkbox", { name: "Unresolved" }));
    await waitFor(() => {
      expect(
        capturedRef.current?.graphData.nodes.some((node) => node.id === UNRESOLVED_ID),
      ).toBe(true);
    });

    const unresolved = sceneNode(UNRESOLVED_ID);
    expect(unresolved.hollow).toBe(true);
    const hollowCtx = new MockCanvasCtx();
    capturedRef.current?.nodeCanvasObject?.(
      unresolved,
      hollowCtx as unknown as CanvasRenderingContext2D,
      1,
    );
    expect(hollowCtx.stroke).toHaveBeenCalled();
    expect(hollowCtx.setLineDash).toHaveBeenCalledWith([2, 2]);

    const solid = sceneNode(GOBBY_ID);
    expect(solid.hollow).toBe(false);
    const solidCtx = new MockCanvasCtx();
    capturedRef.current?.nodeCanvasObject?.(
      solid,
      solidCtx as unknown as CanvasRenderingContext2D,
      1,
    );
    expect(solidCtx.fill).toHaveBeenCalled();
    expect(solidCtx.setLineDash).not.toHaveBeenCalled();
  });

  it("dims non-matching nodes to 15% while searching", async () => {
    stubGraphFetch();
    const user = userEvent.setup();
    render(<WikiHarness />);

    await openGraph(user);
    await user.type(screen.getByRole("textbox", { name: "Search graph" }), "gwiki");

    const missCtx = new MockCanvasCtx();
    capturedRef.current?.nodeCanvasObject?.(
      sceneNode(RUNNER_ID),
      missCtx as unknown as CanvasRenderingContext2D,
      1,
    );
    expect(missCtx.alphas).toContain(0.15);

    const matchCtx = new MockCanvasCtx();
    capturedRef.current?.nodeCanvasObject?.(
      sceneNode(GWIKI_ID),
      matchCtx as unknown as CanvasRenderingContext2D,
      1,
    );
    expect(matchCtx.alphas).not.toContain(0.15);
  });

  it("highlights hover neighbors and dims the rest", async () => {
    stubGraphFetch();
    const user = userEvent.setup();
    render(<WikiHarness />);

    await openGraph(user);
    capturedRef.current?.onNodeHover?.(sceneNode(GWIKI_ID));

    await waitFor(() => {
      const dimmedCtx = new MockCanvasCtx();
      capturedRef.current?.nodeCanvasObject?.(
        sceneNode(RUNNER_ID),
        dimmedCtx as unknown as CanvasRenderingContext2D,
        1,
      );
      expect(dimmedCtx.alphas).toContain(0.3);
    });

    const neighborCtx = new MockCanvasCtx();
    capturedRef.current?.nodeCanvasObject?.(
      sceneNode(GOBBY_ID),
      neighborCtx as unknown as CanvasRenderingContext2D,
      1,
    );
    expect(neighborCtx.alphas).not.toContain(0.3);
  });

  it("draws labels only past the zoom threshold", async () => {
    stubGraphFetch();
    const user = userEvent.setup();
    render(<WikiHarness />);

    await openGraph(user);

    const farCtx = new MockCanvasCtx();
    capturedRef.current?.nodeCanvasObject?.(
      sceneNode(GOBBY_ID),
      farCtx as unknown as CanvasRenderingContext2D,
      1,
    );
    expect(farCtx.fillText).not.toHaveBeenCalled();

    const nearCtx = new MockCanvasCtx();
    capturedRef.current?.nodeCanvasObject?.(
      sceneNode(GOBBY_ID),
      nearCtx as unknown as CanvasRenderingContext2D,
      2,
    );
    expect(nearCtx.fillText).toHaveBeenCalledWith("Gobby", expect.any(Number), expect.any(Number));
  });

  it("zooms from the keyboard on the focused wrapper", async () => {
    stubGraphFetch();
    const user = userEvent.setup();
    render(<WikiHarness />);

    await openGraph(user);
    const wrapper = screen.getByRole("application", { name: /graph of \d+ pages/i });
    wrapper.focus();
    await user.keyboard("+");

    expect(fgHandle.zoom).toHaveBeenCalledWith(expect.any(Number), expect.any(Number));
  });
});

describe("reduced motion (4.1.3)", () => {
  it("pre-simulates the layout and fits without animation", async () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn().mockImplementation((query: string) => ({
        matches: query.includes("prefers-reduced-motion"),
        media: query,
        onchange: null,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    );
    stubGraphFetch();
    const user = userEvent.setup();
    render(<WikiHarness />);

    await openGraph(user);

    expect(capturedRef.current?.warmupTicks).toBe(200);
    expect(capturedRef.current?.cooldownTicks).toBe(0);

    capturedRef.current?.onEngineStop?.();
    expect(fgHandle.zoomToFit).toHaveBeenCalledWith(0, expect.any(Number));
  });

  it("animates settle and fit when motion is allowed", async () => {
    stubGraphFetch();
    const user = userEvent.setup();
    render(<WikiHarness />);

    await openGraph(user);

    expect(capturedRef.current?.warmupTicks).toBe(80);
    expect(capturedRef.current?.cooldownTicks).toBe(100);
    expect(capturedRef.current?.autoPauseRedraw).toBe(false);

    capturedRef.current?.onEngineStop?.();
    expect(fgHandle.zoomToFit).toHaveBeenCalledWith(400, expect.any(Number));
  });
});

describe("buildGraphScene (4.1.1)", () => {
  const payload = () => normalizeGraph(graphViewEnvelope.payload);
  const baseOptions: WikiGraphSceneOptions = {
    sources: false,
    unresolved: false,
    orphans: true,
    trust: true,
    audit: false,
    codeEdges: true,
    communities: false,
  };
  const fakeResolve = (varName: string, alpha?: number) =>
    alpha !== undefined ? `${varName}@${alpha}` : varName;

  it("drops sources/citations/unresolved by default and never keeps callers edges", () => {
    const scene = buildGraphScene(payload(), baseOptions, fakeResolve);

    expect(scene.nodes.map((node) => node.id).sort()).toEqual(
      [GOBBY_ID, GWIKI_ID, RUNNER_ID, WATCHER_ID].sort(),
    );
    const kinds = scene.links.map((link) => link.kind).sort();
    expect(kinds).toEqual(["calls", "imports", "links"]);
  });

  it("adds source/citation nodes with trust and audit layers when enabled", () => {
    const scene = buildGraphScene(
      payload(),
      { ...baseOptions, sources: true, audit: true },
      fakeResolve,
    );

    const ids = scene.nodes.map((node) => node.id);
    expect(ids).toContain(SOURCE_ID);
    expect(ids).toContain(CITATION_ID);
    const kinds = scene.links.map((link) => link.kind).sort();
    expect(kinds).toEqual(["audit", "calls", "imports", "links", "trust"]);
    const codeLinks = scene.links.filter((link) => link.kind === "imports");
    expect(codeLinks.every((link) => link.dashed)).toBe(true);
  });

  it("prunes orphans when the toggle is off", () => {
    const orphanPayload: WikiGraphPayload = {
      nodes: [
        { id: "a", kind: "wiki_page", path: "knowledge/a.md", title: "A" },
        { id: "b", kind: "wiki_page", path: "knowledge/b.md", title: "B" },
        { id: "orphan", kind: "wiki_page", path: "knowledge/o.md", title: "O" },
      ],
      edges: [{ source: "a", target: "b", kind: "links", rawTarget: null }],
      degraded: false,
      degradedSources: [],
      analytics: null,
    };

    const scene = buildGraphScene(orphanPayload, { ...baseOptions, orphans: false }, fakeResolve);
    expect(scene.nodes.map((node) => node.id).sort()).toEqual(["a", "b"]);
  });

  it("caps at the node budget by descending degree", () => {
    const total = MAX_GRAPH_NODES + 100;
    const bigPayload: WikiGraphPayload = {
      nodes: Array.from({ length: total }, (_, index) => ({
        id: `n${index}`,
        kind: "wiki_page",
        path: `knowledge/p${index}.md`,
        title: `P${index}`,
      })),
      edges: Array.from({ length: 20 }, (_, index) => ({
        source: "n0",
        target: `n${index + 1}`,
        kind: "links",
        rawTarget: null,
      })),
      degraded: false,
      degradedSources: [],
      analytics: null,
    };

    const scene = buildGraphScene(bigPayload, baseOptions, fakeResolve);
    expect(scene.totalNodes).toBe(total);
    expect(scene.nodes).toHaveLength(MAX_GRAPH_NODES);
    expect(scene.capped).toBe(true);
    expect(scene.nodes.some((node) => node.id === "n0")).toBe(true);
  });

  it("sizes nodes from the analytics centrality degree map", () => {
    const scene = buildGraphScene(payload(), baseOptions, fakeResolve);
    const gobby = scene.nodes.find((node) => node.id === GOBBY_ID);
    expect(gobby?.val).toBe(wikiNodeVal(5));
  });

  it("cycles community colors through the chart series only when enabled", () => {
    const plain = buildGraphScene(payload(), baseOptions, fakeResolve);
    expect(plain.nodes.every((node) => node.communityColor === null)).toBe(true);

    const colored = buildGraphScene(payload(), { ...baseOptions, communities: true }, fakeResolve);
    const gobby = colored.nodes.find((node) => node.id === GOBBY_ID);
    const runner = colored.nodes.find((node) => node.id === RUNNER_ID);
    expect(gobby?.communityColor).toBe("--chart-series-1");
    expect(runner?.communityColor).toBe("--chart-series-2");
  });
});
