/**
 * Pure helpers for the variable-defaults editor, kept out of the component
 * file so it can export only components (react-refresh). Shared by
 * VariableDefaultsEditor and its tests.
 */

/** Coerce a free-text default into the closest JSON-ish primitive, else string. */
export function parseVariableInput(raw: string): unknown {
  if (raw === "true") return true;
  if (raw === "false") return false;
  if (raw === "null") return null;
  if (raw === "[]") return [];
  if (/^-?\d+$/.test(raw)) return Number.parseInt(raw, 10);
  if (/^-?\d+\.\d+$/.test(raw)) return Number.parseFloat(raw);
  return raw;
}

/** Render a stored variable default_value for the list display. */
export function variableDisplayValue(defaultValue: unknown): string {
  if (defaultValue === null || defaultValue === undefined) return "null";
  if (typeof defaultValue === "object") return JSON.stringify(defaultValue);
  return String(defaultValue);
}
