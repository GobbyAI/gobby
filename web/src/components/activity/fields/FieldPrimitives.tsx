import { useId, useState, type KeyboardEvent } from "react";

import { cn } from "../../../lib/utils";
import type { DraftFieldBaseProps, FieldOption } from "./types";

interface TextFieldProps extends DraftFieldBaseProps {
  value: string;
  onChange: (value: string) => void;
}

interface NumberFieldProps extends DraftFieldBaseProps {
  value: number | null;
  onChange: (value: number | null) => void;
  min?: number;
  max?: number;
  step?: number;
}

interface TextAreaFieldProps extends DraftFieldBaseProps {
  value: string;
  onChange: (value: string) => void;
  rows?: number;
}

interface SelectFieldProps extends DraftFieldBaseProps {
  value: string;
  onChange: (value: string) => void;
  options: FieldOption[];
}

interface TagsFieldProps extends DraftFieldBaseProps {
  value: string[];
  onChange: (value: string[]) => void;
}

const fieldShellClass = "flex flex-col gap-1.5";
const labelClass = "text-xs font-medium text-muted-foreground";
const controlClass = cn(
  "min-h-11 rounded-md border border-border bg-[var(--bg-secondary)] px-3 py-2",
  "text-sm text-foreground transition-colors placeholder:text-muted-foreground",
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent",
  "focus-visible:ring-offset-2 focus-visible:ring-offset-background",
  "disabled:cursor-not-allowed disabled:opacity-50",
);

export function TextField({
  label,
  value,
  onChange,
  disabled,
  placeholder,
  ariaLabel,
}: TextFieldProps) {
  const id = useId();

  return (
    <label className={fieldShellClass} htmlFor={id}>
      <span className={labelClass}>{label}</span>
      <input
        id={id}
        type="text"
        className={controlClass}
        aria-label={ariaLabel}
        value={value}
        disabled={disabled}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}

export function NumberField({
  label,
  value,
  onChange,
  disabled,
  placeholder,
  ariaLabel,
  min,
  max,
  step,
}: NumberFieldProps) {
  const id = useId();

  return (
    <label className={fieldShellClass} htmlFor={id}>
      <span className={labelClass}>{label}</span>
      <input
        id={id}
        type="number"
        className={controlClass}
        aria-label={ariaLabel}
        value={value ?? ""}
        min={min}
        max={max}
        step={step}
        disabled={disabled}
        placeholder={placeholder}
        onChange={(event) => {
          const raw = event.target.value.trim();
          if (raw === "") {
            onChange(null);
            return;
          }
          const parsed = Number(raw);
          onChange(Number.isFinite(parsed) ? parsed : null);
        }}
      />
    </label>
  );
}

export function TextAreaField({
  label,
  value,
  onChange,
  disabled,
  placeholder,
  ariaLabel,
  rows = 4,
}: TextAreaFieldProps) {
  const id = useId();

  return (
    <label className={fieldShellClass} htmlFor={id}>
      <span className={labelClass}>{label}</span>
      <textarea
        id={id}
        className={cn(controlClass, "min-h-24 resize-y")}
        aria-label={ariaLabel}
        value={value}
        disabled={disabled}
        placeholder={placeholder}
        rows={rows}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}

export function SelectField({
  label,
  value,
  onChange,
  disabled,
  ariaLabel,
  options,
}: SelectFieldProps) {
  const id = useId();

  return (
    <label className={fieldShellClass} htmlFor={id}>
      <span className={labelClass}>{label}</span>
      <select
        id={id}
        className={controlClass}
        aria-label={ariaLabel}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      >
        {options.map((option) => (
          <option
            key={option.value}
            value={option.value}
            disabled={option.disabled}
          >
            {option.label}
          </option>
        ))}
      </select>
    </label>
  );
}

export function TagsField({
  label,
  value,
  onChange,
  disabled,
  ariaLabel,
  placeholder,
}: TagsFieldProps) {
  const [entry, setEntry] = useState("");
  const inputId = useId();

  function addEntry() {
    const tag = entry.trim();
    setEntry("");
    if (!tag || value.includes(tag)) return;
    onChange([...value, tag]);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter" || event.key === ",") {
      event.preventDefault();
      addEntry();
    } else if (event.key === "Backspace" && entry === "" && value.length > 0) {
      event.preventDefault();
      onChange(value.slice(0, -1));
    }
  }

  return (
    <div className={fieldShellClass}>
      <span className={labelClass}>{label}</span>
      <div
        role="group"
        aria-label={ariaLabel}
        className={cn(
          "flex min-h-11 flex-wrap items-center gap-2 rounded-md border",
          "border-border bg-[var(--bg-secondary)] px-2 py-1.5",
          disabled && "opacity-50",
        )}
      >
        {value.map((tag) => (
          <span
            key={tag}
            className="inline-flex min-h-8 items-center gap-1 rounded-md bg-muted px-2 text-xs text-foreground"
          >
            {tag}
            <button
              type="button"
              className={cn(
                "inline-flex h-11 w-11 items-center justify-center rounded text-muted-foreground",
                "hover:bg-[var(--bg-tertiary)] hover:text-foreground",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent",
              )}
              aria-label={`Remove ${tag}`}
              disabled={disabled}
              onClick={() => onChange(value.filter((item) => item !== tag))}
            >
              x
            </button>
          </span>
        ))}
        <input
          id={inputId}
          type="text"
          className={cn(
            "min-h-11 min-w-24 flex-1 bg-transparent px-1 text-sm text-foreground",
            "placeholder:text-muted-foreground focus-visible:outline-none",
          )}
          aria-label={`Add ${label}`}
          value={entry}
          disabled={disabled}
          placeholder={placeholder}
          onChange={(event) => setEntry(event.target.value)}
          onKeyDown={handleKeyDown}
          onBlur={addEntry}
        />
      </div>
    </div>
  );
}
