import { Button } from "../ui/Button";

export interface ActivityFilterFooterProps {
  onReset: () => void;
  onApply: () => void;
  resetDisabled?: boolean;
  applyLabel?: string;
}

export function ActivityFilterFooter({
  onReset,
  onApply,
  resetDisabled,
  applyLabel = "Apply",
}: ActivityFilterFooterProps) {
  return (
    <div
      className="flex items-center justify-between border-t border-border px-2 py-1.5"
      style={{ background: "var(--bg-secondary)" }}
    >
      <Button
        type="button"
        variant="accent"
        size="sm"
        onClick={onReset}
        disabled={resetDisabled}
      >
        Reset
      </Button>
      <Button type="button" variant="accent" size="sm" onClick={onApply}>
        {applyLabel}
      </Button>
    </div>
  );
}
