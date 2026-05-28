import { useCallback, useId } from 'react'
import { Input } from '../chat/ui/Input'
import { cn } from '../../lib/utils'

interface ToolArgumentFormProps {
  schema: Record<string, unknown> | null
  values: Record<string, unknown>
  onChange: (values: Record<string, unknown>) => void
  disabled?: boolean
}

interface PropertySchema {
  type?: string
  description?: string
  enum?: string[]
  default?: unknown
  items?: Record<string, unknown>
}

export function ToolArgumentForm({ schema, values, onChange, disabled }: ToolArgumentFormProps) {
  if (!schema) return null

  const properties = (schema.properties ?? {}) as Record<string, PropertySchema>
  const required = (schema.required ?? []) as string[]
  const entries = Object.entries(properties)

  if (entries.length === 0) {
    return <p className="text-sm text-muted-foreground italic">This tool takes no arguments.</p>
  }

  return (
    <div className="flex flex-col gap-3">
      {entries.map(([key, prop]) => (
        <FieldRow
          key={key}
          name={key}
          prop={prop}
          value={values[key]}
          isRequired={required.includes(key)}
          disabled={disabled}
          onChange={(val) => onChange({ ...values, [key]: val })}
        />
      ))}
    </div>
  )
}

function FieldRow({
  name,
  prop,
  value,
  isRequired,
  disabled,
  onChange,
}: {
  name: string
  prop: PropertySchema
  value: unknown
  isRequired: boolean
  disabled?: boolean
  onChange: (val: unknown) => void
}) {
  const inputId = useId()
  const handleChange = useCallback(
    (val: unknown) => onChange(val),
    [onChange],
  )

  const label = (
    <label htmlFor={inputId} className="block text-sm font-medium text-foreground mb-1">
      {name}
      {isRequired && <span className="text-destructive-foreground ml-0.5">*</span>}
    </label>
  )

  const help = prop.description ? (
    <p className="text-xs text-muted-foreground mt-0.5">{prop.description}</p>
  ) : null

  // String with enum -> select
  if (prop.type === 'string' && prop.enum) {
    return (
      <div>
        {label}
        <select
          id={inputId}
          className={cn(
            'flex h-9 w-full rounded-md border border-border bg-transparent px-3 py-1 text-sm',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
            'disabled:cursor-not-allowed disabled:opacity-50',
          )}
          value={(value as string) ?? ''}
          onChange={(e) => handleChange(e.target.value || undefined)}
          disabled={disabled}
        >
          <option value="">-- select --</option>
          {prop.enum.map((opt) => (
            <option key={opt} value={opt}>{opt}</option>
          ))}
        </select>
        {help}
      </div>
    )
  }

  // Boolean -> checkbox
  if (prop.type === 'boolean') {
    return (
      <div>
        <label className="flex items-center gap-2 text-sm font-medium text-foreground cursor-pointer">
          <input
            type="checkbox"
            checked={!!value}
            onChange={(e) => handleChange(e.target.checked)}
            disabled={disabled}
            className="h-4 w-4 rounded border-border accent-accent"
          />
          {name}
          {isRequired && <span className="text-destructive-foreground">*</span>}
        </label>
        {help}
      </div>
    )
  }

  // Number / integer
  if (prop.type === 'number' || prop.type === 'integer') {
    return (
      <div>
        {label}
        <Input
          id={inputId}
          type="number"
          value={value !== undefined && value !== null ? String(value) : ''}
          onChange={(e) => {
            const raw = e.target.value
            if (!raw) { handleChange(undefined); return }
            handleChange(prop.type === 'integer' ? parseInt(raw, 10) : parseFloat(raw))
          }}
          step={prop.type === 'integer' ? 1 : 'any'}
          disabled={disabled}
          placeholder={prop.default !== undefined ? `Default: ${prop.default}` : undefined}
        />
        {help}
      </div>
    )
  }

  // Object / array -> JSON textarea
  if (prop.type === 'object' || prop.type === 'array') {
    const strValue = value !== undefined && value !== null
      ? (typeof value === 'string' ? value : JSON.stringify(value, null, 2))
      : ''
    return (
      <div>
        {label}
        <textarea
          id={inputId}
          className={cn(
            'flex w-full rounded-md border border-border bg-transparent px-3 py-2 text-sm font-mono',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
            'disabled:cursor-not-allowed disabled:opacity-50',
            'min-h-[80px] resize-y',
          )}
          value={strValue}
          onChange={(e) => {
            const raw = e.target.value
            try {
              handleChange(JSON.parse(raw))
            } catch {
              handleChange(raw)
            }
          }}
          disabled={disabled}
          placeholder={`JSON ${prop.type}`}
        />
        {help}
      </div>
    )
  }

  // Default: string input
  return (
    <div>
      {label}
      <Input
        id={inputId}
        type="text"
        value={(value as string) ?? ''}
        onChange={(e) => handleChange(e.target.value || undefined)}
        disabled={disabled}
        placeholder={prop.default !== undefined ? `Default: ${prop.default}` : undefined}
      />
      {help}
    </div>
  )
}
