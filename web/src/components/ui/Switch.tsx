import { cn } from "../../lib/utils";

interface SwitchProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  "aria-label": string;
}

export function Switch({
  checked,
  onChange,
  disabled = false,
  "aria-label": ariaLabel,
}: SwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        "inline-flex h-6 w-11 shrink-0 items-center justify-center rounded-full border-0 bg-transparent p-0 pointer-coarse:h-11",
        disabled ? "cursor-not-allowed opacity-50" : "cursor-pointer",
      )}
    >
      <span
        className={cn(
          "inline-flex h-6 w-11 shrink-0 items-center rounded-full border-2 transition-colors duration-150",
          checked
            ? "border-transparent bg-accent"
            : "border-border bg-[var(--bg-tertiary)]",
        )}
      >
        {/* The off-state knob takes a mid-tone fill: a bg-background knob on a
            muted track is invisible in dark mode (#20047). */}
        <span
          className={cn(
            "block h-5 w-5 rounded-full shadow-sm transition-transform duration-150",
            checked
              ? "translate-x-5 bg-background"
              : "translate-x-0 bg-[var(--text-muted)]",
          )}
        />
      </span>
    </button>
  );
}
