import { cn } from "../../lib/utils";
import type { StagePivotChip } from "./TasksTabModel";

interface TasksMobileStageChipsProps {
  stages: StagePivotChip[];
  /** Count for the `All` chip (every status-filtered task). */
  totalCount: number;
  /**
   * `null` → `All` is active (no stage filter). A stage name → that chip is
   * active. `undefined` → a custom multi-stage filter is set via the Filter
   * dropdown, so no single chip owns the selection.
   */
  activeStage: string | null | undefined;
  /** `null` selects `All` (clear the stage filter); a name pivots to it. */
  onSelect: (stageName: string | null) => void;
}

const CHIP_BASE =
  "inline-flex min-h-11 shrink-0 items-center rounded-full px-3 " +
  "text-[length:var(--text-sm)] whitespace-nowrap transition-colors " +
  "focus-visible:outline-none focus-visible:ring-2 " +
  "focus-visible:ring-[var(--accent)] focus-visible:ring-offset-1 " +
  "focus-visible:ring-offset-[var(--bg-primary)]";
const CHIP_ACTIVE =
  "bg-[var(--accent)] text-[var(--accent-foreground)] font-semibold";
const CHIP_INACTIVE =
  "bg-[var(--bg-secondary)] text-[var(--text-secondary)] " +
  "border border-border font-medium";

/**
 * Mobile-only stage pivot. The Board (wide drag-between-stages kanban) is a
 * desktop affordance; on mobile the List is the task surface, and this is the
 * one-tap stage-centric view. It drives the *existing* stage filter state —
 * `All` clears it, a chip narrows to a single stage — so there is no second
 * filtering path and no kanban on a phone. Stage *moves* stay on the row
 * three-dot menu.
 *
 * Deutan-safe per `.impeccable.md`: the active chip is carried by lightness +
 * fill + weight (accent fill, bold), not hue alone, so it survives a
 * grayscale screenshot. Chips are ≥44px touch targets; the row scrolls
 * horizontally without scrollbar chrome.
 */
export function TasksMobileStageChips({
  stages,
  totalCount,
  activeStage,
  onSelect,
}: TasksMobileStageChipsProps) {
  return (
    <div
      role="group"
      aria-label="Filter tasks by stage"
      className={cn(
        "flex gap-2 overflow-x-auto border-b border-border px-2.5 py-1.5",
        "[scrollbar-width:none] [&::-webkit-scrollbar]:hidden",
      )}
    >
      <Chip
        label="All"
        count={totalCount}
        active={activeStage === null}
        onClick={() => onSelect(null)}
      />
      {stages.map((stage) => (
        <Chip
          key={stage.name}
          label={stage.label}
          count={stage.count}
          active={activeStage === stage.name}
          onClick={() => onSelect(stage.name)}
        />
      ))}
    </div>
  );
}

function Chip({
  label,
  count,
  active,
  onClick,
}: {
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      className={cn(CHIP_BASE, active ? CHIP_ACTIVE : CHIP_INACTIVE)}
    >
      <span className="max-w-[9rem] truncate">{label}</span>
      <span className="ml-1 tabular-nums opacity-70">{count}</span>
    </button>
  );
}
