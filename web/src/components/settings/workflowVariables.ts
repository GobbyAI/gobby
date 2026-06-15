/**
 * Pure helpers for the workflow-variables editor, kept out of the component
 * file so it can export only components (react-refresh). Shared by
 * WorkflowVariablesEditor and its tests.
 */

/** Coerce a free-text default into the closest JSON-ish primitive, else string. */
export function parseVariableInput(raw: string): unknown {
  if (raw === 'true') return true
  if (raw === 'false') return false
  if (raw === 'null') return null
  if (raw === '[]') return []
  if (/^-?\d+$/.test(raw)) return Number.parseInt(raw, 10)
  if (/^-?\d+\.\d+$/.test(raw)) return Number.parseFloat(raw)
  return raw
}

/** Render a stored variable definition's `value` for the list display. */
export function variableDisplayValue(definitionJson: string): string {
  try {
    const parsed = JSON.parse(definitionJson) as { value?: unknown }
    const value = parsed.value
    if (value === null || value === undefined) return 'null'
    if (Array.isArray(value)) return JSON.stringify(value)
    return String(value)
  } catch {
    return '-'
  }
}
