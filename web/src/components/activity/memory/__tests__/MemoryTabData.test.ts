import { describe, expect, it } from "vitest";

import type { GobbyMemory } from "../../../../hooks/useMemory";
import {
  dreamFlagLabel,
  extractDreamPurgeGraceDays,
  extractGraphLimits,
  filterMemories,
  filtersFromMemoryHook,
  isHiddenMemory,
  memoryDreamFlag,
  memoryScopeLabel,
  purgeCountdownLabel,
  type MemoryTabFilters,
} from "../MemoryTabData";
import { DEFAULT_GRAPH_LIMITS } from "../KnowledgeGraphModel";

const NOW = new Date("2026-06-15T00:00:00Z").getTime();
const GRACE_DAYS = { review: 90, delete: 30 };

function makeMemory(overrides: Partial<GobbyMemory> = {}): GobbyMemory {
  return {
    id: "mem-1",
    memory_type: "fact",
    content: "TypeScript uses structural typing",
    created_at: "2026-06-14T00:00:00Z",
    updated_at: "2026-06-14T00:00:00Z",
    project_id: "proj-1",
    is_global: false,
    source_type: "agent",
    source_session_id: null,
    importance: 0.5,
    access_count: 0,
    last_accessed_at: null,
    tags: [],
    deleted_at: null,
    dream_action: null,
    last_dreamed_at: null,
    ...overrides,
  };
}

function baseFilters(overrides: Partial<MemoryTabFilters> = {}): MemoryTabFilters {
  return {
    search: "",
    memoryType: null,
    recentOnly: false,
    visibility: "active",
    ...overrides,
  };
}

describe("isHiddenMemory / memoryDreamFlag", () => {
  it("treats only rows with deleted_at as hidden", () => {
    expect(isHiddenMemory(makeMemory())).toBe(false);
    expect(isHiddenMemory(makeMemory({ deleted_at: "2026-06-10T00:00:00Z" }))).toBe(true);
  });

  it("normalizes the dream action only for hidden rows with a known action", () => {
    expect(memoryDreamFlag(makeMemory({ dream_action: "review" }))).toBeNull();
    expect(
      memoryDreamFlag(makeMemory({ deleted_at: "x", dream_action: "review" })),
    ).toBe("review");
    expect(
      memoryDreamFlag(makeMemory({ deleted_at: "x", dream_action: "delete" })),
    ).toBe("delete");
    expect(
      memoryDreamFlag(makeMemory({ deleted_at: "x", dream_action: "merge" })),
    ).toBeNull();
  });

  it("labels the dream flag with text (never hue alone)", () => {
    expect(dreamFlagLabel(makeMemory())).toBeNull();
    expect(
      dreamFlagLabel(makeMemory({ deleted_at: "x", dream_action: "review" })),
    ).toBe("Flagged for review");
    expect(
      dreamFlagLabel(makeMemory({ deleted_at: "x", dream_action: "delete" })),
    ).toBe("Flagged for deletion");
    // Hidden but unknown/missing action still gets a generic label.
    expect(dreamFlagLabel(makeMemory({ deleted_at: "x", dream_action: null }))).toBe("Flagged");
  });
});

describe("memoryScopeLabel", () => {
  it("labels global and project-scoped rows", () => {
    expect(memoryScopeLabel(makeMemory({ is_global: true }))).toBe("Global");
    expect(memoryScopeLabel(makeMemory({ is_global: false }))).toBe("Project");
  });
});

describe("filterMemories visibility", () => {
  const active = makeMemory({ id: "active" });
  const hidden = makeMemory({ id: "hidden", deleted_at: "2026-06-10T00:00:00Z", dream_action: "review" });

  it("active visibility hides dream-flagged rows", () => {
    const result = filterMemories([active, hidden], baseFilters({ visibility: "active" }), NOW);
    expect(result.map((m) => m.id)).toEqual(["active"]);
  });

  it("hidden visibility shows only dream-flagged rows", () => {
    const result = filterMemories([active, hidden], baseFilters({ visibility: "hidden" }), NOW);
    expect(result.map((m) => m.id)).toEqual(["hidden"]);
  });

  it("all visibility shows everything", () => {
    const result = filterMemories([active, hidden], baseFilters({ visibility: "all" }), NOW);
    expect(result.map((m) => m.id)).toEqual(["active", "hidden"]);
  });

  it("still applies type and search filters within the visibility scope", () => {
    const hiddenPattern = makeMemory({
      id: "hidden-pattern",
      memory_type: "pattern",
      deleted_at: "2026-06-10T00:00:00Z",
      dream_action: "delete",
    });
    const result = filterMemories(
      [active, hidden, hiddenPattern],
      baseFilters({ visibility: "hidden", memoryType: "pattern" }),
      NOW,
    );
    expect(result.map((m) => m.id)).toEqual(["hidden-pattern"]);
  });
});

