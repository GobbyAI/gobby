import type { ReactNode } from 'react'
import { NumberField, SecretField, SwitchField, TextAreaField, TextField } from '../../activity/fields'
import type { FieldOption } from '../../activity/fields'
import {
  BoundedSelectField,
  KeyValueMapField,
  StringListField,
  TypedListField,
} from '../fields'
import { enumOptionsAt, numberBoundsAt, resolveSchemaNode } from '../configSchema'
import type { SettingsSectionFields } from './SettingsSection'
import {
  asMap,
  asNumber,
  asString,
  asStringList,
  asTypedList,
} from './configAccessors'

/**
 * Shared field wrappers for config-backed settings sections. Every section that
 * owns DaemonConfig dotted-paths renders its rows through these so behavior
 * (null coercion, schema bounds, enum options) stays identical across sections
 * instead of being re-derived per file. The draft-value accessors live beside
 * this file in `configAccessors.ts`.
 */

/**
 * A titled cluster of related rows. The section body spaces direct children by
 * 1.5rem, so each subsection reads as a distinct block without borders.
 */
export function Subsection({
  title,
  hint,
  children,
}: {
  title: string
  hint?: string
  children: ReactNode
}) {
  return (
    <section className="flex flex-col gap-4">
      <div className="flex flex-col gap-1">
        <h4 className="text-base font-semibold leading-[1.3] text-foreground">
          {title}
        </h4>
        {hint ? (
          <p className="max-w-[48ch] text-sm leading-[1.4] text-muted-foreground">
            {hint}
          </p>
        ) : null}
      </div>
      {children}
    </section>
  )
}

interface ConfigFieldBase {
  fields: SettingsSectionFields
  path: string
  label: string
  ariaLabel: string
  managedAction?: (value: unknown) => void
}

function activationAt(fields: SettingsSectionFields, path: string): string {
  const activation = resolveSchemaNode(fields.schema, path)?.activation
  return typeof activation === 'string' ? activation : 'live'
}

function displayValue(value: unknown): string {
  if (typeof value === 'string') return value
  if (value === undefined) return 'unset'
  return JSON.stringify(value)
}

function updateConfigField(
  fields: SettingsSectionFields,
  path: string,
  value: unknown,
  managedAction?: (value: unknown) => void,
): void {
  if (activationAt(fields, path) === 'managed') {
    managedAction?.(value)
    return
  }
  fields.setValue(path, value)
}

function ConfigFieldState({
  fields,
  path,
  children,
}: {
  fields: SettingsSectionFields
  path: string
  children: ReactNode
}) {
  const activation = activationAt(fields, path)
  const pending = fields.pendingRestartKeys?.includes(path) === true
  const failure = fields.failedLiveKeys?.[path]
  // A freshly typed secret sits unmasked in the draft until save; the status
  // strip must never echo it (or the stored active value) in plaintext.
  const secret = fields.secretKeys.includes(path)
    || resolveSchemaNode(fields.schema, path)?.secrecy === 'reference'
  const statusValue = (value: unknown): string =>
    secret && value !== undefined && value !== null ? '********' : displayValue(value)
  const label = activation === 'restart_required'
    ? 'Restart required'
    : activation === 'managed'
      ? 'Managed activation'
      : 'Live activation'
  return (
    <div className="settings-config-field" data-activation={activation} data-config-path={path}>
      {children}
      <div className="settings-config-field__status">
        <span>{label}</span>
        {pending ? (
          <>
            <span>{`Desired: ${statusValue(fields.getValue(path))}`}</span>
            <span>{`Active: ${statusValue(fields.getActiveValue?.(path))}`}</span>
          </>
        ) : null}
        {failure ? (
          <span role="status">
            Live apply failed in {failure.subscriber} at revision {failure.revision}
          </span>
        ) : null}
      </div>
    </div>
  )
}

/** A boolean config row backed by a toggle. */
export function SwitchConfigField({
  fields,
  path,
  label,
  ariaLabel,
  managedAction,
}: ConfigFieldBase) {
  return (
    <ConfigFieldState fields={fields} path={path}>
      <SwitchField
        label={label}
        ariaLabel={ariaLabel}
        value={fields.getValue(path) === true}
        onChange={(value) => updateConfigField(fields, path, value, managedAction)}
      />
    </ConfigFieldState>
  )
}

/** A numeric config row whose min/max come from the daemon schema. */
export function NumberConfigField({
  fields,
  path,
  label,
  ariaLabel,
  step,
  managedAction,
}: ConfigFieldBase & { step?: number }) {
  const bounds = numberBoundsAt(fields.schema, path)
  return (
    <ConfigFieldState fields={fields} path={path}>
      <NumberField
        label={label}
        ariaLabel={ariaLabel}
        value={asNumber(fields.getValue(path))}
        min={bounds.min}
        max={bounds.max}
        step={step}
        disabled={activationAt(fields, path) === 'managed' && !managedAction}
        onChange={(value) => updateConfigField(fields, path, value, managedAction)}
      />
    </ConfigFieldState>
  )
}

