import { useEffect, useMemo, useRef, useState } from "react";

import { ActivityFilterFooter } from "./ActivityFilterFooter";
import { FilterCheckboxRow, FilterSection } from "./FilterPrimitives";
import { SegmentedControl } from "../ui/SegmentedControl";
import { getProviderDisplayName } from "../../lib/providerModels";
import { useIsMobile } from "../../hooks/useIsMobile";
import {
  countActiveFilters,
  defaultSessionsFilters,
  type DatePreset,
  type SessionMode,
  type SessionsFilters,
  type TaskRefRole,
} from "./sessionsFilters";

interface SessionsFilterDropdownProps {
  filters: SessionsFilters;
  onChange: (next: SessionsFilters) => void;
  providerOptions: readonly string[];
  onClose: () => void;
}

const MODE_OPTIONS: ReadonlyArray<{ value: SessionMode; label: string }> = [
  { value: "interactive", label: "Interactive" },
  { value: "auto", label: "Autonomous" },
];
const MODE_VALUES = MODE_OPTIONS.map((option) => option.value);

const TASK_REF_ROLES: ReadonlyArray<{ value: TaskRefRole; label: string }> = [
  { value: "claimed", label: "Claimed" },
  { value: "created", label: "Created" },
  { value: "closed", label: "Closed" },
];

const DATE_PRESET_OPTIONS = [
  { value: "all" as const, label: "All" },
  { value: "24h" as const, label: "24h" },
  { value: "7d" as const, label: "7d" },
  { value: "30d" as const, label: "30d" },
] as const;

function toggleSetMember<T>(set: Set<T>, value: T): Set<T> {
  const next = new Set(set);
  if (next.has(value)) {
    next.delete(value);
  } else {
    next.add(value);
  }
  return next;
}

function isInclusiveSetChecked<T>(set: Set<T>, value: T): boolean {
  return set.size === 0 || set.has(value);
}

function toggleInclusiveSetMember<T>(
  set: Set<T>,
  value: T,
  allValues: readonly T[],
): Set<T> {
  const next = set.size === 0 ? new Set(allValues) : new Set(set);
  if (next.has(value)) {
    next.delete(value);
  } else {
    next.add(value);
  }
  return next.size === 0 || next.size === allValues.length ? new Set<T>() : next;
}

function parseRefBound(raw: string): number | null {
  if (raw.trim() === "") return null;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) ? parsed : null;
}