describe("filtersFromMemoryHook", () => {
  it("passes visibility through to the tab filters", () => {
    const tab = filtersFromMemoryHook({
      projectId: "proj-1",
      memoryType: "fact",
      recentOnly: true,
      search: "ts",
      visibility: "all",
    });
    expect(tab).toEqual({
      search: "ts",
      memoryType: "fact",
      recentOnly: true,
      visibility: "all",
    });
  });
});

describe("purgeCountdownLabel", () => {
  it("returns null for active memories", () => {
    expect(purgeCountdownLabel(makeMemory(), GRACE_DAYS, NOW)).toBeNull();
  });

  it("counts down from deleted_at using the per-action grace window", () => {
    const fiveDaysAgo = new Date(NOW - 5 * 24 * 60 * 60 * 1000).toISOString();
    // delete grace is 30 days → 30 - 5 = 25 remaining.
    expect(
      purgeCountdownLabel(
        makeMemory({ deleted_at: fiveDaysAgo, dream_action: "delete" }),
        GRACE_DAYS,
        NOW,
      ),
    ).toBe("Purges in 25 days");
    // review grace is 90 days → 90 - 5 = 85 remaining.
    expect(
      purgeCountdownLabel(
        makeMemory({ deleted_at: fiveDaysAgo, dream_action: "review" }),
        GRACE_DAYS,
        NOW,
      ),
    ).toBe("Purges in 85 days");
  });

  it("reports imminent purge once the grace window has elapsed", () => {
    const longAgo = new Date(NOW - 40 * 24 * 60 * 60 * 1000).toISOString();
    expect(
      purgeCountdownLabel(
        makeMemory({ deleted_at: longAgo, dream_action: "delete" }),
        GRACE_DAYS,
        NOW,
      ),
    ).toBe("Purges on next sweep");
  });

  it("returns null until backend grace days are available", () => {
    const fiveDaysAgo = new Date(NOW - 5 * 24 * 60 * 60 * 1000).toISOString();

    expect(
      purgeCountdownLabel(
        makeMemory({ deleted_at: fiveDaysAgo, dream_action: "delete" }),
        null,
        NOW,
      ),
    ).toBeNull();
  });
});

describe("extractDreamPurgeGraceDays", () => {
  it("reads purge grace windows from nested backend config values", () => {
    expect(
      extractDreamPurgeGraceDays({
        memory: {
          dream: {
            purge_review_after_days: 91,
            purge_delete_after_days: 31,
          },
        },
      }),
    ).toEqual({ review: 91, delete: 31 });
  });

  it("returns null for malformed config values", () => {
    expect(extractDreamPurgeGraceDays({ memory: { dream: {} } })).toBeNull();
    expect(
      extractDreamPurgeGraceDays({
        memory: { dream: { purge_review_after_days: Number.POSITIVE_INFINITY } },
      }),
    ).toBeNull();
  });
});

describe("extractGraphLimits (#19157)", () => {
  it("reads the persisted knowledge-graph limits from ui config", () => {
    expect(
      extractGraphLimits({
        ui: { knowledge_graph_limit: 0, knowledge_graph_relationship_limit: 12000 },
      }),
    ).toEqual({ entities: 0, relationships: 12000 });
  });

  it("falls back to defaults for missing or malformed values", () => {
    expect(extractGraphLimits(undefined)).toEqual(DEFAULT_GRAPH_LIMITS);
    expect(extractGraphLimits({ ui: {} })).toEqual(DEFAULT_GRAPH_LIMITS);
    expect(
      extractGraphLimits({
        ui: { knowledge_graph_limit: null, knowledge_graph_relationship_limit: "" },
      }),
    ).toEqual(DEFAULT_GRAPH_LIMITS);
    expect(
      extractGraphLimits({
        ui: { knowledge_graph_limit: "lots", knowledge_graph_relationship_limit: -5 },
      }),
    ).toEqual(DEFAULT_GRAPH_LIMITS);
  });
});
