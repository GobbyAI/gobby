import { describe, expect, it } from "vitest";

import {
  countActiveFilters,
  defaultSessionsFilters,
  deserializeFromStorage,
  matchesSessionsFilters,
  resolveDateRange,
  serializeForStorage,
  serializeSessionsFilters,
} from "../sessionsFilters";

interface TestSession {
  source: string;
  status: string;
  model: string | null;
  agent_depth: number;
  seq_num: number | null;
  created_at: string;
  claimed_task_refs?: number[];
  created_task_refs?: number[];
  closed_task_refs?: number[];
}

function makeSession(overrides: Partial<TestSession> = {}): TestSession {
  return {
    source: "claude",
    status: "active",
    model: "claude-opus-4-7",
    agent_depth: 0,
    seq_num: 1,
    created_at: "2026-04-29T12:00:00.000Z",
    ...overrides,
  };
}

const NOW = new Date("2026-04-29T12:00:00.000Z");

describe("defaultSessionsFilters", () => {
  it("starts with empty filters except claimed-only task ref roles", () => {
    const f = defaultSessionsFilters();
    expect(f.modes.size).toBe(0);
    expect(f.providers.size).toBe(0);
    expect(f.models.size).toBe(0);
    expect(f.sessionRefMin).toBeNull();
    expect(f.sessionRefMax).toBeNull();
    expect(f.taskRefMin).toBeNull();
    expect(f.taskRefMax).toBeNull();
    expect([...f.taskRefRoles]).toEqual(["claimed"]);
    expect(f.datePreset).toBe("all");
  });
});

describe("countActiveFilters", () => {
  it("returns 0 for the default state", () => {
    expect(countActiveFilters(defaultSessionsFilters())).toBe(0);
  });

  it("counts each non-default section once", () => {
    const f = defaultSessionsFilters();
    f.modes.add("interactive");
    f.providers.add("claude");
    f.taskRefMin = 100;
    f.datePreset = "7d";
    // 4 sections active: modes, providers, task ref range, date preset
    expect(countActiveFilters(f)).toBe(4);
  });

  it("treats a single ref bound as an active range", () => {
    const f = defaultSessionsFilters();
    f.sessionRefMax = 500;
    expect(countActiveFilters(f)).toBe(1);
  });
});

describe("serializeSessionsFilters", () => {
  it("emits status_in=active,paused for the default Live state", () => {
    const params = serializeSessionsFilters(defaultSessionsFilters(), NOW);
    // The Live | Expired SegmentedControl is always set to one of two values,
    // so the status filter is always emitted. Default = Live = {active, paused}.
    expect(params.getAll("status_in").sort()).toEqual(["active", "paused"]);
    // No other filter params for the default state.
    const nonStatus = [...params.entries()].filter(([k]) => k !== "status_in");
    expect(nonStatus).toEqual([]);
  });

  it("serializes modes as repeated mode= entries", () => {
    const f = defaultSessionsFilters();
    f.modes.add("interactive");
    const params = serializeSessionsFilters(f, NOW);
    expect(params.getAll("mode")).toEqual(["interactive"]);
  });

  it("omits mode entries when all modes are selected", () => {
    const f = defaultSessionsFilters();
    f.modes = new Set(["interactive", "auto"]);
    const params = serializeSessionsFilters(f, NOW);
    expect(params.has("mode")).toBe(false);
  });

  it("serializes providers as repeated sources= entries", () => {
    const f = defaultSessionsFilters();
    f.providers.add("claude");
    f.providers.add("codex");
    const params = serializeSessionsFilters(f, NOW);
    expect(params.getAll("sources").sort()).toEqual(["claude", "codex"]);
  });

  it("serializes session ref range to session_seq_min/max", () => {
    const f = defaultSessionsFilters();
    f.sessionRefMin = 10;
    f.sessionRefMax = 200;
    const params = serializeSessionsFilters(f, NOW);
    expect(params.get("session_seq_min")).toBe("10");
    expect(params.get("session_seq_max")).toBe("200");
  });

  it("serializes task ref range with default 'claimed' role", () => {
    const f = defaultSessionsFilters();
    f.taskRefMin = 5000;
    f.taskRefMax = 5500;
    const params = serializeSessionsFilters(f, NOW);
    expect(params.get("task_ref_min")).toBe("5000");
    expect(params.get("task_ref_max")).toBe("5500");
    expect(params.getAll("task_ref_role")).toEqual(["claimed"]);
  });

  it("omits task_ref_role when range is unset, even if roles are non-default", () => {
    const f = defaultSessionsFilters();
    f.taskRefRoles.add("created");
    f.taskRefRoles.add("closed");
    const params = serializeSessionsFilters(f, NOW);
    expect(params.has("task_ref_role")).toBe(false);
    expect(params.has("task_ref_min")).toBe(false);
  });

  it("emits multiple roles when the range is set", () => {
    const f = defaultSessionsFilters();
    f.taskRefMin = 1;
    f.taskRefRoles = new Set(["claimed", "created", "closed"]);
    const params = serializeSessionsFilters(f, NOW);
    expect(params.getAll("task_ref_role").sort()).toEqual(["claimed", "closed", "created"]);
  });

  it("resolves the 7d preset to created_after", () => {
    const f = defaultSessionsFilters();
    f.datePreset = "7d";
    const params = serializeSessionsFilters(f, NOW);
    expect(params.has("created_after")).toBe(true);
    expect(params.has("created_before")).toBe(false);
    // 7 days before NOW
    const after = params.get("created_after")!;
    expect(after).toBe("2026-04-22T12:00:00.000Z");
  });

  it("serializes Expired as status_in=expired only", () => {
    const f = defaultSessionsFilters();
    f.statuses = new Set(["expired"]);
    const params = serializeSessionsFilters(f, NOW);
    expect(params.getAll("status_in")).toEqual(["expired"]);
  });

  it("emits multi-status status_in when caller forces all three", () => {
    const f = defaultSessionsFilters();
    f.statuses = new Set(["active", "paused", "expired"]);
    const params = serializeSessionsFilters(f, NOW);
    expect(params.getAll("status_in").sort()).toEqual(["active", "expired", "paused"]);
  });

  it("omits status_in when statuses set is empty", () => {
    const f = defaultSessionsFilters();
    f.statuses = new Set();
    const params = serializeSessionsFilters(f, NOW);
    expect(params.has("status_in")).toBe(false);
  });

  it("resolves a custom date range to inclusive-after / exclusive-before", () => {
    const f = defaultSessionsFilters();
    f.datePreset = "custom";
    f.dateCustomFrom = "2026-04-01";
    f.dateCustomTo = "2026-04-15";
    const params = serializeSessionsFilters(f, NOW);
    expect(params.get("created_after")).toBe("2026-04-01T00:00:00.000Z");
    // The bound is bumped a day so end-of-day stays inclusive when paired
    // with the backend's exclusive-before predicate.
    expect(params.get("created_before")).toBe("2026-04-16T00:00:00.000Z");
  });
});

