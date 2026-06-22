import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { StageRegistryEntry } from "../../hooks/useStagesRegistry";
import { areSetsEqual } from "./TasksTabData";
import { DEFAULT_FILTERS, type TaskFilterKey } from "./TasksTabModel";

export function useTasksTabFilters(stagesRegistry: StageRegistryEntry[]) {
  const [selectedStageFilters, setSelectedStageFilters] = useState<Set<string>>(
    () => new Set(),
  );
  const [statusFilters, setStatusFilters] = useState<Set<TaskFilterKey>>(
    () => new Set(DEFAULT_FILTERS),
  );
  const [showFilterDropdown, setShowFilterDropdown] = useState(false);
  const previousDefaultStageFiltersRef = useRef<Set<string>>(new Set());
  const stageFiltersInitializedRef = useRef(false);

  const registryStageNames = useMemo(
    () => stagesRegistry.map((stage) => stage.name).sort(),
    [stagesRegistry],
  );
  const defaultStageFilters = useMemo(
    () => new Set(registryStageNames),
    [registryStageNames],
  );

  useEffect(() => {
    const previousDefaultStageFilters = previousDefaultStageFiltersRef.current;
    setSelectedStageFilters((prev) => {
      const shouldUseDefault =
        !stageFiltersInitializedRef.current ||
        areSetsEqual(prev, previousDefaultStageFilters);
      stageFiltersInitializedRef.current = true;

      if (shouldUseDefault) {
        return areSetsEqual(prev, defaultStageFilters)
          ? prev
          : new Set(defaultStageFilters);
      }

      const next = new Set(
        [...prev].filter((stageName) => defaultStageFilters.has(stageName)),
      );
      return areSetsEqual(prev, next) ? prev : next;
    });
    previousDefaultStageFiltersRef.current = new Set(defaultStageFilters);
  }, [defaultStageFilters]);

  const stageSelectionMatchesDefault = useMemo(
    () => areSetsEqual(selectedStageFilters, defaultStageFilters),
    [defaultStageFilters, selectedStageFilters],
  );
  const selectedRegistryStageNames = useMemo(
    () => registryStageNames.filter((stageName) => selectedStageFilters.has(stageName)),
    [registryStageNames, selectedStageFilters],
  );
  const stageQueryKey = useMemo(
    () =>
      !stageSelectionMatchesDefault && selectedRegistryStageNames.length > 0
        ? selectedRegistryStageNames.join("\u0000")
        : "",
    [selectedRegistryStageNames, stageSelectionMatchesDefault],
  );
  const stageQueryList = useMemo(
    () => (stageQueryKey ? stageQueryKey.split("\u0000") : []),
    [stageQueryKey],
  );
  const includeClosedTasks = statusFilters.has("closed");
  const activeFilterCount = useMemo(() => {
    const symmetricDifference = new Set([...DEFAULT_FILTERS, ...statusFilters]);
    const statusFilterCount = [...symmetricDifference].filter(
      (key) => DEFAULT_FILTERS.has(key) !== statusFilters.has(key),
    ).length;
    const stageFilterCount = registryStageNames.filter(
      (stageName) => !selectedStageFilters.has(stageName),
    ).length;
    return statusFilterCount + (stageSelectionMatchesDefault ? 0 : stageFilterCount);
  }, [
    registryStageNames,
    selectedStageFilters,
    stageSelectionMatchesDefault,
    statusFilters,
  ]);

  const handleFiltersApply = useCallback(
    (nextFilters: Set<TaskFilterKey>, nextStages: Set<string>) => {
      setStatusFilters(nextFilters);
      setSelectedStageFilters(nextStages);
    },
    [],
  );

  return {
    activeFilterCount,
    defaultStageFilters,
    handleFiltersApply,
    includeClosedTasks,
    selectedStageFilters,
    setShowFilterDropdown,
    showFilterDropdown,
    stageQueryList,
    stageSelectionMatchesDefault,
    statusFilters,
  };
}
