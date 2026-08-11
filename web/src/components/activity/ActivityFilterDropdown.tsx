import { useEffect, useRef } from "react";

import { cn } from "../../lib/utils";
import { Button } from "../ui/Button";
import { coarseHitAreaCls } from "../ui/controlStyles";
import { FilterDropdownShell } from "./FilterPrimitives";

export interface ActivityFilterOption<T extends string> {
  value: T;
  label: string;
}

interface ActivityFilterDropdownProps<T extends string> {
  value: T;
  options: readonly ActivityFilterOption<T>[];
  onChange: (value: T) => void;
  onClose: () => void;
  ariaLabel: string;
}

export function ActivityFilterDropdown<T extends string>({
  value,
  options,
  onChange,
  onClose,
  ariaLabel,
}: ActivityFilterDropdownProps<T>) {
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  // Anchored to the tab body (relative) just under the panel header — the
  // trigger lives in the shared header toolbar, not inside the tab.
  return (
    <FilterDropdownShell
      panelRef={panelRef}
      onClose={onClose}
      role="listbox"
      ariaLabel={ariaLabel}
      className="absolute top-1 right-2 w-[min(220px,calc(100vw-1.5rem))] p-1"
    >
      {options.map((option) => {
        const isActive = option.value === value;
        return (
          <Button
            key={option.value}
            type="button"
            variant="ghost"
            size="sm"
            dense
            role="option"
            aria-selected={isActive}
            className={cn(
              "activity-filter-dropdown__item",
              isActive && "activity-filter-dropdown__item--active",
              coarseHitAreaCls,
            )}
            onClick={() => {
              onChange(option.value);
              onClose();
            }}
          >
            {option.label}
          </Button>
        );
      })}
    </FilterDropdownShell>
  );
}
