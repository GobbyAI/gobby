import { useEffect, useMemo, useRef, useState } from "react";

import { SegmentedControl } from "../ui/SegmentedControl";
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
    update({ datePreset: preset });
  }

  function handleReset(): void {
    onChange(defaultSessionsFilters());
    setShowCustomDate(false);
  }

  const activeCount = countActiveFilters(filters);

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
        className="absolute top-full right-2 z-[100] w-[min(280px,calc(100vw-1.5rem))] max-h-[60vh] overflow-y-auto border border-border rounded-md shadow-xl flex flex-col"
        style={{ background: "var(--bg-secondary)" }}
        role="dialog"
        aria-label="Session filters"
      >
        <div className="flex flex-col p-1.5 gap-0.5">
          {/* Mode */}
          <Section label="Mode">
            {MODE_OPTIONS.map((option) => (
              <CheckboxRow
                key={option.value}
                label={option.label}
                checked={isInclusiveSetChecked(filters.modes, option.value)}
                onToggle={() => handleModeToggle(option.value)}
              />
            ))}
          </Section>

          {/* Provider */}
          <Section label="Provider">
            {sortedProviderOptions.length === 0 ? (
              <EmptyHint>No providers available</EmptyHint>
            ) : (
              sortedProviderOptions.map((provider) => (
                <CheckboxRow
                  key={provider}
                  label={provider}
                  checked={isInclusiveSetChecked(filters.providers, provider)}
                  onToggle={() => handleProviderToggle(provider)}
                />
              ))
            )}
          </Section>

          {/* Session ref */}
          <Section label="Session ref">
            <RefRangeInputs
              minValue={filters.sessionRefMin}
              maxValue={filters.sessionRefMax}
              onChangeMin={(value) => update({ sessionRefMin: value })}
              onChangeMax={(value) => update({ sessionRefMax: value })}
              ariaLabelPrefix="Session ref"
            />
          </Section>

          {/* Task ref */}
          <Section label="Task ref">
            <div className="flex items-center gap-2 px-2 py-1">
              {TASK_REF_ROLES.map((role) => (
                <label
                  key={role.value}
                  className="flex items-center gap-1 text-xs text-muted-foreground cursor-pointer"
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
          </Section>

          {/* Date range */}
          <Section label="Date range">
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
              className="px-2 py-1 text-xs text-muted-foreground hover:text-foreground text-left"
              onClick={() => {
                const next = !showCustomDate;
                setShowCustomDate(next);
                if (next) {
                  update({ datePreset: "custom" });
                } else if (filters.datePreset === "custom") {
                  update({ datePreset: "all" });
                }
              }}
              aria-expanded={showCustomDate}
            >
              {showCustomDate ? "▾" : "▸"} Custom range
            </button>
            {showCustomDate && (
              <div className="flex items-center gap-1 px-2 py-1">
                <input
                  type="date"
                  className="w-[7.5rem] px-1.5 py-0.5 text-xs bg-transparent border border-border rounded text-foreground focus:outline-none focus:border-accent"
                  value={filters.dateCustomFrom ?? ""}
                  onChange={(e) =>
                    update({ dateCustomFrom: e.target.value || null, datePreset: "custom" })
                  }
                  aria-label="Custom date from"
                />
                <span className="text-xs text-muted-foreground">→</span>
                <input
                  type="date"
                  className="w-[7.5rem] px-1.5 py-0.5 text-xs bg-transparent border border-border rounded text-foreground focus:outline-none focus:border-accent"
                  value={filters.dateCustomTo ?? ""}
                  onChange={(e) =>
                    update({ dateCustomTo: e.target.value || null, datePreset: "custom" })
                  }
                  aria-label="Custom date to"
                />
              </div>
            )}
          </Section>
        </div>

        <div
          className="flex items-center justify-between border-t border-border px-2 py-1.5"
          style={{ background: "var(--bg-secondary)" }}
        >
          <button
            type="button"
            className="text-xs text-muted-foreground hover:text-foreground disabled:opacity-50"
            onClick={handleReset}
            disabled={activeCount === 0}
          >
            Reset
          </button>
          <button
            type="button"
            className="text-xs text-accent hover:underline"
            onClick={onClose}
          >
            Apply
          </button>
        </div>
      </div>
    </>
  );
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-0.5 py-0.5">
      <div className="px-2 py-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground/80">
        {label}
      </div>
      {children}
    </div>
  );
}

function CheckboxRow({
  label,
  checked,
  onToggle,
}: {
  label: string;
  checked: boolean;
  onToggle: () => void;
}) {
  return (
    <label className="flex items-center gap-1.5 px-2 py-1 rounded text-xs text-muted-foreground cursor-pointer hover:bg-muted/50">
      <input type="checkbox" className="w-3 h-3" checked={checked} onChange={onToggle} />
      <span className="truncate">{label}</span>
    </label>
  );
}

function EmptyHint({ children }: { children: React.ReactNode }) {
  return <div className="px-2 py-1 text-xs text-muted-foreground">{children}</div>;
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
  const inputClassName = `w-16 px-1.5 py-0.5 text-xs font-mono bg-transparent border rounded text-foreground focus:outline-none ${
    isInvalid
      ? "border-[var(--color-error)] focus:border-[var(--color-error)]"
      : "border-border focus:border-accent"
  }`;

  return (
    <div className="px-2 py-1">
      <div className="flex items-center gap-1">
        <input
          type="number"
          className={inputClassName}
          placeholder="from #"
          value={minValue !== null ? String(minValue) : ""}
          onChange={(e) => onChangeMin(parseRefBound(e.target.value))}
          aria-label={`${ariaLabelPrefix} minimum`}
          aria-invalid={isInvalid}
        />
        <span className="text-xs text-muted-foreground">→</span>
        <input
          type="number"
          className={inputClassName}
          placeholder="to #"
          value={maxValue !== null ? String(maxValue) : ""}
          onChange={(e) => onChangeMax(parseRefBound(e.target.value))}
          aria-label={`${ariaLabelPrefix} maximum`}
          aria-invalid={isInvalid}
        />
      </div>
      {isInvalid && (
        <div className="mt-1 text-xs text-[var(--color-error)]">Min must be &lt;= Max</div>
      )}
    </div>
  );
}