describe("resolveDateRange", () => {
  it("returns null bounds for 'all'", () => {
    const f = defaultSessionsFilters();
    expect(resolveDateRange(f, NOW)).toEqual({ after: null, before: null });
  });

  it("returns 24h after only", () => {
    const f = defaultSessionsFilters();
    f.datePreset = "24h";
    const { after, before } = resolveDateRange(f, NOW);
    expect(after).toBe("2026-04-28T12:00:00.000Z");
    expect(before).toBeNull();
  });
});

describe("matchesSessionsFilters", () => {
  it("default filters match every session", () => {
    expect(matchesSessionsFilters(makeSession(), defaultSessionsFilters(), NOW)).toBe(true);
  });

  it("mode interactive narrows to agent_depth 0", () => {
    const f = defaultSessionsFilters();
    f.modes.add("interactive");
    expect(matchesSessionsFilters(makeSession({ agent_depth: 0 }), f, NOW)).toBe(true);
    expect(matchesSessionsFilters(makeSession({ agent_depth: 2 }), f, NOW)).toBe(false);
  });

  it("provider narrows to matching source", () => {
    const f = defaultSessionsFilters();
    f.providers.add("codex");
    expect(matchesSessionsFilters(makeSession({ source: "codex" }), f, NOW)).toBe(true);
    expect(matchesSessionsFilters(makeSession({ source: "claude" }), f, NOW)).toBe(false);
  });

  it("model filters are ignored after model selection was removed", () => {
    const f = defaultSessionsFilters();
    f.models.add("claude-opus-4-7");
    expect(matchesSessionsFilters(makeSession({ model: null }), f, NOW)).toBe(true);
  });

  it("session ref range filters by seq_num", () => {
    const f = defaultSessionsFilters();
    f.sessionRefMin = 100;
    f.sessionRefMax = 200;
    expect(matchesSessionsFilters(makeSession({ seq_num: 150 }), f, NOW)).toBe(true);
    expect(matchesSessionsFilters(makeSession({ seq_num: 50 }), f, NOW)).toBe(false);
    expect(matchesSessionsFilters(makeSession({ seq_num: 250 }), f, NOW)).toBe(false);
    expect(matchesSessionsFilters(makeSession({ seq_num: null }), f, NOW)).toBe(false);
  });

  it("task ref range matches when any selected role overlaps", () => {
    const f = defaultSessionsFilters();
    f.taskRefMin = 1000;
    f.taskRefMax = 2000;
    f.taskRefRoles = new Set(["claimed", "created"]);

    expect(
      matchesSessionsFilters(makeSession({ claimed_task_refs: [1500] }), f, NOW),
    ).toBe(true);
    expect(
      matchesSessionsFilters(makeSession({ created_task_refs: [1500] }), f, NOW),
    ).toBe(true);
    // closed is not in selected roles → match fails
    expect(
      matchesSessionsFilters(makeSession({ closed_task_refs: [1500] }), f, NOW),
    ).toBe(false);
    expect(
      matchesSessionsFilters(makeSession({ claimed_task_refs: [3000] }), f, NOW),
    ).toBe(false);
    // No task refs at all
    expect(matchesSessionsFilters(makeSession(), f, NOW)).toBe(false);
  });

  it("status filter narrows to selected statuses", () => {
    const f = defaultSessionsFilters();
    f.statuses = new Set(["expired"]);
    expect(matchesSessionsFilters(makeSession({ status: "expired" }), f, NOW)).toBe(true);
    expect(matchesSessionsFilters(makeSession({ status: "active" }), f, NOW)).toBe(false);
    expect(matchesSessionsFilters(makeSession({ status: "paused" }), f, NOW)).toBe(false);
  });

  it("default Live status filter excludes expired sessions", () => {
    const f = defaultSessionsFilters();
    expect(matchesSessionsFilters(makeSession({ status: "active" }), f, NOW)).toBe(true);
    expect(matchesSessionsFilters(makeSession({ status: "paused" }), f, NOW)).toBe(true);
    expect(matchesSessionsFilters(makeSession({ status: "expired" }), f, NOW)).toBe(false);
  });

  it("empty statuses set acts as no filter", () => {
    const f = defaultSessionsFilters();
    f.statuses = new Set();
    expect(matchesSessionsFilters(makeSession({ status: "active" }), f, NOW)).toBe(true);
    expect(matchesSessionsFilters(makeSession({ status: "expired" }), f, NOW)).toBe(true);
  });

  it("date preset 24h excludes older sessions", () => {
    const f = defaultSessionsFilters();
    f.datePreset = "24h";
    expect(
      matchesSessionsFilters(makeSession({ created_at: NOW.toISOString() }), f, NOW),
    ).toBe(true);
    expect(
      matchesSessionsFilters(makeSession({ created_at: "2026-04-01T00:00:00.000Z" }), f, NOW),
    ).toBe(false);
  });
});

