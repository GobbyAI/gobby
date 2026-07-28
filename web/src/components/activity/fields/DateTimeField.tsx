import { useId } from "react";

import { useResolvedTheme } from "../../../hooks/useResolvedTheme";
import { cn } from "../../../lib/utils";
import { localInputValueToUtcIso, utcIsoToLocalInputValue } from "./dateTimeConversion";
import type { DraftFieldBaseProps } from "./types";

interface DateTimeFieldProps extends DraftFieldBaseProps {
  value: string;
  onChange: (value: string) => void;
}

const fieldShellClass = "flex flex-col gap-1.5";
const labelClass = "text-sm font-medium text-muted-foreground";
const controlClass = cn(
  "min-h-11 rounded-md border border-border bg-[var(--bg-secondary)] px-3 py-2",
  "text-sm text-foreground transition-colors placeholder:text-muted-foreground",
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent",
  "focus-visible:ring-offset-2 focus-visible:ring-offset-background",
  "disabled:cursor-not-allowed disabled:opacity-60",
);

export function DateTimeField({
  label,
  value,
  onChange,
  disabled,
  placeholder,
  ariaLabel,
}: DateTimeFieldProps) {
  const id = useId();
  const resolvedTheme = useResolvedTheme();

  return (
    <label className={fieldShellClass} htmlFor={id}>
      <span className={labelClass}>{label}</span>
      <input
        id={id}
        type="datetime-local"
        className={controlClass}
        aria-label={ariaLabel}
        value={utcIsoToLocalInputValue(value)}
        disabled={disabled}
        placeholder={placeholder}
        step={60}
        style={{ colorScheme: resolvedTheme }}
        onChange={(event) => onChange(localInputValueToUtcIso(event.target.value, value))}
      />
    </label>
  );
}
