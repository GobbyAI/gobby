import { describe, expect, it } from "vitest";

import type { StageRegistryEntry } from "../../../lib/taskNormalization";
import {
  computeStagePivot,
  getOrderedBoardStages,
  resolveActiveStagePivot,
} from "../TasksTabModel";

function stage(
  name: string,
  sequence_order: number | null,
  display_name = name,
): StageRegistryEntry {
  return { name, display_name, sequence_order } as StageRegistryEntry;
}

describe("getOrderedBoardStages", () => {
  it("drops retired stages and sorts by sequence_order", () => {
    const registry = [
      stage("review", 20),
      stage("test_arch", 5), // retired
      stage("development", 10),
    ];
    expect(getOrderedBoardStages(registry).map((s) => s.name)).toEqual([
      "development",
      "review",
    ]);
  });

  it("falls back to position then 0 when sequence_order is absent", () => {
    const registry = [
      { name: "b", display_name: "B", position: 2 } as StageRegistryEntry,
      { name: "a", display_name: "A", position: 1 } as StageRegistryEntry,
    ];
    expect(getOrderedBoardStages(registry).map((s) => s.name)).toEqual([
      "a",
      "b",
    ]);
  });
});

describe("resolveActiveStagePivot", () => {
  it("is null (All) when the selection equals the default set", () => {
    expect(resolveActiveStagePivot(new Set(["a", "b"]), true)).toBeNull();
  });

  it("is the single stage name when exactly one is selected", () => {
    expect(resolveActiveStagePivot(new Set(["development"]), false)).toBe(
      "development",
    );
  });

  it("is undefined for a custom multi-stage filter", () => {
    expect(
      resolveActiveStagePivot(new Set(["a", "b"]), false),
    ).toBeUndefined();
  });

  it("is undefined for an empty non-default selection", () => {
    expect(resolveActiveStagePivot(new Set(), false)).toBeUndefined();
  });
});

describe("computeStagePivot", () => {
  const registry = [stage("development", 10, "Development"), stage("qa", 20, "QA")];

  it("shapes chips in board order with zero counts and total when no tasks", () => {
    const pivot = computeStagePivot(
      [],
      new Set(),
      registry,
      () => "development",
    );
    expect(pivot.total).toBe(0);
    expect(pivot.chips).toEqual([
      { name: "development", label: "Development", count: 0 },
      { name: "qa", label: "QA", count: 0 },
    ]);
  });

  it("falls back to the stage name when display_name is empty", () => {
    const pivot = computeStagePivot(
      [],
      new Set(),
      [stage("solo", 1, "")],
      () => null,
    );
    expect(pivot.chips).toEqual([{ name: "solo", label: "solo", count: 0 }]);
  });
});
