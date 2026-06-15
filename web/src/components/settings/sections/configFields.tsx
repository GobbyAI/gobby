import type { ReactNode } from 'react'
import { NumberField, SwitchField, TextAreaField, TextField } from '../../activity/fields'
import type { FieldOption } from '../../activity/fields'
import { BoundedSelectField, KeyValueMapField, StringListField } from '../fields'
import { enumOptionsAt, numberBoundsAt } from '../configSchema'
import type { SettingsSectionFields } from './SettingsSection'
import { asMap, asNumber, asString, asStringList } from './configAccessors'

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
    <section className="settings-subsection">
      <div className="settings-subsection__head">
        <h4 className="settings-subsection__title">{title}</h4>
        {hint ? <p className="settings-field__hint">{hint}</p> : null}
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
}

/** A boolean config row backed by a toggle. */
export function SwitchConfigField({ fields, path, label, ariaLabel }: ConfigFieldBase) {
  return (
    <SwitchField
      label={label}
      ariaLabel={ariaLabel}
      value={fields.getValue(path) === true}
      onChange={(value) => fields.setValue(path, value)}
    />
  )
}

/** A numeric config row whose min/max come from the daemon schema. */
export function NumberConfigField({
  fields,
  path,
  label,
  ariaLabel,
  step,
}: ConfigFieldBase & { step?: number }) {
  const bounds = numberBoundsAt(fields.schema, path)
  return (
    <NumberField
      label={label}
      ariaLabel={ariaLabel}
      value={asNumber(fields.getValue(path))}
      min={bounds.min}
      max={bounds.max}
      step={step}
      onChange={(value) => fields.setValue(path, value)}
    />
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
}: ConfigFieldBase & { placeholder?: string; nullable?: boolean }) {
  return (
    <TextField
      label={label}
      ariaLabel={ariaLabel}
      value={asString(fields.getValue(path))}
      placeholder={placeholder}
      onChange={(value) => fields.setValue(path, nullable && value === '' ? null : value)}
    />
  )
}

/** A select whose options are the schema enum at `path` (resolved through $ref). */
export function SchemaSelectField({
  fields,
  path,
  label,
  ariaLabel,
}: ConfigFieldBase) {
  return (
    <BoundedSelectField
      label={label}
      ariaLabel={ariaLabel}
      value={asString(fields.getValue(path))}
      options={enumOptionsAt(fields.schema, path)}
      onChange={(value) => fields.setValue(path, value)}
    />
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
}: ConfigFieldBase & { options: FieldOption[] }) {
  return (
    <BoundedSelectField
      label={label}
      ariaLabel={ariaLabel}
      value={asString(fields.getValue(path))}
      options={options}
      onChange={(value) => fields.setValue(path, value)}
    />
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
}: ConfigFieldBase & { placeholder?: string; nullable?: boolean; rows?: number }) {
  return (
    <TextAreaField
      label={label}
      ariaLabel={ariaLabel}
      value={asString(fields.getValue(path))}
      placeholder={placeholder}
      rows={rows}
      onChange={(value) => fields.setValue(path, nullable && value === '' ? null : value)}
    />
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
}: ConfigFieldBase & { addLabel?: string; keyPlaceholder?: string }) {
  return (
    <KeyValueMapField
      label={label}
      ariaLabel={ariaLabel}
      value={asMap<string>(fields.getValue(path))}
      addLabel={addLabel}
      keyPlaceholder={keyPlaceholder}
      onChange={(value) => fields.setValue(path, value)}
    />
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
}: ConfigFieldBase & { addLabel?: string; placeholder?: string }) {
  return (
    <StringListField
      label={label}
      ariaLabel={ariaLabel}
      value={asStringList(fields.getValue(path))}
      addLabel={addLabel}
      placeholder={placeholder}
      onChange={(value) => fields.setValue(path, value)}
    />
  )
}
