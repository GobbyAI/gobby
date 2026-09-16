import { describe, it, expect } from "vitest";
import { act, render } from "@testing-library/react";
import { useEffect } from "react";
import type { StageRegistryEntry } from "../../../hooks/useStagesRegistry";
import { useTasksTabFilters } from "../useTasksTabFilters";
import { DEFAULT_FILTERS } from "../TasksTabModel";

function registryStage(name: string, position: number): StageRegistryEntry {
  return {
    name,
    display_name: name,
    category: "implementation",
    state: "ready",
    review_policy: "required",
    updated_at: null,
    position,
  };
}

const REVIEW_STAGE = registryStage("operator_review", 20);
const REGISTRY: StageRegistryEntry[] = [
  registryStage("development", 10),
  REVIEW_STAGE,
];
const REVIEW_ONLY_REGISTRY: StageRegistryEntry[] = [REVIEW_STAGE];

type Filters = ReturnType<typeof useTasksTabFilters>;

// Reports every committed frame, not every render: a render the hook corrects
// before commit is never shown to anyone and must not count as one.
function FiltersProbe({
  stages,
  onCommit,
}: {
  stages: StageRegistryEntry[];
  onCommit: (filters: Filters) => void;
}) {
  const filters = useTasksTabFilters(stages);
  useEffect(() => {
    onCommit(filters);
  });
  return null;
}

function latest(commits: Filters[]): Filters | undefined {
  return commits[commits.length - 1];
}

// The pair TasksTab reads to decide whether the tree shows every stage.
function hidesStages(filters: Filters): boolean {
  return (
    filters.defaultStageFilters.size > 0 &&
    !filters.stageSelectionMatchesDefault
  );
}

describe("useTasksTabFilters", () => {
  it("never commits a frame that hides every stage while the registry loads", () => {
    const commits: Filters[] = [];
    const record = (filters: Filters) => commits.push(filters);
    const { rerender } = render(<FiltersProbe stages={[]} onCommit={record} />);

    rerender(<FiltersProbe stages={REGISTRY} onCommit={record} />);

    // Such a frame empties the task tree and drops the caller's selection with
    // it, which the registry merely arriving must never do.
    expect(commits.map(hidesStages)).not.toContain(true);
    expect(latest(commits)?.stageQueryList).toEqual([]);
    expect(latest(commits)?.activeFilterCount).toBe(0);
  });

  it("keeps a narrowed stage selection when the registry grows", () => {
    const commits: Filters[] = [];
    const record = (filters: Filters) => commits.push(filters);
    const { rerender } = render(
      <FiltersProbe stages={REGISTRY} onCommit={record} />,
    );

    act(() => {
      latest(commits)?.handleFiltersApply(
        new Set(DEFAULT_FILTERS),
        new Set(["development"]),
      );
    });
    expect(latest(commits)?.stageQueryList).toEqual(["development"]);

    rerender(
      <FiltersProbe
        stages={[...REGISTRY, registryStage("qa", 30)]}
        onCommit={record}
      />,
    );

    expect(latest(commits)?.stageQueryList).toEqual(["development"]);
  });

  it("drops a selected stage the registry no longer offers", () => {
    const commits: Filters[] = [];
    const record = (filters: Filters) => commits.push(filters);
    const { rerender } = render(
      <FiltersProbe stages={REGISTRY} onCommit={record} />,
    );

    act(() => {
      latest(commits)?.handleFiltersApply(
        new Set(DEFAULT_FILTERS),
        new Set(["development"]),
      );
    });

    rerender(<FiltersProbe stages={REVIEW_ONLY_REGISTRY} onCommit={record} />);

    expect(latest(commits)?.selectedStageFilters.size).toBe(0);
  });
});