describe("storage round-trip", () => {
  it("restores Set fields from the stored array form", () => {
    const original = defaultSessionsFilters();
    original.modes.add("auto");
    original.providers.add("codex");
    original.models.add("legacy-hidden-model");
    original.taskRefMin = 10;
    original.taskRefRoles = new Set(["claimed", "created"]);
    original.datePreset = "30d";

    const stored = JSON.stringify(serializeForStorage(original));
    const restored = deserializeFromStorage(stored);

    expect([...restored.modes]).toEqual(["auto"]);
    expect([...restored.providers]).toEqual(["codex"]);
    expect([...restored.models]).toEqual([]);
    expect(restored.taskRefMin).toBe(10);
    expect([...restored.taskRefRoles].sort()).toEqual(["claimed", "created"]);
    expect(restored.datePreset).toBe("30d");
  });

  it("returns defaults when storage is null", () => {
    const restored = deserializeFromStorage(null);
    expect(restored.datePreset).toBe("all");
    expect([...restored.taskRefRoles]).toEqual(["claimed"]);
  });

  it("returns defaults on malformed JSON without throwing", () => {
    const restored = deserializeFromStorage("{not json");
    expect(restored.datePreset).toBe("all");
  });

  it("strips unknown enum values gracefully", () => {
    const stored = JSON.stringify({
      modes: ["interactive", "bogus"],
      taskRefRoles: ["claimed", "fake"],
      datePreset: "made-up",
      statuses: ["active", "ghost"],
    });
    const restored = deserializeFromStorage(stored);
    expect([...restored.modes]).toEqual(["interactive"]);
    expect([...restored.taskRefRoles]).toEqual(["claimed"]);
    expect(restored.datePreset).toBe("all"); // fell back to default
    expect([...restored.statuses]).toEqual(["active"]);
  });

  it("falls back to default Live when statuses field is missing", () => {
    const stored = JSON.stringify({
      modes: ["interactive"],
    });
    const restored = deserializeFromStorage(stored);
    expect([...restored.statuses].sort()).toEqual(["active", "paused"]);
  });

  it("round-trips a non-default Expired status filter", () => {
    const original = defaultSessionsFilters();
    original.statuses = new Set(["expired"]);
    const stored = JSON.stringify(serializeForStorage(original));
    const restored = deserializeFromStorage(stored);
    expect([...restored.statuses]).toEqual(["expired"]);
  });
});
