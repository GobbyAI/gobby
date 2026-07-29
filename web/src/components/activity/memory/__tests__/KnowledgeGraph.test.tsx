import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { KnowledgeGraphData } from "../../../../hooks/useMemory";

// The 3D stack never renders in these unit tests — stub it so importing the
// component module stays cheap and jsdom-safe.
vi.mock("react-force-graph-3d", async () => {
  const ReactModule = await import("react");
  return {
    default: ReactModule.forwardRef(function MockForceGraph(_props, _ref) {
      return null;
    }),
  };
});
vi.mock("three-spritetext", () => ({ default: class SpriteText {} }));
vi.mock("three", () => ({
  SphereGeometry: class {},
  MeshLambertMaterial: class {},
  Mesh: class {},
}));

// Deterministic resolver: the contract under test is that node/link colors go
// through resolveCssVar (three.js parses concrete colors only), never raw
// `var()` literals — jsdom cannot resolve real custom properties.
vi.mock("../../../../lib/utils", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../../lib/utils")>();
  return {
    ...actual,
    resolveCssVar: (varName: string, alpha?: number) =>
      `resolved(${varName}@${alpha ?? 1})`,
  };
});

const {
  CANONICAL_ENTITY_TYPES,
  DEFAULT_GRAPH_LIMITS,
  MOBILE_ENTITY_CAP,
  MOBILE_RELATIONSHIP_CAP,
  buildForceData,
  buildNeighborIndex,
  buildNodeCardHtml,
  edgeColor,
  effectiveGraphLimits,
  entityColorVar,
  humanizeRelation,
  isOpaqueIdentifier,
  sanitizeGraphLimit,
} = await import("../KnowledgeGraphModel");
const { KnowledgeGraph } = await import("../KnowledgeGraph");

class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

function graphFixture(): KnowledgeGraphData {
  const entities = CANONICAL_ENTITY_TYPES.map((entityType, index) => ({
    entity_key: `entity-${index}`,
    name: `Entity ${index}`,
    entity_type: entityType,
    project_id: "project-1",
    properties: {},
  }));
  return {
    entities: [
      ...entities,
      // Off-vocabulary legacy type — must fall back to muted gray, not crash.
      {
        entity_key: "legacy-fn",
        name: "legacy_function",
        entity_type: "function",
        project_id: "project-1",
        properties: {},
      },
    ],
    relationships: [
      { source_key: "entity-0", target_key: "entity-1", type: "USES", properties: {} },
      { source_key: "entity-1", target_key: "legacy-fn", type: "DEPENDS_ON", properties: {} },
    ],
  };
}