/**
 * A free-text config row. When `nullable`, an empty input writes `null` rather
 * than an empty string so optional daemon fields clear back to their default.
 */
export function TextConfigField({
  fields,
  path,
  label,
  ariaLabel,
  placeholder,
  nullable,
  managedAction,
}: ConfigFieldBase & { placeholder?: string; nullable?: boolean }) {
  return (
    <ConfigFieldState fields={fields} path={path}>
      <TextField
        label={label}
        ariaLabel={ariaLabel}
        value={asString(fields.getValue(path))}
        placeholder={placeholder}
        disabled={activationAt(fields, path) === 'managed' && !managedAction}
        onChange={(value) => updateConfigField(
          fields,
          path,
          nullable && value === '' ? null : value,
          managedAction,
        )}
      />
    </ConfigFieldState>
  )
}

/**
 * A masked secret row: a password input with a reveal toggle, bound to `path`.
 * Set secrets arrive masked as "********" from `/api/config/values`; submitting
 * that sentinel unchanged keeps the stored value server-side, while a typed
 * replacement overwrites it. `nullable` clears optional daemon fields back to
 * their default the same way `TextConfigField` does.
 */
export function SecretConfigField({
  fields,
  path,
  label,
  ariaLabel,
  placeholder,
  nullable,
  managedAction,
}: ConfigFieldBase & { placeholder?: string; nullable?: boolean }) {
  return (
    <ConfigFieldState fields={fields} path={path}>
      <SecretField
        label={label}
        ariaLabel={ariaLabel}
        value={asString(fields.getValue(path))}
        placeholder={placeholder}
        onChange={(value) => updateConfigField(
          fields,
          path,
          nullable && value === '' ? null : value,
          managedAction,
        )}
      />
    </ConfigFieldState>
  )
}

/** A select whose options are the schema enum at `path` (resolved through $ref). */
export function SchemaSelectField({
  fields,
  path,
  label,
  ariaLabel,
  managedAction,
}: ConfigFieldBase) {
  return (
    <ConfigFieldState fields={fields} path={path}>
      <BoundedSelectField
        label={label}
        ariaLabel={ariaLabel}
        value={asString(fields.getValue(path))}
        options={enumOptionsAt(fields.schema, path)}
        onChange={(value) => updateConfigField(fields, path, value, managedAction)}
      />
    </ConfigFieldState>
  )
}

/**
 * A select bound to an explicit option list, for daemon fields that are bounded
 * by convention (described in their docstring) but typed as plain strings in the
 * schema, so no enum is available. A stored value outside `options` is preserved
 * and surfaced by BoundedSelectField as "(unsupported)".
 */
export function OptionsSelectField({
  fields,
  path,
  label,
  ariaLabel,
  options,
  managedAction,
}: ConfigFieldBase & { options: FieldOption[] }) {
  return (
    <ConfigFieldState fields={fields} path={path}>
      <BoundedSelectField
        label={label}
        ariaLabel={ariaLabel}
        value={asString(fields.getValue(path))}
        options={options}
        onChange={(value) => updateConfigField(fields, path, value, managedAction)}
      />
    </ConfigFieldState>
  )
}

/**
 * A multi-line free-text config row, for long templates and prompts. When
 * `nullable`, an empty input writes `null` rather than an empty string so
 * optional daemon fields clear back to their default.
 */
export function TextAreaConfigField({
  fields,
  path,
  label,
  ariaLabel,
  placeholder,
  nullable,
  rows,
  managedAction,
}: ConfigFieldBase & { placeholder?: string; nullable?: boolean; rows?: number }) {
  return (
    <ConfigFieldState fields={fields} path={path}>
      <TextAreaField
        label={label}
        ariaLabel={ariaLabel}
        value={asString(fields.getValue(path))}
        placeholder={placeholder}
        rows={rows}
        onChange={(value) => updateConfigField(
          fields,
          path,
          nullable && value === '' ? null : value,
          managedAction,
        )}
      />
    </ConfigFieldState>
  )
}

/**
 * A `dict[str, str]` config row backed by a key-value editor. The daemon stores
 * the value as a plain object, so reads coerce through `asMap` and writes pass
 * the edited map straight back.
 */
