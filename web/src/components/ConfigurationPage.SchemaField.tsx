import { useState } from 'react'
import { cn } from '../lib/utils'
import {
  BACKEND_SECRET_MASK,
  FIELD_HELP_CLS,
  FIELD_LABEL_CLS,
  FORM_FIELD_CLS,
  FORM_SECTION_CLS,
  INPUT_CLS,
  SECRET_BADGE_CLS,
  SECTION_BODY_CLS,
  SECTION_BODY_COLLAPSED_CLS,
  SECTION_HEADER_CLS,
  SECTION_TITLE_CLS,
  SECTION_TOGGLE_CLS,
  SECTION_TOGGLE_OPEN_CLS,
  SELECT_CLS,
  TOGGLE_CLS,
  TOGGLE_ON_CLS,
  TOGGLE_ROW_CLS,
} from './ConfigurationPage.styles'
import {
  formatFieldName,
  getSchemaProperties,
  getSchemaType,
  isSecretField,
} from './ConfigurationPage.helpers'

interface SchemaFieldProps {
  name: string
  fieldSchema: Record<string, unknown>
  value: unknown
  onChange: (name: string, value: unknown) => void
  path: string
  secretKeys?: string[]
}

export function SchemaField({ name, fieldSchema, value, onChange, path, secretKeys = [] }: SchemaFieldProps) {
  const type = getSchemaType(fieldSchema)
  const description = fieldSchema.description as string | undefined
  const enumValues = fieldSchema.enum as string[] | undefined
  const fullPath = path ? `${path}.${name}` : name

  if (enumValues) {
    const hasEnumValues = enumValues.length > 0
    return (
      <div className={FORM_FIELD_CLS}>
        <label className={FIELD_LABEL_CLS}>{formatFieldName(name)}</label>
        {description && <span className={FIELD_HELP_CLS}>{description}</span>}
        <select
          className={SELECT_CLS}
          value={String(value ?? '')}
          disabled={!hasEnumValues}
          onChange={e => onChange(fullPath, e.target.value)}
        >
          {hasEnumValues ? (
            enumValues.map(v => (
              <option key={v} value={v}>{v}</option>
            ))
          ) : (
            <option value="">No options available</option>
          )}
        </select>
      </div>
    )
  }

  if (type === 'boolean') {
    return (
      <div className={FORM_FIELD_CLS}>
        <div className={TOGGLE_ROW_CLS}>
          <div>
            <div className={FIELD_LABEL_CLS}>{formatFieldName(name)}</div>
            {description && <span className={FIELD_HELP_CLS}>{description}</span>}
          </div>
          <button type="button"
            className={cn(TOGGLE_CLS, Boolean(value) && TOGGLE_ON_CLS)}
            onClick={() => onChange(fullPath, !value)}
            aria-label={`Toggle ${name}`}
          />
        </div>
      </div>
    )
  }

  if (type === 'integer' || type === 'number') {
    const min = fieldSchema.minimum as number | undefined
    const max = fieldSchema.maximum as number | undefined
    return (
      <div className={FORM_FIELD_CLS}>
        <label className={FIELD_LABEL_CLS}>{formatFieldName(name)}</label>
        {description && <span className={FIELD_HELP_CLS}>{description}</span>}
        <input
          type="number"
          className={INPUT_CLS}
          value={value != null ? String(value) : ''}
          min={min}
          max={max}
          step={type === 'number' ? 0.1 : 1}
          onChange={e => {
            const v = e.target.value.trim()
            if (v === '') {
              onChange(fullPath, null)
              return
            }
            const parsed = type === 'integer' ? parseInt(v, 10) : parseFloat(v)
            onChange(fullPath, Number.isFinite(parsed) ? parsed : null)
          }}
        />
      </div>
    )
  }

  const secret = isSecretField(fullPath, secretKeys)
  const isMasked = secret && value === BACKEND_SECRET_MASK
  return (
    <div className={FORM_FIELD_CLS}>
      <label className={FIELD_LABEL_CLS}>
        {formatFieldName(name)}
        {secret && <span className={SECRET_BADGE_CLS}>encrypted</span>}
      </label>
      {description && <span className={FIELD_HELP_CLS}>{description}</span>}
      <input
        type={secret ? 'password' : 'text'}
        className={INPUT_CLS}
        value={String(value ?? '')}
        placeholder={isMasked ? 'Enter new value to change' : undefined}
        onChange={e => onChange(fullPath, e.target.value)}
      />
    </div>
  )
}

interface SchemaSectionProps {
  name: string
  sectionSchema: Record<string, unknown>
  values: Record<string, unknown>
  onChange: (path: string, value: unknown) => void
  parentPath: string
  secretKeys?: string[]
}

export function SchemaSection({ name, sectionSchema, values, onChange, parentPath, secretKeys = [] }: SchemaSectionProps) {
  const [open, setOpen] = useState(false)
  const props = getSchemaProperties(sectionSchema)
  const description = sectionSchema.description as string | undefined
  const path = parentPath ? `${parentPath}.${name}` : name
  const bodyId = `schema-section-${path.replace(/[^a-zA-Z0-9_-]/g, '-')}`

  const sectionValues = (values || {}) as Record<string, unknown>

  return (
    <div className={FORM_SECTION_CLS}>
      <button
        type="button"
        className={SECTION_HEADER_CLS}
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        aria-controls={bodyId}
      >
        <div>
          <span className={SECTION_TITLE_CLS}>{formatFieldName(name)}</span>
          {description && <span className={cn(FIELD_HELP_CLS, 'ml-2')}>{description}</span>}
        </div>
        <span className={cn(SECTION_TOGGLE_CLS, open && SECTION_TOGGLE_OPEN_CLS)}>&#9654;</span>
      </button>
      <div id={bodyId} className={cn(SECTION_BODY_CLS, !open && SECTION_BODY_COLLAPSED_CLS)}>
        {Object.entries(props).map(([fieldName, fieldSchema]) => {
          const fs = fieldSchema as Record<string, unknown>
          const fieldType = getSchemaType(fs)

          if (fieldType === 'object' && fs.properties) {
            return (
              <SchemaSection
                key={fieldName}
                name={fieldName}
                sectionSchema={fs}
                values={(sectionValues[fieldName] || {}) as Record<string, unknown>}
                onChange={onChange}
                parentPath={path}
                secretKeys={secretKeys}
              />
            )
          }

          return (
            <SchemaField
              key={fieldName}
              name={fieldName}
              fieldSchema={fs}
              value={sectionValues[fieldName]}
              onChange={onChange}
              path={path}
              secretKeys={secretKeys}
            />
          )
        })}
      </div>
    </div>
  )
}
