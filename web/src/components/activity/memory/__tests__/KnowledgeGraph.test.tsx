import { render } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { KnowledgeGraphData } from "../../../../hooks/useMemory";

// The 3D stack never renders in these unit tests — stub it so importing the
// component module stays cheap and jsdom-safe.
vi.mock("react-force-graph-3d", () => ({ default: () => null }));
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

const { CANONICAL_ENTITY_TYPES, buildForceData, edgeColor, entityColorVar } = await import(
  "../KnowledgeGraphModel"
);
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