export function MapConfigField({
  fields,
  path,
  label,
  ariaLabel,
  addLabel,
  keyPlaceholder,
  managedAction,
}: ConfigFieldBase & { addLabel?: string; keyPlaceholder?: string }) {
  return (
    <ConfigFieldState fields={fields} path={path}>
      <KeyValueMapField
        label={label}
        ariaLabel={ariaLabel}
        value={asMap<string>(fields.getValue(path))}
        addLabel={addLabel}
        keyPlaceholder={keyPlaceholder}
        onChange={(value) => updateConfigField(fields, path, value, managedAction)}
      />
    </ConfigFieldState>
  )
}

/**
 * A `dict[str, list[str]]` config row: a key/value map whose every value is an
 * ordered string list (the audit's `map<array>` "fix" rows, e.g. the task
 * expansion `pattern_criteria.*` templates and keyword sets). Reads coerce the
 * outer object through `asMap` and each value through `asStringList`.
 */
export function ListMapConfigField({
  fields,
  path,
  label,
  ariaLabel,
  valueLabel,
  addLabel,
  keyPlaceholder,
  itemAddLabel,
  managedAction,
}: ConfigFieldBase & {
  /** Label for each entry's nested string-list editor (its group name). */
  valueLabel: string
  addLabel?: string
  keyPlaceholder?: string
  itemAddLabel?: string
}) {
  return (
    <ConfigFieldState fields={fields} path={path}>
      <KeyValueMapField<string[]>
        label={label}
        ariaLabel={ariaLabel}
        value={asMap<string[]>(fields.getValue(path))}
        addLabel={addLabel}
        keyPlaceholder={keyPlaceholder}
        createValue={() => []}
        renderValue={(value, onValueChange, key) => (
          <StringListField
            label={valueLabel}
            ariaLabel={`${ariaLabel} ${key || 'new entry'} values`}
            value={asStringList(value)}
            addLabel={itemAddLabel}
            onChange={onValueChange}
          />
        )}
        onChange={(value) => updateConfigField(fields, path, value, managedAction)}
      />
    </ConfigFieldState>
  )
}

/**
 * A `list[int]` config row backed by an editable number list (the audit's
 * `array<integer>` "fix" rows, e.g. `cron.backoff_delays`). Each row is a
 * `NumberField`; a freshly added row seeds to 0.
 */
export function NumberListConfigField({
  fields,
  path,
  label,
  ariaLabel,
  addLabel,
  managedAction,
}: ConfigFieldBase & { addLabel?: string }) {
  return (
    <ConfigFieldState fields={fields} path={path}>
      <TypedListField<number>
        label={label}
        ariaLabel={ariaLabel}
        value={asTypedList<number>(fields.getValue(path))}
        addLabel={addLabel}
        createItem={() => 0}
        onChange={(value) => updateConfigField(fields, path, value, managedAction)}
        renderItem={(item, onItemChange, index) => (
          <NumberField
            label=""
            ariaLabel={`${ariaLabel} item ${index + 1}`}
            value={typeof item === 'number' ? item : null}
            onChange={(next) => onItemChange(next ?? 0)}
          />
        )}
      />
    </ConfigFieldState>
  )
}

/**
 * A `dict[str, number]` config row: a key/value map whose every value is a
 * number (the audit's `map<number>` "fix" rows, e.g. the MCP proxy
 * `tool_timeouts`). Reads coerce the outer object through `asMap` and each
 * value through `asNumber`; a freshly added entry seeds to 0.
 */
export function NumberMapConfigField({
  fields,
  path,
  label,
  ariaLabel,
  addLabel,
  keyPlaceholder,
  managedAction,
}: ConfigFieldBase & { addLabel?: string; keyPlaceholder?: string }) {
  return (
    <ConfigFieldState fields={fields} path={path}>
      <KeyValueMapField<number>
        label={label}
        ariaLabel={ariaLabel}
        value={asMap<number>(fields.getValue(path))}
        addLabel={addLabel}
        keyPlaceholder={keyPlaceholder}
        createValue={() => 0}
        renderValue={(value, onValueChange, key) => (
          <NumberField
            label=""
            ariaLabel={`${ariaLabel} ${key || 'new entry'} value`}
            value={asNumber(value)}
            onChange={(next) => onValueChange(next ?? 0)}
          />
        )}
        onChange={(value) => updateConfigField(fields, path, value, managedAction)}
      />
    </ConfigFieldState>
  )
}

/** A list-of-strings config row. */
export function StringListConfigField({
  fields,
  path,
  label,
  ariaLabel,
  addLabel,
  placeholder,
  managedAction,
}: ConfigFieldBase & { addLabel?: string; placeholder?: string }) {
  return (
    <ConfigFieldState fields={fields} path={path}>
      <StringListField
        label={label}
        ariaLabel={ariaLabel}
        value={asStringList(fields.getValue(path))}
        addLabel={addLabel}
        placeholder={placeholder}
        onChange={(value) => updateConfigField(fields, path, value, managedAction)}
      />
    </ConfigFieldState>
  )
}