export function SessionsFilterDropdown({
  filters,
  onChange,
  providerOptions,
  onClose,
}: SessionsFilterDropdownProps) {
  const [showCustomDate, setShowCustomDate] = useState(filters.datePreset === "custom");
  const panelRef = useRef<HTMLDivElement>(null);
  const isMobile = useIsMobile();
  const sortedProviderOptions = useMemo(
    () =>
      [...providerOptions].sort((left, right) =>
        left.localeCompare(right, undefined, { sensitivity: "base" }),
      ),
    [providerOptions],
  );

  // Escape closes the dropdown — small a11y improvement over the Tasks-tab
  // pattern, which leaves Esc as a browser-default no-op.
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setShowCustomDate(filters.datePreset === "custom");
  }, [filters.datePreset]);

  function update(patch: Partial<SessionsFilters>): void {
    onChange({ ...filters, ...patch });
  }

  function handleModeToggle(mode: SessionMode): void {
    update({ modes: toggleInclusiveSetMember(filters.modes, mode, MODE_VALUES) });
  }

  function handleProviderToggle(provider: string): void {
    update({
      providers: toggleInclusiveSetMember(
        filters.providers,
        provider,
        sortedProviderOptions,
      ),
    });
  }

  function handleTaskRefRoleToggle(role: TaskRefRole): void {
    update({ taskRefRoles: toggleSetMember(filters.taskRefRoles, role) });
  }

  function handleDatePresetChange(preset: DatePreset): void {
    setShowCustomDate(preset === "custom");
    update({ datePreset: preset });
  }

  function handleCustomDateToggle(): void {
    const nextShowCustomDate = !showCustomDate;
    setShowCustomDate(nextShowCustomDate);
    if (!nextShowCustomDate) {
      update({ datePreset: "all" });
    } else if (filters.dateCustomFrom || filters.dateCustomTo) {
      update({ datePreset: "custom" });
    }
  }

  function handleReset(): void {
    onChange(defaultSessionsFilters());
    setShowCustomDate(false);
  }

  const activeCount = countActiveFilters(filters);

  // Width is capped at 320px on every viewport. On mobile (<768px) the panel
  // becomes a centered popup modal; on larger viewports it's a right-anchored
  // dropdown attached to the filter button. Both share the same internal
  // 2-column body so the visual treatment is identical — only positioning
  // changes.
  const panelClass = isMobile
    ? "fixed left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 z-[100] w-80 max-w-[calc(100vw-1.5rem)] max-h-[80vh] overflow-y-auto border border-border rounded-md shadow-xl flex flex-col"
    : "absolute top-full right-2 z-[100] w-80 max-h-[60vh] overflow-y-auto border border-border rounded-md shadow-xl flex flex-col";

  return (
    <>
      <div
        className="fixed inset-0 z-[99]"
        onClick={onClose}
        aria-hidden="true"
        data-testid="sessions-filter-overlay"
      />
      <div
        ref={panelRef}
        className={panelClass}
        style={{ background: "var(--bg-secondary)" }}
        role="dialog"
        aria-label="Session filters"
        aria-modal={isMobile || undefined}
      >
        <div className="grid grid-cols-[auto_minmax(0,1fr)] divide-x divide-border">
          {/* Left column: Mode + Provider */}
          <div className="flex flex-col gap-0.5 p-1.5 min-w-0">
            <FilterSection label="Mode">
              {MODE_OPTIONS.map((option) => (
                <FilterCheckboxRow
                  key={option.value}
                  label={option.label}
                  checked={isInclusiveSetChecked(filters.modes, option.value)}
                  onToggle={() => handleModeToggle(option.value)}
                />
              ))}
            </FilterSection>

            <FilterSection label="Provider">
              {sortedProviderOptions.length === 0 ? (
                <EmptyHint>No providers available</EmptyHint>
              ) : (
                sortedProviderOptions.map((provider) => (
                  <FilterCheckboxRow
                    key={provider}
                    label={getProviderDisplayName(provider) || provider}
                    checked={isInclusiveSetChecked(filters.providers, provider)}
                    onToggle={() => handleProviderToggle(provider)}
                  />
                ))
              )}
            </FilterSection>
          </div>

          {/* Right column: Session ref + Task ref + Date range */}
          <div className="flex flex-col gap-0.5 p-1.5 min-w-0">
            <FilterSection label="Session ref">
              <RefRangeInputs
                minValue={filters.sessionRefMin}
                maxValue={filters.sessionRefMax}
                onChangeMin={(value) => update({ sessionRefMin: value })}
                onChangeMax={(value) => update({ sessionRefMax: value })}
                ariaLabelPrefix="Session ref"
              />
            </FilterSection>

            <FilterSection label="Task ref">
              <div className="flex flex-col gap-0.5 px-2 py-1">
                {TASK_REF_ROLES.map((role) => (
                  <label
                    key={role.value}
                    className="flex min-w-0 items-center gap-1.5 text-[length:var(--text-md)] text-muted-foreground cursor-pointer"
                  >
                    <input
                      type="checkbox"
                      className="w-3 h-3"
                      checked={filters.taskRefRoles.has(role.value)}
                      onChange={() => handleTaskRefRoleToggle(role.value)}
                    />
                    <span>{role.label}</span>
                  </label>
                ))}
              </div>
              <RefRangeInputs
                minValue={filters.taskRefMin}
                maxValue={filters.taskRefMax}
                onChangeMin={(value) => update({ taskRefMin: value })}
                onChangeMax={(value) => update({ taskRefMax: value })}
                ariaLabelPrefix="Task ref"
              />
            </FilterSection>

            <FilterSection label="Date range">
              <div className="px-2 py-1">
                <SegmentedControl<DatePreset>
                  value={filters.datePreset === "custom" ? "all" : filters.datePreset}
                  onChange={handleDatePresetChange}
                  options={DATE_PRESET_OPTIONS}
                  ariaLabel="Date preset"
                />
              </div>
              <button
                type="button"
                className="px-2 py-1 text-[length:var(--text-md)] text-muted-foreground hover:text-foreground text-left"
                onClick={handleCustomDateToggle}
                aria-expanded={showCustomDate}
              >
                {showCustomDate ? "▾" : "▸"} Custom range
              </button>
              {showCustomDate && (
                <div className="flex flex-col gap-1 px-2 py-1">
                  <input
                    type="date"
                    className="w-full px-1.5 py-0.5 text-[length:var(--text-md)] bg-transparent border border-border rounded text-foreground focus:outline-none focus:border-accent"
                    value={filters.dateCustomFrom ?? ""}
                    onChange={(e) =>
                      update({ dateCustomFrom: e.target.value || null, datePreset: "custom" })
                    }
                    aria-label="Custom date from"
                  />
                  <input
                    type="date"
                    className="w-full px-1.5 py-0.5 text-[length:var(--text-md)] bg-transparent border border-border rounded text-foreground focus:outline-none focus:border-accent"
                    value={filters.dateCustomTo ?? ""}
                    onChange={(e) =>
                      update({ dateCustomTo: e.target.value || null, datePreset: "custom" })
                    }
                    aria-label="Custom date to"
                  />
                </div>
              )}
            </FilterSection>
          </div>
        </div>

        <ActivityFilterFooter
          onReset={handleReset}
          onApply={onClose}
          resetDisabled={activeCount === 0}
        />
      </div>
    </>
  );
}

