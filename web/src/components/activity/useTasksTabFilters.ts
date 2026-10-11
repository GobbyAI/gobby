import { useCallback, useMemo, useState } from "react";
import { DEFAULT_FILTERS, type TaskFilterKey } from "./TasksTabModel";

export function useTasksTabFilters() {
  const [statusFilters, setStatusFilters] = useState<Set<TaskFilterKey>>(
    () => new Set(DEFAULT_FILTERS),
  );
  const [showFilterDropdown, setShowFilterDropdown] = useState(false);
  const includeClosedTasks = statusFilters.has("closed");
  const activeFilterCount = useMemo(() => {
    const keys = new Set([...DEFAULT_FILTERS, ...statusFilters]);
    return [...keys].filter(
      (key) => DEFAULT_FILTERS.has(key) !== statusFilters.has(key),
    ).length;
  }, [statusFilters]);
  const handleFiltersApply = useCallback((nextFilters: Set<TaskFilterKey>) => {
    setStatusFilters(nextFilters);
  }, []);

  return {
    activeFilterCount,
    handleFiltersApply,
    includeClosedTasks,
    setShowFilterDropdown,
    showFilterDropdown,
    statusFilters,
  };
}
