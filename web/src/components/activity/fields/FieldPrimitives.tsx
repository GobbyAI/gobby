import { useState, type KeyboardEvent } from "react";

import { cn } from "../../../lib/utils";
import { Button } from "../../ui/Button";
import { FormField } from "../../ui/FormField";
import { Input } from "../../ui/Input";
import { NativeSelect } from "../../ui/NativeSelect";
import { Textarea } from "../../ui/Textarea";
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

export function TextField({
  label,
  value,
  onChange,
  disabled,
  placeholder,
  ariaLabel,
}: TextFieldProps) {
  return (
    <FormField label={label}>
      {({ id }) => (
        <Input
          id={id}
          type="text"
          aria-label={ariaLabel}
          value={value}
          disabled={disabled}
          placeholder={placeholder}
          onChange={(event) => onChange(event.target.value)}
        />
      )}
    </FormField>
  );
}

export function SecretField({
  label,
  value,
  onChange,
  disabled,
  placeholder,
  ariaLabel,
}: TextFieldProps) {
  const [revealed, setRevealed] = useState(false);

  return (
    <FormField label={label}>
      {({ id }) => (
        <div className="relative">
          <Input
            id={id}
            type={revealed ? "text" : "password"}
            className="pr-16"
            aria-label={ariaLabel}
            value={value}
            disabled={disabled}
            placeholder={placeholder}
            autoComplete="off"
            spellCheck={false}
            onChange={(event) => onChange(event.target.value)}
          />
          <Button
            type="button"
            variant="ghost"
            size="sm"
            dense
            className="absolute inset-y-0 right-0 rounded-l-none px-3"
            aria-label={revealed ? `Hide ${ariaLabel}` : `Show ${ariaLabel}`}
            aria-pressed={revealed}
            disabled={disabled}
            onClick={() => setRevealed((current) => !current)}
          >
            {revealed ? "Hide" : "Show"}
          </Button>
        </div>
      )}
    </FormField>
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
  return (
    <FormField label={label}>
      {({ id }) => (
        <Input
          id={id}
          type="number"
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
      )}
    </FormField>
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
  return (
    <FormField label={label}>
      {({ id }) => (
        /* No resize grip, no scrollbars: content soft-wraps and the field
           auto-grows to fit (field-sizing) — the pane scrolls, not the box. */
        <Textarea
          id={id}
          className="min-h-24 resize-none [field-sizing:content]"
          aria-label={ariaLabel}
          value={value}
          disabled={disabled}
          placeholder={placeholder}
          rows={rows}
          onChange={(event) => onChange(event.target.value)}
        />
      )}
    </FormField>
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
  return (
    <FormField label={label}>
      {({ id }) => (
        <NativeSelect
          id={id}
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
        </NativeSelect>
      )}
    </FormField>
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
    <FormField label={label} group>
      {() => (
        <div
          role="group"
          aria-label={ariaLabel}
          className={cn(
            "flex flex-wrap items-center gap-1.5 rounded-md border",
            "border-border bg-[var(--bg-secondary)] px-2 py-1.5",
            disabled && "opacity-50",
          )}
        >
          {value.map((tag) => (
            <span
              key={tag}
              className="inline-flex h-5 items-center gap-1 rounded-full bg-accent-tint px-2 text-2xs font-semibold text-accent pointer-coarse:min-h-11 pointer-coarse:min-w-11"
            >
              {tag}
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className={cn(
                  "min-h-5 w-5 rounded-full text-muted-foreground",
                  "hover:text-foreground",
                )}
                aria-label={`Remove ${tag}`}
                disabled={disabled}
                onClick={() => onChange(value.filter((item) => item !== tag))}
              >
                ×
              </Button>
            </span>
          ))}
          {/* Naked inline entry: the group box is the visual boundary, so the
              control's own border, radius, and focus ring are stripped. */}
          <Input
            type="text"
            wrapperClassName="min-w-24 flex-1"
            className="h-auto min-h-7 rounded-none border-0 px-1 py-0 text-foreground focus-visible:ring-0 focus-visible:ring-offset-0"
            aria-label={`Add ${label}`}
            value={entry}
            disabled={disabled}
            placeholder={placeholder}
            onChange={(event) => setEntry(event.target.value)}
            onKeyDown={handleKeyDown}
            onBlur={addEntry}
          />
        </div>
      )}
    </FormField>
  );
}