describe("KnowledgeGraph color resolution (#19153)", () => {
  it("edgeColor returns resolved colors, never var() literals three.js renders black", () => {
    for (const relType of ["USES", "DEPENDS_ON", "MENTIONS", "RELATES_TO", "WORKS_ON", ""]) {
      const color = edgeColor(relType);
      expect(color).toMatch(/^resolved\(--/);
      expect(color).not.toMatch(/var\(/);
    }
  });

  it("buildForceData emits resolved colors on every node and link", () => {
    const { nodes, links } = buildForceData(graphFixture());
    expect(nodes.length).toBe(CANONICAL_ENTITY_TYPES.length + 1);
    expect(links.length).toBe(2);
    for (const node of nodes) {
      expect(node.color).toMatch(/^resolved\(--/);
      expect(node.color).not.toMatch(/var\(/);
    }
    for (const link of links) {
      expect(link.color).toMatch(/^resolved\(--/);
      expect(link.color).not.toMatch(/var\(/);
    }
  });
});

describe("KnowledgeGraph entity taxonomy colors (#19153)", () => {
  it("gives each canonical extraction type a distinct token with no gray fallthrough", () => {
    const vars = CANONICAL_ENTITY_TYPES.map((entityType) => entityColorVar(entityType));
    expect(new Set(vars).size).toBe(CANONICAL_ENTITY_TYPES.length);
    for (const colorVar of vars) {
      expect(colorVar).not.toBe("--text-muted");
      expect(colorVar).toMatch(/^--kg-entity-/);
    }
  });

  it("keeps muted gray as the fallback for off-vocabulary legacy types", () => {
    expect(entityColorVar("function")).toBe("--text-muted");
    expect(entityColorVar("unknown-thing")).toBe("--text-muted");
  });
});

describe("KnowledgeGraph node card (#19156)", () => {
  const entity = (overrides: Record<string, unknown> = {}) => ({
    entity_key: "gobby",
    name: "Gobby",
    entity_type: "project",
    project_id: "project-1",
    properties: { ignored: "never shown" },
    memory_count: 3,
    memory_preview: "Gobby is a local-first daemon unifying AI coding tools.",
    ...overrides,
  });

  it("renders name, type, memory snippet, and named connections — no raw properties", () => {
    const html = buildNodeCardHtml(entity(), [
      { name: "gcode", relation: "depends on", outgoing: true },
      { name: "Josh", relation: "maintained by", outgoing: false },
    ]);
    expect(html).toContain("Gobby");
    expect(html).toContain("project");
    expect(html).toContain("“Gobby is a local-first daemon unifying AI coding tools.”");
    expect(html).toContain("→ depends on ");
    expect(html).toContain("gcode");
    expect(html).toContain("← maintained by ");
    expect(html).toContain("Josh");
    expect(html).toContain("3 memories · 2 connections");
    expect(html).not.toContain("never shown");
  });

  it("never shows a bare UUID as primary text", () => {
    const uuid = "b38dc83b-1234-4abc-9def-0123456789ab";
    const html = buildNodeCardHtml(entity({ name: uuid, entity_type: "concept" }), []);
    expect(html).toContain("Unlabeled concept");
    expect(html).not.toContain(uuid);
    expect(html).toContain("b38dc83b…");
  });

  it("caps connections at four with an overflow line", () => {
    const connections = Array.from({ length: 6 }, (_, i) => ({
      name: `peer-${i}`,
      relation: "uses",
      outgoing: true,
    }));
    const html = buildNodeCardHtml(entity(), connections);
    expect(html).toContain("peer-3");
    expect(html).not.toContain("peer-4");
    expect(html).toContain("+2 more connections");
    expect(html).toContain("6 connections");
  });

  it("omits snippet and footer when the daemon fails open (no enrichment)", () => {
    const html = buildNodeCardHtml(
      entity({ memory_count: undefined, memory_preview: undefined }),
      [],
    );
    expect(html).toContain("Gobby");
    expect(html).not.toContain("“");
    expect(html).not.toContain("memories");
  });

  it("buildNeighborIndex resolves both string and post-simulation object endpoints", () => {
    const nodes = [
      { id: "a", name: "Alpha" },
      { id: "b", name: "Beta" },
    ];
    const links = [
      { source: "a", target: { id: "b" }, type: "DEPENDS_ON" },
      { source: "a", target: "missing", type: "USES" },
    ];
    const index = buildNeighborIndex(nodes, links);
    expect(index.get("a")).toEqual([{ name: "Beta", relation: "depends on", outgoing: true }]);
    expect(index.get("b")).toEqual([{ name: "Alpha", relation: "depends on", outgoing: false }]);
  });

  it("humanizes relation types and detects opaque identifiers", () => {
    expect(humanizeRelation("DEPENDS_ON")).toBe("depends on");
    expect(isOpaqueIdentifier("b38dc83b-1234-4abc-9def-0123456789ab")).toBe(true);
    expect(isOpaqueIdentifier("0123456789abcdef0123")).toBe(true);
    expect(isOpaqueIdentifier("Gobby")).toBe(false);
  });
});

describe("KnowledgeGraph limits (#19157)", () => {
  it("effectiveGraphLimits passes desktop values through, including 0 = no limit", () => {
    expect(effectiveGraphLimits({ entities: 0, relationships: 12000 }, false)).toEqual({
      entities: 0,
      relationships: 12000,
    });
  });

  it("effectiveGraphLimits hard-caps mobile and collapses 0 to the cap", () => {
    expect(effectiveGraphLimits({ entities: 0, relationships: 0 }, true)).toEqual({
      entities: MOBILE_ENTITY_CAP,
      relationships: MOBILE_RELATIONSHIP_CAP,
    });
    expect(effectiveGraphLimits({ entities: 9999, relationships: 99999 }, true)).toEqual({
      entities: MOBILE_ENTITY_CAP,
      relationships: MOBILE_RELATIONSHIP_CAP,
    });
    expect(effectiveGraphLimits({ entities: 100, relationships: 400 }, true)).toEqual({
      entities: 100,
      relationships: 400,
    });
  });

  it("sanitizeGraphLimit rejects NaN and negatives, floors fractions", () => {
    expect(sanitizeGraphLimit(Number.NaN, 500)).toBe(500);
    expect(sanitizeGraphLimit(-3, 500)).toBe(500);
    expect(sanitizeGraphLimit(250.7, 500)).toBe(250);
    expect(sanitizeGraphLimit(0, 500)).toBe(0);
  });

  it("fetches with the configured entity and relationship limits", async () => {
    vi.stubGlobal("ResizeObserver", MockResizeObserver);
    const fetchKnowledgeGraph = vi
      .fn()
      .mockResolvedValue({ entities: [], relationships: [] });
    render(
      <KnowledgeGraph
        fetchKnowledgeGraph={fetchKnowledgeGraph}
        fetchEntityNeighbors={vi.fn()}
        limits={{ entities: 42, relationships: 84 }}
      />,
    );
    await waitFor(() => expect(fetchKnowledgeGraph).toHaveBeenCalledWith(42, 84));
    vi.unstubAllGlobals();
  });

  it("gear panel exposes persisted limit controls with the instability warning", async () => {
    vi.stubGlobal("ResizeObserver", MockResizeObserver);
    const onLimitsChange = vi.fn();
    const fetchKnowledgeGraph = vi.fn().mockResolvedValue({
      entities: [
        {
          entity_key: "e1",
          name: "Entity One",
          entity_type: "concept",
          project_id: null,
          properties: {},
        },
      ],
      relationships: [],
    });
    render(
      <KnowledgeGraph
        fetchKnowledgeGraph={fetchKnowledgeGraph}
        fetchEntityNeighbors={vi.fn()}
        limits={DEFAULT_GRAPH_LIMITS}
        onLimitsChange={onLimitsChange}
      />,
    );

    fireEvent.click(await screen.findByLabelText("Graph settings"));

    const entityInput = screen.getByLabelText("Entity limit");
    expect(entityInput).toHaveValue(DEFAULT_GRAPH_LIMITS.entities);
    expect(screen.getByLabelText("Relationship limit")).toHaveValue(
      DEFAULT_GRAPH_LIMITS.relationships,
    );
    // Desktop allows unlimited, so the instability warning must be visible.
    expect(screen.getByText(/values can be unstable/)).toBeInTheDocument();

    fireEvent.change(entityInput, { target: { value: "0" } });
    fireEvent.blur(entityInput);
    expect(onLimitsChange).toHaveBeenCalledWith({
      entities: 0,
      relationships: DEFAULT_GRAPH_LIMITS.relationships,
    });

    // An emptied field reverts instead of silently persisting "no limit".
    const relationshipInput = screen.getByLabelText("Relationship limit");
    fireEvent.change(relationshipInput, { target: { value: "" } });
    fireEvent.blur(relationshipInput);
    expect(onLimitsChange).toHaveBeenCalledTimes(1);
    expect(relationshipInput).toHaveValue(DEFAULT_GRAPH_LIMITS.relationships);
    vi.unstubAllGlobals();
  });
});

describe("KnowledgeGraph stacking context (#19153)", () => {
  it("isolates its overlays so lower-z panel chrome (mobile tab menu, z-5) layers above the graph", () => {
    vi.stubGlobal("ResizeObserver", MockResizeObserver);
    const { container } = render(
      <KnowledgeGraph
        // Never resolves — the loading branch renders the same container.
        fetchKnowledgeGraph={vi.fn().mockReturnValue(new Promise(() => undefined))}
        fetchEntityNeighbors={vi.fn()}
      />,
    );
    const graphContainer = container.firstElementChild;
    expect(graphContainer?.classList.contains("isolate")).toBe(true);
    vi.unstubAllGlobals();
  });
});
