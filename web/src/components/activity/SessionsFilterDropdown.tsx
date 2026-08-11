import { useEffect, useMemo, useRef, useState } from "react";

import { cn } from "../../lib/utils";
import { Button } from "../ui/Button";
import { coarseHitAreaCls } from "../ui/controlStyles";
import { DropdownCaret } from "../ui/DropdownCaret";
import { Input } from "../ui/Input";
import { ActivityFilterFooter } from "./ActivityFilterFooter";
import {
  FilterCheckboxRow,
  FilterDropdownShell,
  FilterFieldRow,
  FilterSection,
} from "./FilterPrimitives";
import { SegmentedControl } from "../ui/SegmentedControl";
import { getProviderDisplayName } from "../../lib/providerModels";
import { useDialogFocus } from "../../hooks/useDialogFocus";
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
  const [previousDatePreset, setPreviousDatePreset] = useState(filters.datePreset);
  const [showCustomDate, setShowCustomDate] = useState(filters.datePreset === "custom");
  if (previousDatePreset !== filters.datePreset) {
    setPreviousDatePreset(filters.datePreset);
    setShowCustomDate(filters.datePreset === "custom");
  }
  const panelRef = useRef<HTMLDivElement>(null);
  const isMobile = useIsMobile();
  useDialogFocus({ ref: panelRef, isOpen: isMobile, onClose });
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
      if (event.key === "Escape" && !event.defaultPrevented) onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

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

  // Width is capped at 320px on every viewport, and on desktop additionally by
  // the tab content so the popover never overflows a minimum-width activity
  // panel (#20045). On the mobile tier the panel becomes a centered popup
  // modal; on larger viewports it's a right-anchored dropdown pinned to the
  // top of the tab content, directly below the header Filter trigger. Both
  // share the same internal body, which collapses from two columns to one via
  // a container query when the popover itself is squeezed below 320px.
  const panelClass = isMobile
    ? "@container fixed left-1/2 top-1/2 w-80 max-w-[calc(100vw-1.5rem)] max-h-[80vh] -translate-x-1/2 -translate-y-1/2 overflow-y-auto"
    : "@container absolute top-1 right-2 w-80 max-w-[calc(100%-1rem)] max-h-[60vh] overflow-y-auto";

  return (
    <FilterDropdownShell
      panelRef={panelRef}
      onClose={onClose}
      overlayTestId="sessions-filter-overlay"
      ariaLabel="Session filters"
      ariaModal={isMobile || undefined}
      className={panelClass}
    >
      <div className="grid grid-cols-1 @min-[20rem]:grid-cols-[auto_minmax(0,1fr)]">
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

          <FilterSection label="Attention">
            <FilterCheckboxRow
              label="Blocked"
              checked={filters.blockedOnly}
              onToggle={() => update({ blockedOnly: !filters.blockedOnly })}
            />
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

        {/* Right column: Session ref + Task ref */}
        <div className="flex flex-col gap-0.5 p-1.5 min-w-0 border-t border-border @min-[20rem]:border-t-0 @min-[20rem]:border-l">
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
            {/* Rows sit directly in the section like Mode/Provider do — an
                extra px-2 wrapper indented these checkboxes off the shared
                rail (#20047). */}
            {TASK_REF_ROLES.map((role) => (
              <FilterCheckboxRow
                key={role.value}
                label={role.label}
                checked={filters.taskRefRoles.has(role.value)}
                onToggle={() => handleTaskRefRoleToggle(role.value)}
              />
            ))}
            <RefRangeInputs
              minValue={filters.taskRefMin}
              maxValue={filters.taskRefMax}
              onChangeMin={(value) => update({ taskRefMin: value })}
              onChangeMax={(value) => update({ taskRefMax: value })}
              ariaLabelPrefix="Task ref"
            />
          </FilterSection>
        </div>

        {/* Date range spans the full popover so every preset segment stays visible */}
        <div className="flex flex-col gap-0.5 p-1.5 min-w-0 border-t border-border @min-[20rem]:col-span-2">
          <FilterSection label="Date range">
            <div className="px-2 py-1">
              <SegmentedControl<DatePreset>
                value={filters.datePreset === "custom" ? "all" : filters.datePreset}
                onChange={handleDatePresetChange}
                options={DATE_PRESET_OPTIONS}
                ariaLabel="Date preset"
              />
            </div>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              dense
              className={cn(
                "w-full justify-start px-2 text-left text-[length:var(--text-md)] font-normal text-muted-foreground",
                coarseHitAreaCls,
              )}
              onClick={handleCustomDateToggle}
              aria-expanded={showCustomDate}
            >
              <DropdownCaret open={showCustomDate} />
              Custom range
            </Button>
            {showCustomDate && (
              <FilterFieldRow className="flex flex-col gap-1">
                <Input
                  type="date"
                  wrapperClassName="w-full"
                  className="h-7 w-full px-1.5 py-0.5 text-[length:var(--text-md)]"
                  value={filters.dateCustomFrom ?? ""}
                  onChange={(e) =>
                    update({ dateCustomFrom: e.target.value || null, datePreset: "custom" })
                  }
                  aria-label="Custom date from"
                />
                <Input
                  type="date"
                  wrapperClassName="w-full"
                  className="h-7 w-full px-1.5 py-0.5 text-[length:var(--text-md)]"
                  value={filters.dateCustomTo ?? ""}
                  onChange={(e) =>
                    update({ dateCustomTo: e.target.value || null, datePreset: "custom" })
                  }
                  aria-label="Custom date to"
                />
              </FilterFieldRow>
            )}
          </FilterSection>
        </div>
      </div>

      <ActivityFilterFooter
        onReset={handleReset}
        onApply={onClose}
        resetDisabled={activeCount === 0}
      />
    </FilterDropdownShell>
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
  const inputClassName =
    "h-7 w-full px-1.5 py-0.5 text-[length:var(--text-md)] font-mono";

  return (
    <FilterFieldRow>
      <div className="flex items-center gap-1">
        <Input
          type="text"
          inputMode="numeric"
          pattern="[0-9]*"
          className={inputClassName}
          wrapperClassName="min-w-0 flex-1"
          placeholder="from"
          value={minValue !== null ? String(minValue) : ""}
          onChange={(e) => onChangeMin(parseRefBound(e.target.value))}
          aria-label={`${ariaLabelPrefix} minimum`}
          aria-invalid={isInvalid}
          error={isInvalid}
        />
        <span className="text-[length:var(--text-md)] text-muted-foreground">to</span>
        <Input
          type="text"
          inputMode="numeric"
          pattern="[0-9]*"
          className={inputClassName}
          wrapperClassName="min-w-0 flex-1"
          placeholder="to"
          value={maxValue !== null ? String(maxValue) : ""}
          onChange={(e) => onChangeMax(parseRefBound(e.target.value))}
          aria-label={`${ariaLabelPrefix} maximum`}
          aria-invalid={isInvalid}
          error={isInvalid}
        />
      </div>
      {isInvalid && (
        <div className="mt-1 text-[length:var(--text-md)] text-[var(--color-error)]">Min must be &lt;= Max</div>
      )}
    </FilterFieldRow>
  );
}