function EmptyHint({ children }: { children: React.ReactNode }) {
  return <div className="px-2 py-1 text-[length:var(--text-md)] text-muted-foreground">{children}</div>;
}

function RefRangeInputs({
  minValue,
  maxValue,
  onChangeMin,
  onChangeMax,
  ariaLabelPrefix,
}: {
  minValue: number | null;
  maxValue: number | null;
  onChangeMin: (value: number | null) => void;
  onChangeMax: (value: number | null) => void;
  ariaLabelPrefix: string;
}) {
  const isInvalid = minValue !== null && maxValue !== null && minValue > maxValue;
  const inputClassName = `w-[4.5rem] px-1.5 py-0.5 text-[length:var(--text-md)] font-mono bg-transparent border rounded text-foreground focus:outline-none ${
    isInvalid
      ? "border-[var(--color-error)] focus:border-[var(--color-error)]"
      : "border-border focus:border-accent"
  }`;

  return (
    <div className="px-2 py-1">
      <div className="flex items-center gap-1">
        <input
          type="text"
          inputMode="numeric"
          pattern="[0-9]*"
          className={inputClassName}
          placeholder="from"
          value={minValue !== null ? String(minValue) : ""}
          onChange={(e) => onChangeMin(parseRefBound(e.target.value))}
          aria-label={`${ariaLabelPrefix} minimum`}
          aria-invalid={isInvalid}
        />
        <span className="text-[length:var(--text-md)] text-muted-foreground">→</span>
        <input
          type="text"
          inputMode="numeric"
          pattern="[0-9]*"
          className={inputClassName}
          placeholder="to"
          value={maxValue !== null ? String(maxValue) : ""}
          onChange={(e) => onChangeMax(parseRefBound(e.target.value))}
          aria-label={`${ariaLabelPrefix} maximum`}
          aria-invalid={isInvalid}
        />
      </div>
      {isInvalid && (
        <div className="mt-1 text-[length:var(--text-md)] text-[var(--color-error)]">Min must be &lt;= Max</div>
      )}
    </div>
  );
}
