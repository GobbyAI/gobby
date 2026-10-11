import { useCallback, useEffect, useState } from "react";
import { ActivityFilterFooter } from "./ActivityFilterFooter";
import {
  FilterCheckboxRow,
  FilterDropdownShell,
  FilterSection,
} from "./FilterPrimitives";
import {
  DEFAULT_FILTERS,
  getTaskFilterColor,
  getTaskFilterLabel,
  STAGE_STATE_FILTERS,
  STATUS_FILTERS,
  type TaskFilterKey,
} from "./TasksTabModel";

interface FilterDropdownProps {
  filters: Set<TaskFilterKey>;
  onApply: (filters: Set<TaskFilterKey>) => void;
  onClose: () => void;
}

export function TasksTabFilters({
  filters,
  onApply,
  onClose,
}: FilterDropdownProps) {
  // Draft state mirrors the SessionsFilterDropdown pattern: changes don't
  // touch the parent until Apply, so the user can stage edits and back out
  // via Escape or overlay click without polluting the visible task list.
  const [draftFilters, setDraftFilters] = useState<Set<TaskFilterKey>>(
    () => new Set(filters),
  );

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  const toggleDraftFilter = useCallback((key: TaskFilterKey) => {
    setDraftFilters((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const handleApply = useCallback(() => {
    onApply(draftFilters);
    onClose();
  }, [onApply, onClose, draftFilters]);

  const handleReset = useCallback(() => {
    setDraftFilters(new Set(DEFAULT_FILTERS));
  }, []);

  return (
    <FilterDropdownShell
      onClose={onClose}
      outsideInteraction="primary-mousedown"
      ariaLabel="Task filters"
      className="absolute top-1 right-2 w-[min(12rem,calc(100vw-1.5rem))]"
    >
      <div>
        <div className="flex min-w-0 flex-col gap-0.5 p-1.5">
          <FilterSection label="Stage state">
            <FilterCheckboxList
              states={STAGE_STATE_FILTERS}
              draftFilters={draftFilters}
              onToggle={toggleDraftFilter}
            />
          </FilterSection>
          <FilterSection label="Status">
            <FilterCheckboxList
              states={STATUS_FILTERS}
              draftFilters={draftFilters}
              onToggle={toggleDraftFilter}
            />
          </FilterSection>
        </div>
      </div>

      <ActivityFilterFooter onReset={handleReset} onApply={handleApply} />
    </FilterDropdownShell>
  );
}

function FilterCheckboxList({
  states,
  draftFilters,
  onToggle,
}: {
  states: TaskFilterKey[];
  draftFilters: Set<TaskFilterKey>;
  onToggle: (key: TaskFilterKey) => void;
}) {
  return (
    <div className="flex flex-col gap-0.5 px-2 py-1">
      {states.map((status) => (
        <FilterCheckboxRow
          key={status}
          label={getTaskFilterLabel(status)}
          checked={draftFilters.has(status)}
          onToggle={() => onToggle(status)}
          leading={
            <span
              className="h-1.5 w-1.5 shrink-0 rounded-full"
              style={{ backgroundColor: getTaskFilterColor(status) }}
              aria-hidden="true"
            />
          }
        />
      ))}
    </div>
  );
}
