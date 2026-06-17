import { useState } from "react";

import { cn } from "../../../lib/utils";

interface KeyValueFieldProps {
  label: string;
  value: Record<string, string>;
  onChange: (value: Record<string, string>) => void;
  disabled?: boolean;
  ariaLabel: string;
}

function entriesToRecord(entries: Array<[string, string]>): Record<string, string> {
  return Object.fromEntries(entries);
}

export function KeyValueField({
  label,
  value,
  onChange,
  disabled,
  ariaLabel,
}: KeyValueFieldProps) {
  const entries = Object.entries(value);
  const [keyError, setKeyError] = useState<string | null>(null);

  function updateKey(index: number, nextKey: string) {
    const isDuplicate =
      nextKey !== "" &&
      entries.some((entry, entryIndex) => entryIndex !== index && entry[0] === nextKey);
    if (isDuplicate) {
      setKeyError(`Key "${nextKey}" already exists`);
      console.warn(
        `KeyValueField: ignored rename to duplicate key "${nextKey}" to avoid overwriting an existing entry`,
      );
      return;
    }
    setKeyError(null);
    onChange(
      entriesToRecord(
        entries.map((entry, entryIndex) =>
          entryIndex === index ? [nextKey, entry[1]] : entry,
        ),
      ),
    );
  }

  function updateValue(index: number, nextValue: string) {
    onChange(
      entriesToRecord(
        entries.map((entry, entryIndex) =>
          entryIndex === index ? [entry[0], nextValue] : entry,
        ),
      ),
    );
  }

  function removeRow(index: number) {
    onChange(entriesToRecord(entries.filter((_, entryIndex) => entryIndex !== index)));
  }

  return (
    <div className="flex flex-col gap-2">
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      <div role="group" aria-label={ariaLabel} className="flex flex-col gap-2">
        {keyError && (
          <span className="text-xs text-error" role="alert">
            {keyError}
          </span>
        )}
        {entries.map(([key, entryValue], index) => (
          <div key={index} className="grid grid-cols-[1fr_1fr_auto] gap-2">
            <input
              type="text"
              aria-label={`Key ${index + 1}`}
              className={inputClass}
              value={key}
              disabled={disabled}
              onChange={(event) => updateKey(index, event.target.value)}
            />
            <input
              type="text"
              aria-label={`Value ${index + 1}`}
              className={inputClass}
              value={entryValue}
              disabled={disabled}
              onChange={(event) => updateValue(index, event.target.value)}
            />
            <button
              type="button"
              className={iconButtonClass}
              aria-label={key ? `Remove ${key}` : `Remove row ${index + 1}`}
              disabled={disabled}
              onClick={() => removeRow(index)}
            >
              x
            </button>
          </div>
        ))}
        <button
          type="button"
          className={cn(
            "inline-flex min-h-11 items-center justify-center self-start rounded-md",
            "border border-border px-3 text-sm font-medium text-foreground",
            "transition-colors hover:bg-muted focus-visible:outline-none",
            "focus-visible:ring-2 focus-visible:ring-accent",
            "focus-visible:ring-offset-2 focus-visible:ring-offset-background",
            "disabled:cursor-not-allowed disabled:opacity-50",
          )}
          disabled={disabled}
          onClick={() => onChange({ ...value, "": "" })}
        >
          Add row
        </button>
      </div>
    </div>
  );
}

const inputClass = cn(
  "min-h-11 min-w-0 rounded-md border border-border bg-[var(--bg-secondary)]",
  "px-3 py-2 text-sm text-foreground transition-colors",
  "placeholder:text-muted-foreground focus-visible:outline-none",
  "focus-visible:ring-2 focus-visible:ring-accent",
  "focus-visible:ring-offset-2 focus-visible:ring-offset-background",
  "disabled:cursor-not-allowed disabled:opacity-50",
);

const iconButtonClass = cn(
  "inline-flex h-11 w-11 items-center justify-center rounded-md border border-border",
  "text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent",
  "focus-visible:ring-offset-2 focus-visible:ring-offset-background",
  "disabled:cursor-not-allowed disabled:opacity-50",
);
