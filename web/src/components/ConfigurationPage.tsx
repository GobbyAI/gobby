import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useConfiguration } from '../hooks/useConfiguration'
import type { SecretInfo, PromptInfo, PromptDetail } from '../hooks/useConfiguration'
import { CodeMirrorEditor } from './shared/CodeMirrorEditor'
import { cn } from '../lib/utils'

type TabId = 'config' | 'approvals' | 'secrets' | 'prompts' | 'variables' | 'template'
type ApprovalRuleRow = { id: string; value: string }

const BACKEND_SECRET_MASK = '********'

const PAGE_CLS = 'flex flex-1 flex-col overflow-hidden'
const TOOLBAR_CLS =
  'flex min-h-11 items-center justify-between gap-3 border-b border-[var(--border)] bg-[var(--bg-secondary)] px-4 py-2 max-md:flex-wrap max-md:px-3'
const TOOLBAR_LEFT_CLS = 'flex min-w-0 flex-[1_1_0] items-center gap-3 overflow-hidden'
const TOOLBAR_RIGHT_CLS = 'flex shrink-0 items-center gap-2'
const TABS_CLS =
  'flex min-w-0 gap-0.5 overflow-x-auto rounded-md bg-[var(--bg-tertiary)] p-0.5 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden'
const TAB_CLS =
  'cursor-pointer whitespace-nowrap rounded border-0 bg-transparent px-3.5 py-1.5 text-[length:var(--text-md)] font-medium text-[var(--text-secondary)] transition-all duration-150 hover:bg-[rgba(255,255,255,0.05)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11'
const TAB_ACTIVE_CLS = 'bg-[var(--bg-secondary)] text-[var(--text-primary)] shadow-[var(--shadow-sm)]'

const TOOLBAR_BTN_CLS =
  'flex cursor-pointer items-center gap-1.5 rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-1.5 text-[length:var(--text-sm)] text-[var(--text-secondary)] transition-all duration-150 hover:border-[var(--border-active)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11'
const TOOLBAR_BTN_PRIMARY_CLS =
  'border-[var(--accent)] bg-[var(--accent)] text-[var(--accent-foreground)] hover:border-[var(--accent)] hover:bg-[var(--accent)] hover:text-[var(--accent-foreground)] hover:opacity-90'
const TOOLBAR_BTN_DANGER_CLS =
  'border-[color-mix(in_srgb,var(--color-error)_20%,transparent)] text-[var(--color-error)] hover:border-[color-mix(in_srgb,var(--color-error)_40%,transparent)] hover:bg-[color-mix(in_srgb,var(--color-error)_8%,transparent)] hover:text-[var(--color-error)]'

const CONTENT_CLS = 'flex-1 overflow-y-auto'

const RESTART_BANNER_CLS =
  'flex items-center justify-between border-b border-[color-mix(in_srgb,var(--color-warning-foreground)_20%,transparent)] bg-[color-mix(in_srgb,var(--color-warning-foreground)_8%,transparent)] px-4 py-2.5 text-[length:var(--text-md)] text-[var(--color-warning-foreground)]'
const RESTART_BTN_CLS =
  'cursor-pointer rounded border-0 bg-[var(--color-warning-foreground)] px-3 py-1 text-[length:var(--text-sm)] font-semibold text-[var(--text-on-warning)] pointer-coarse:min-h-11'

const FORM_CLS = 'max-w-[800px] p-4 max-md:p-3'
const FORM_SECTION_CLS = 'mb-5 overflow-hidden rounded-lg border border-[var(--border)]'
const SECTION_HEADER_CLS =
  'flex cursor-pointer select-none items-center justify-between border-b border-[var(--border)] bg-[var(--bg-secondary)] px-3.5 py-2.5 hover:bg-[var(--bg-tertiary)]'
const SECTION_HEADER_STATIC_CLS =
  'flex select-none items-center justify-between border-b border-[var(--border)] bg-[var(--bg-secondary)] px-3.5 py-2.5'
const SECTION_TITLE_CLS = 'text-[length:var(--text-base)] font-semibold text-[var(--text-primary)]'
const SECTION_TOGGLE_CLS =
  'text-[length:var(--text-xs)] text-[var(--text-tertiary)] transition-transform duration-200'
const SECTION_TOGGLE_OPEN_CLS = 'rotate-90'
const SECTION_BODY_CLS = 'flex flex-col gap-3 px-3.5 py-3'
const SECTION_BODY_COLLAPSED_CLS = 'hidden'

const FORM_FIELD_CLS = 'flex flex-col gap-1'
const FIELD_LABEL_CLS = 'text-[length:var(--text-md)] font-medium text-[var(--text-primary)]'
const FIELD_HELP_CLS = 'text-[length:var(--text-xs)] leading-[1.4] text-[var(--text-tertiary)]'
const INPUT_CLS =
  'rounded border border-[var(--border)] bg-[var(--bg-primary)] px-2.5 py-1.5 font-mono text-[length:var(--text-md)] text-[var(--text-primary)] outline-none focus:border-[var(--accent)] pointer-coarse:min-h-11'
const SELECT_CLS =
  'rounded border border-[var(--border)] bg-[var(--bg-primary)] px-2.5 py-1.5 text-[length:var(--text-md)] text-[var(--text-primary)] outline-none focus:border-[var(--accent)] pointer-coarse:min-h-11'

const TOGGLE_ROW_CLS = 'flex items-center justify-between py-1'
const TOGGLE_CLS =
  'relative h-5 w-9 shrink-0 cursor-pointer rounded-[10px] border-0 bg-[var(--bg-tertiary)] transition-colors duration-200 after:absolute after:left-0.5 after:top-0.5 after:h-4 after:w-4 after:rounded-full after:bg-[var(--text-primary)] after:transition-transform after:duration-200 after:content-[""] pointer-coarse:h-11 pointer-coarse:w-[88px] pointer-coarse:rounded-[22px] pointer-coarse:after:h-10 pointer-coarse:after:w-10'
const TOGGLE_ON_CLS = 'bg-[var(--accent)] after:translate-x-4 pointer-coarse:after:translate-x-[44px]'

const FORM_FOOTER_CLS =
  'sticky bottom-0 flex justify-end gap-2 border-t border-[var(--border)] bg-[var(--bg-secondary)] px-4 py-3'

const SECRET_BADGE_CLS =
  'ml-1.5 inline-block rounded-sm bg-[var(--bg-tertiary)] px-1.5 py-px align-middle text-[length:var(--text-2xs)] font-medium text-[var(--text-tertiary)]'

const SECRETS_CLS = 'max-w-[800px] p-4 max-md:p-3'
const SECRETS_HEADER_CLS = 'mb-4 flex items-center justify-between'
const SECRETS_HEADER_H3_CLS = 'm-0 text-[length:var(--text-base)] font-semibold'

const SECRETS_TABLE_CLS =
  'w-full border-collapse text-[length:var(--text-md)] max-md:text-[length:var(--text-sm)] max-sm:block [&_thead]:max-sm:hidden [&_tbody]:max-sm:block [&_tr]:max-sm:mb-2 [&_tr]:max-sm:block [&_tr]:max-sm:rounded-md [&_tr]:max-sm:border [&_tr]:max-sm:border-[var(--border)] [&_tr]:max-sm:bg-[var(--bg-secondary)] [&_tr]:max-sm:px-2.5 [&_tr]:max-sm:py-2 [&_td]:max-sm:block [&_td]:max-sm:border-b-0 [&_td]:max-sm:px-0 [&_td]:max-sm:py-1 [&_td]:max-sm:before:mb-0.5 [&_td]:max-sm:before:block [&_td]:max-sm:before:text-[length:var(--text-xs)] [&_td]:max-sm:before:uppercase [&_td]:max-sm:before:tracking-[0.5px] [&_td]:max-sm:before:text-[var(--text-tertiary)] [&_td]:max-sm:before:[content:attr(data-label)]'
const SECRETS_TH_CLS =
  'border-b border-[var(--border)] px-2.5 py-2 text-left text-[length:var(--text-xs)] font-medium uppercase tracking-[0.5px] text-[var(--text-tertiary)] max-md:px-1.5 max-md:py-1.5'
const SECRETS_TD_CLS = 'border-b border-[var(--border)] px-2.5 py-2 text-[var(--text-primary)] max-md:px-1.5 max-md:py-1.5'

const SECRET_MASKED_CLS = 'text-[length:var(--text-sm)] italic text-[var(--text-tertiary)]'
const SECRET_ACTIONS_CLS = 'flex gap-1.5 max-sm:flex-wrap'
const SECRET_ACTION_BTN_CLS =
  'cursor-pointer rounded-sm border border-[var(--border)] bg-transparent px-2 py-0.5 text-[length:var(--text-xs)] text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11'
const SECRET_ACTION_DELETE_CLS =
  'hover:border-[color-mix(in_srgb,var(--color-error)_40%,transparent)] hover:text-[var(--color-error)]'

const SECRET_HINT_CLS =
  'mt-4 rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-3.5 py-2.5 text-[length:var(--text-sm)] leading-[1.5] text-[var(--text-secondary)] [&_code]:rounded-sm [&_code]:bg-[var(--bg-tertiary)] [&_code]:px-1.5 [&_code]:py-px [&_code]:font-mono [&_code]:text-[length:var(--text-xs)]'

const SECRET_FORM_CLS =
  'mb-4 flex flex-col gap-2.5 rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] p-3.5'
const SECRET_FORM_ROW_CLS = 'flex gap-2.5 max-sm:flex-col [&>*]:flex-1'
const SECRET_FORM_ACTIONS_CLS = 'flex justify-end gap-2'

const PROMPTS_CLS = 'flex flex-1 overflow-hidden max-sm:flex-col'
const PROMPTS_SIDEBAR_CLS =
  'flex w-[220px] min-w-[220px] flex-col overflow-y-auto border-r border-[var(--border)] bg-[var(--bg-secondary)] max-sm:w-full max-sm:min-w-0 max-sm:flex-row max-sm:overflow-x-auto max-sm:border-b max-sm:border-r-0 max-sm:border-b-[var(--border)]'
const PROMPTS_SIDEBAR_TITLE_CLS =
  'border-b border-[var(--border)] px-3.5 py-2.5 text-[length:var(--text-sm)] font-semibold uppercase tracking-[0.5px] text-[var(--text-tertiary)] max-sm:hidden'
const PROMPT_CATEGORY_CLS =
  'flex cursor-pointer items-center justify-between px-3.5 py-2 text-[length:var(--text-md)] text-[var(--text-secondary)] transition-all duration-100 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] max-sm:whitespace-nowrap max-sm:px-3 pointer-coarse:min-h-11'
const PROMPT_CATEGORY_ACTIVE_CLS = 'bg-[var(--bg-tertiary)] font-medium text-[var(--text-primary)]'
const PROMPT_CATEGORY_COUNT_CLS =
  'rounded-[10px] bg-[var(--bg-primary)] px-1.5 py-px text-[length:var(--text-xs)] text-[var(--text-tertiary)]'

const PROMPTS_MAIN_CLS = 'flex flex-1 flex-col overflow-hidden'
const PROMPTS_LIST_CLS = 'flex flex-1 flex-col gap-1.5 overflow-y-auto p-3'
const PROMPT_CARD_CLS =
  'flex cursor-pointer items-center justify-between rounded-md border border-[var(--border)] px-3 py-2 transition-all duration-100 hover:border-[var(--border-active)] hover:bg-[var(--bg-secondary)]'
const PROMPT_CARD_NAME_CLS = 'text-[length:var(--text-md)] font-medium text-[var(--text-primary)]'
const PROMPT_CARD_DESC_CLS = 'mt-0.5 text-[length:var(--text-xs)] text-[var(--text-tertiary)]'

const PROMPT_BADGE_CLS =
  'shrink-0 rounded-[10px] px-2 py-0.5 text-[length:var(--text-2xs)] font-semibold uppercase tracking-[0.3px]'
const PROMPT_BADGE_BG: Record<string, string> = {
  bundled:
    'bg-[color-mix(in_srgb,var(--color-success-foreground)_8%,transparent)] text-[var(--color-success-foreground)]',
  overridden:
    'bg-[color-mix(in_srgb,var(--color-warning-foreground)_8%,transparent)] text-[var(--color-warning-foreground)]',
}

const PROMPT_DETAIL_CLS = 'flex flex-1 flex-col overflow-hidden'
const PROMPT_DETAIL_HEADER_CLS =
  'flex items-center justify-between border-b border-[var(--border)] px-3.5 py-2.5 max-sm:flex-col max-sm:items-start max-sm:gap-2'
const PROMPT_DETAIL_TITLE_CLS = 'text-[length:var(--text-base)] font-semibold'
const PROMPT_DETAIL_ACTIONS_CLS = 'flex gap-1.5'
const PROMPT_EDITOR_CLS = 'flex-1 overflow-hidden [&_.codemirror-container]:h-full'
const PROMPT_EMPTY_CLS = 'flex flex-1 items-center justify-center text-[length:var(--text-md)] text-[var(--text-tertiary)]'

const YAML_CLS = 'flex flex-1 flex-col overflow-hidden'
const YAML_EDITOR_CLS = 'flex-1 overflow-hidden [&_.codemirror-container]:h-full'
const YAML_FOOTER_CLS =
  'flex items-center justify-between border-t border-[var(--border)] bg-[var(--bg-secondary)] px-4 py-2'
const YAML_ERRORS_CLS = 'text-[length:var(--text-sm)] text-[var(--color-error)]'

const EMPTY_CLS = 'flex flex-1 items-center justify-center text-[length:var(--text-base)] text-[var(--text-tertiary)]'
const LOADING_CLS = 'flex flex-1 items-center justify-center text-[length:var(--text-md)] text-[var(--text-tertiary)]'

const ERRORS_CLS = 'mb-3 text-[length:var(--text-sm)] text-[var(--color-error)]'

function createApprovalRuleRow(value = ''): ApprovalRuleRow {
  const id =
    typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? crypto.randomUUID()
      : `project-approval-rule-${Date.now()}-${Math.random().toString(36).slice(2)}`
  return { id, value }
}

function toApprovalRuleRows(rules: string[]): ApprovalRuleRow[] {
  return rules.map((rule) => createApprovalRuleRow(rule))
}

const SECRET_PATTERNS = ['api_key', 'api_token', 'api_secret', 'password', 'access_token', 'auth_token', 'secret_key', 'secret', 'credentials', 'private_key', 'client_secret']

function isSecretField(path: string, secretKeys: string[]): boolean {
  if (secretKeys.includes(path)) return true
  const last = path.split('.').pop() || ''
  return SECRET_PATTERNS.some(p => last.includes(p))
}

function formatFieldName(name: string): string {
  return name
    .replace(/_/g, ' ')
    .replace(/-/g, ' ')
    .replace(/\b\w/g, c => c.toUpperCase())
}

function getSchemaProperties(schema: Record<string, unknown>): Record<string, unknown> {
  const props = schema.properties as Record<string, unknown> | undefined
  return props || {}
}

function getSchemaType(fieldSchema: Record<string, unknown>): string {
  if (fieldSchema.anyOf) {
    const types = (fieldSchema.anyOf as Record<string, unknown>[])
      .map(t => t.type as string)
      .filter(t => t !== 'null')
    return types[0] || 'string'
  }
  return (fieldSchema.type as string) || 'string'
}

interface SchemaFieldProps {
  name: string
  fieldSchema: Record<string, unknown>
  value: unknown
  onChange: (name: string, value: unknown) => void
  path: string
  secretKeys?: string[]
}

function SchemaField({ name, fieldSchema, value, onChange, path, secretKeys = [] }: SchemaFieldProps) {
  const type = getSchemaType(fieldSchema)
  const description = fieldSchema.description as string | undefined
  const enumValues = fieldSchema.enum as string[] | undefined
  const fullPath = path ? `${path}.${name}` : name

  if (enumValues) {
    return (
      <div className={FORM_FIELD_CLS}>
        <label className={FIELD_LABEL_CLS}>{formatFieldName(name)}</label>
        {description && <span className={FIELD_HELP_CLS}>{description}</span>}
        <select
          className={SELECT_CLS}
          value={String(value ?? '')}
          onChange={e => onChange(fullPath, e.target.value)}
        >
          {enumValues.map(v => (
            <option key={v} value={v}>{v}</option>
          ))}
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
            const v = e.target.value
            onChange(fullPath, v === '' ? null : type === 'integer' ? parseInt(v, 10) : parseFloat(v))
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

function SchemaSection({ name, sectionSchema, values, onChange, parentPath, secretKeys = [] }: SchemaSectionProps) {
  const [open, setOpen] = useState(false)
  const props = getSchemaProperties(sectionSchema)
  const description = sectionSchema.description as string | undefined
  const path = parentPath ? `${parentPath}.${name}` : name

  const sectionValues = (values || {}) as Record<string, unknown>

  return (
    <div className={FORM_SECTION_CLS}>
      <div className={SECTION_HEADER_CLS} onClick={() => setOpen(!open)}>
        <div>
          <span className={SECTION_TITLE_CLS}>{formatFieldName(name)}</span>
          {description && <span className={cn(FIELD_HELP_CLS, 'ml-2')}>{description}</span>}
        </div>
        <span className={cn(SECTION_TOGGLE_CLS, open && SECTION_TOGGLE_OPEN_CLS)}>&#9654;</span>
      </div>
      <div className={cn(SECTION_BODY_CLS, !open && SECTION_BODY_COLLAPSED_CLS)}>
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

interface ConfigFormTabProps {
  schema: Record<string, unknown> | null
  values: Record<string, unknown>
  onSave: (values: Record<string, unknown>) => Promise<{ ok: boolean; errors?: string[] }>
  onReset: () => Promise<boolean>
  secretKeys?: string[]
  rulesEnforcement?: boolean
  onToggleRulesEnforcement?: (enabled: boolean) => Promise<boolean>
}

function ConfigFormTab({ schema, values: initialValues, onSave, onReset, secretKeys = [], rulesEnforcement, onToggleRulesEnforcement }: ConfigFormTabProps) {
  const [localValues, setLocalValues] = useState<Record<string, unknown>>(initialValues)
  const [saving, setSaving] = useState(false)
  const [errors, setErrors] = useState<string[]>([])
  const [showRestart, setShowRestart] = useState(false)

  useEffect(() => {
    setLocalValues(initialValues)
  }, [initialValues])

  const handleChange = useCallback((path: string, value: unknown) => {
    setLocalValues(prev => {
      const next = { ...prev }
      const parts = path.split('.')
      let current: Record<string, unknown> = next
      for (let i = 0; i < parts.length - 1; i++) {
        if (!current[parts[i]] || typeof current[parts[i]] !== 'object') {
          current[parts[i]] = {}
        }
        current[parts[i]] = { ...(current[parts[i]] as Record<string, unknown>) }
        current = current[parts[i]] as Record<string, unknown>
      }
      current[parts[parts.length - 1]] = value
      return next
    })
  }, [])

  const handleSave = async () => {
    setSaving(true)
    setErrors([])
    const result = await onSave(localValues)
    setSaving(false)
    if (result.ok) {
      setShowRestart(true)
    } else {
      setErrors(result.errors || ['Save failed'])
    }
  }

  const handleReset = async () => {
    if (!confirm('Reset all configuration to defaults? This cannot be undone.')) return
    const ok = await onReset()
    if (ok) setShowRestart(true)
  }

  if (!schema) return <div className={LOADING_CLS}>Loading schema...</div>

  const properties = getSchemaProperties(schema)
  const defs = (schema.$defs || schema.definitions || {}) as Record<string, Record<string, unknown>>

  const primitiveFields: [string, Record<string, unknown>][] = []
  const objectSections: [string, Record<string, unknown>][] = []

  for (const [name, fieldSchema] of Object.entries(properties)) {
    const fs = fieldSchema as Record<string, unknown>

    let resolved = fs
    if (fs.$ref) {
      const refName = (fs.$ref as string).split('/').pop()!
      resolved = { ...defs[refName], ...fs, $ref: undefined }
    }

    const type = getSchemaType(resolved)
    if (type === 'object' && (resolved.properties || resolved.$ref)) {
      objectSections.push([name, resolved])
    } else {
      primitiveFields.push([name, resolved])
    }
  }

  return (
    <>
      {showRestart && (
        <div className={RESTART_BANNER_CLS}>
          <span>Configuration saved. Restart the daemon to apply changes.</span>
          <button type="button" className={RESTART_BTN_CLS} onClick={() => fetch(`${import.meta.env.VITE_API_BASE_URL || ''}/api/admin/restart`, { method: 'POST' }).then(() => setShowRestart(false))}>
            Restart Now
          </button>
        </div>
      )}
      <div className={FORM_CLS}>
        {errors.length > 0 && (
          <div className={ERRORS_CLS}>
            {errors.map((e, i) => <div key={i}>{e}</div>)}
          </div>
        )}

        {onToggleRulesEnforcement && (
          <div className={FORM_SECTION_CLS}>
            <div className={SECTION_HEADER_STATIC_CLS}>
              <span className={SECTION_TITLE_CLS}>Rules Engine</span>
            </div>
            <div className={SECTION_BODY_CLS}>
              <div className={FORM_FIELD_CLS}>
                <div className={TOGGLE_ROW_CLS}>
                  <div>
                    <div className={FIELD_LABEL_CLS}>Rules Enforcement</div>
                    <span className={FIELD_HELP_CLS}>
                      Enable or disable the rule engine globally. When disabled, no rules will be evaluated.
                    </span>
                  </div>
                  <button type="button"
                    className={cn(TOGGLE_CLS, rulesEnforcement && TOGGLE_ON_CLS)}
                    onClick={() => onToggleRulesEnforcement(!rulesEnforcement)}
                    aria-label="Toggle rules enforcement"
                  />
                </div>
              </div>
            </div>
          </div>
        )}

        {primitiveFields.length > 0 && (
          <div className={FORM_SECTION_CLS}>
            <div className={SECTION_HEADER_STATIC_CLS}>
              <span className={SECTION_TITLE_CLS}>General</span>
            </div>
            <div className={SECTION_BODY_CLS}>
              {primitiveFields.map(([name, fs]) => (
                <SchemaField
                  key={name}
                  name={name}
                  fieldSchema={fs}
                  value={localValues[name]}
                  onChange={handleChange}
                  path=""
                  secretKeys={secretKeys}
                />
              ))}
            </div>
          </div>
        )}

        {objectSections.map(([name, sectionSchema]) => {
          let resolved = sectionSchema
          if (sectionSchema.$ref) {
            const refName = (sectionSchema.$ref as string).split('/').pop()!
            resolved = defs[refName] || sectionSchema
          }
          return (
            <SchemaSection
              key={name}
              name={name}
              sectionSchema={resolved}
              values={(localValues[name] || {}) as Record<string, unknown>}
              onChange={handleChange}
              parentPath=""
              secretKeys={secretKeys}
            />
          )
        })}
      </div>
      <div className={FORM_FOOTER_CLS}>
        <button type="button" className={cn(TOOLBAR_BTN_CLS, TOOLBAR_BTN_DANGER_CLS)} onClick={handleReset}>Reset to Defaults</button>
        <button type="button" className={cn(TOOLBAR_BTN_CLS, TOOLBAR_BTN_PRIMARY_CLS)} onClick={handleSave} disabled={saving}>
          {saving ? 'Saving...' : 'Save Configuration'}
        </button>
      </div>
    </>
  )
}

interface ApprovalRulesTabProps {
  rules: string[]
  defaultRules: string[]
  builtInExemptions: string[]
  onSave: (rules: string[]) => Promise<boolean>
}

function ApprovalRulesTab({
  rules,
  defaultRules,
  builtInExemptions,
  onSave,
}: ApprovalRulesTabProps) {
  const [localRules, setLocalRules] = useState<ApprovalRuleRow[]>(() => toApprovalRuleRows(rules))
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState<string | null>(null)

  useEffect(() => {
    setLocalRules(toApprovalRuleRows(rules))
    setSaveError(null)
  }, [rules])

  const handleSave = async () => {
    setSaving(true)
    setSaveError(null)
    try {
      const ok = await onSave(localRules.map((rule) => rule.value.trim()).filter(Boolean))
      if (!ok) {
        setSaveError('Failed to save approval rules.')
      }
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : 'Failed to save approval rules.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className={FORM_CLS}>
      <div className={FORM_SECTION_CLS}>
        <div className={SECTION_HEADER_STATIC_CLS}>
          <div>
            <span className={SECTION_TITLE_CLS}>Built-In Exemptions</span>
            <span className={cn(FIELD_HELP_CLS, 'ml-2')}>
              Always auto-allowed and read-only
            </span>
          </div>
        </div>
        <div className={SECTION_BODY_CLS}>
          {builtInExemptions.map((rule) => (
            <div key={rule} className={FORM_FIELD_CLS}>
              <input type="text" className={INPUT_CLS} value={rule} readOnly />
            </div>
          ))}
        </div>
      </div>

      <div className={FORM_SECTION_CLS}>
        <div className={SECTION_HEADER_STATIC_CLS}>
          <div>
            <span className={SECTION_TITLE_CLS}>Global Auto-Allow Rules</span>
            <span className={cn(FIELD_HELP_CLS, 'ml-2')}>
              Shared across providers for interactive web chat
            </span>
          </div>
        </div>
        <div className={SECTION_BODY_CLS}>
          {localRules.map((rule, index) => (
            <div key={rule.id} className={cn(TOGGLE_ROW_CLS, 'mb-3 gap-2')}>
              <input
                type="text"
                className={cn(INPUT_CLS, 'flex-1')}
                value={rule.value}
                onChange={(e) =>
                  setLocalRules((prev) => {
                    setSaveError(null)
                    return prev.map((value, i) =>
                      i === index ? { ...value, value: e.target.value } : value,
                    )
                  })
                }
                placeholder="tool:Write or mcp:gobby-tasks:*"
              />
              <button
                type="button"
                className={TOOLBAR_BTN_CLS}
                onClick={() =>
                  setLocalRules((prev) => {
                    setSaveError(null)
                    return prev.filter((_, i) => i !== index)
                  })
                }
              >
                Remove
              </button>
            </div>
          ))}

          <div className={cn(FIELD_HELP_CLS, 'mb-3')}>
            Recommended defaults: {defaultRules.join(', ')}
          </div>

          <div className={TOOLBAR_RIGHT_CLS}>
            <button
              type="button"
              className={TOOLBAR_BTN_CLS}
              onClick={() =>
                setLocalRules((prev) => {
                  setSaveError(null)
                  return [...prev, createApprovalRuleRow('')]
                })
              }
            >
              Add Rule
            </button>
            <button
              type="button"
              className={TOOLBAR_BTN_CLS}
              onClick={() => {
                setSaveError(null)
                setLocalRules(toApprovalRuleRows(defaultRules))
              }}
            >
              Reset To Defaults
            </button>
            <button
              type="button"
              className={cn(TOOLBAR_BTN_CLS, TOOLBAR_BTN_PRIMARY_CLS)}
              onClick={handleSave}
              disabled={saving}
            >
              {saving ? 'Saving...' : 'Save Rules'}
            </button>
          </div>
          {saveError && (
            <div
              className={cn(FIELD_HELP_CLS, 'mt-2 text-[var(--color-error)]')}
              role="alert"
            >
              {saveError}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

interface SecretsTabProps {
  secrets: SecretInfo[]
  categories: string[]
  onSave: (name: string, value: string, category?: string, description?: string) => Promise<boolean>
  onDelete: (name: string) => Promise<boolean>
  onRefresh: () => void
}

function SecretsTab({ secrets, categories, onSave, onDelete }: SecretsTabProps) {
  const [showForm, setShowForm] = useState(false)
  const [formName, setFormName] = useState('')
  const [formValue, setFormValue] = useState('')
  const [formCategory, setFormCategory] = useState('general')
  const [formDescription, setFormDescription] = useState('')
  const [editingName, setEditingName] = useState<string | null>(null)

  const handleSubmit = async () => {
    if (!formName.trim() || !formValue.trim()) return
    const ok = await onSave(formName.trim(), formValue, formCategory, formDescription || undefined)
    if (ok) {
      setShowForm(false)
      setEditingName(null)
      setFormName('')
      setFormValue('')
      setFormCategory('general')
      setFormDescription('')
    }
  }

  const handleEdit = (secret: SecretInfo) => {
    setEditingName(secret.name)
    setFormName(secret.name)
    setFormValue('')
    setFormCategory(secret.category)
    setFormDescription(secret.description || '')
    setShowForm(true)
  }

  const handleDelete = async (name: string) => {
    if (!confirm(`Delete secret "${name}"? This cannot be undone.`)) return
    await onDelete(name)
  }

  return (
    <div className={SECRETS_CLS}>
      <div className={SECRETS_HEADER_CLS}>
        <h3 className={SECRETS_HEADER_H3_CLS}>Secrets Store</h3>
        <button type="button"
          className={cn(TOOLBAR_BTN_CLS, TOOLBAR_BTN_PRIMARY_CLS)}
          onClick={() => {
            setEditingName(null)
            setFormName('')
            setFormValue('')
            setFormCategory('general')
            setFormDescription('')
            setShowForm(true)
          }}
        >
          Add Secret
        </button>
      </div>

      {showForm && (
        <div className={SECRET_FORM_CLS}>
          <div className={SECRET_FORM_ROW_CLS}>
            <input
              className={INPUT_CLS}
              placeholder="Secret name (e.g. anthropic_key)"
              value={formName}
              onChange={e => setFormName(e.target.value)}
              disabled={editingName !== null}
            />
            <select
              className={SELECT_CLS}
              value={formCategory}
              onChange={e => setFormCategory(e.target.value)}
            >
              {categories.map(c => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </div>
          <input
            className={INPUT_CLS}
            type="password"
            placeholder={editingName ? 'Enter new value' : 'Secret value'}
            value={formValue}
            onChange={e => setFormValue(e.target.value)}
          />
          <input
            className={INPUT_CLS}
            placeholder="Description (optional)"
            value={formDescription}
            onChange={e => setFormDescription(e.target.value)}
          />
          <div className={SECRET_FORM_ACTIONS_CLS}>
            <button type="button" className={TOOLBAR_BTN_CLS} onClick={() => setShowForm(false)}>Cancel</button>
            <button type="button" className={cn(TOOLBAR_BTN_CLS, TOOLBAR_BTN_PRIMARY_CLS)} onClick={handleSubmit}>
              {editingName ? 'Update' : 'Save'}
            </button>
          </div>
        </div>
      )}

      {secrets.length === 0 ? (
        <div className={cn(EMPTY_CLS, 'p-10')}>
          No secrets stored yet. Add API keys and sensitive values here.
        </div>
      ) : (
        <table className={SECRETS_TABLE_CLS}>
          <thead>
            <tr>
              <th className={SECRETS_TH_CLS}>Name</th>
              <th className={SECRETS_TH_CLS}>Category</th>
              <th className={SECRETS_TH_CLS}>Value</th>
              <th className={SECRETS_TH_CLS}>Description</th>
              <th className={SECRETS_TH_CLS} style={{ width: 120 }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {secrets.map(s => (
              <tr key={s.id}>
                <td className={SECRETS_TD_CLS} data-label="Name"><code>{s.name}</code></td>
                <td className={SECRETS_TD_CLS} data-label="Category">{s.category}</td>
                <td className={SECRETS_TD_CLS} data-label="Value"><span className={SECRET_MASKED_CLS}>encrypted</span></td>
                <td className={SECRETS_TD_CLS} data-label="Description">{s.description || '-'}</td>
                <td className={SECRETS_TD_CLS} data-label="Actions">
                  <div className={SECRET_ACTIONS_CLS}>
                    <button type="button" className={SECRET_ACTION_BTN_CLS} onClick={() => handleEdit(s)}>Update</button>
                    <button type="button" className={cn(SECRET_ACTION_BTN_CLS, SECRET_ACTION_DELETE_CLS)} onClick={() => handleDelete(s.name)}>Delete</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className={SECRET_HINT_CLS}>
        Use <code>$secret:NAME</code> in MCP server headers or env vars to reference secrets.
        The daemon resolves them at connection time — agents never see raw values.
      </div>
    </div>
  )
}

interface PromptsTabProps {
  prompts: PromptInfo[]
  categories: Record<string, number>
  onGetDetail: (path: string) => Promise<PromptDetail | null>
  onSaveOverride: (path: string, content: string) => Promise<boolean>
  onDeleteOverride: (path: string) => Promise<boolean>
}

function PromptsTab({ prompts, categories, onGetDetail, onSaveOverride, onDeleteOverride }: PromptsTabProps) {
  const [selectedCategory, setSelectedCategory] = useState<string | null>(null)
  const [selectedPrompt, setSelectedPrompt] = useState<PromptDetail | null>(null)
  const [editContent, setEditContent] = useState('')
  const [saving, setSaving] = useState(false)

  const filteredPrompts = useMemo(() => {
    if (!selectedCategory) return prompts
    return prompts.filter(p => p.category === selectedCategory)
  }, [prompts, selectedCategory])

  const handleSelectPrompt = async (p: PromptInfo) => {
    const detail = await onGetDetail(p.path)
    if (detail) {
      setSelectedPrompt(detail)
      setEditContent(detail.content)
    }
  }

  const handleSaveOverride = async () => {
    if (!selectedPrompt) return
    setSaving(true)
    const ok = await onSaveOverride(selectedPrompt.path, editContent)
    setSaving(false)
    if (ok) {
      setSelectedPrompt({ ...selectedPrompt, source: 'overridden', has_override: true })
    }
  }

  const handleRevert = async () => {
    if (!selectedPrompt) return
    if (!confirm(`Revert "${selectedPrompt.path}" to bundled default?`)) return
    const ok = await onDeleteOverride(selectedPrompt.path)
    if (ok && selectedPrompt.bundled_content !== null) {
      setSelectedPrompt({ ...selectedPrompt, source: 'bundled', has_override: false, content: selectedPrompt.bundled_content })
      setEditContent(selectedPrompt.bundled_content)
    }
  }

  const categoryList = useMemo(() => {
    return Object.entries(categories).sort(([a], [b]) => a.localeCompare(b))
  }, [categories])

  return (
    <div className={PROMPTS_CLS}>
      <div className={PROMPTS_SIDEBAR_CLS}>
        <div className={PROMPTS_SIDEBAR_TITLE_CLS}>Categories</div>
        <div
          className={cn(PROMPT_CATEGORY_CLS, selectedCategory === null && PROMPT_CATEGORY_ACTIVE_CLS)}
          onClick={() => setSelectedCategory(null)}
        >
          <span>All</span>
          <span className={PROMPT_CATEGORY_COUNT_CLS}>{prompts.length}</span>
        </div>
        {categoryList.map(([cat, count]) => (
          <div
            key={cat}
            className={cn(PROMPT_CATEGORY_CLS, selectedCategory === cat && PROMPT_CATEGORY_ACTIVE_CLS)}
            onClick={() => setSelectedCategory(cat)}
          >
            <span>{formatFieldName(cat)}</span>
            <span className={PROMPT_CATEGORY_COUNT_CLS}>{count}</span>
          </div>
        ))}
      </div>

      <div className={PROMPTS_MAIN_CLS}>
        {selectedPrompt ? (
          <div className={PROMPT_DETAIL_CLS}>
            <div className={PROMPT_DETAIL_HEADER_CLS}>
              <div>
                <div className={PROMPT_DETAIL_TITLE_CLS}>{selectedPrompt.path}</div>
                {selectedPrompt.description && (
                  <span className={FIELD_HELP_CLS}>{selectedPrompt.description}</span>
                )}
              </div>
              <div className={PROMPT_DETAIL_ACTIONS_CLS}>
                <button type="button" className={TOOLBAR_BTN_CLS} onClick={() => setSelectedPrompt(null)}>Back</button>
                {selectedPrompt.has_override && (
                  <button type="button" className={cn(TOOLBAR_BTN_CLS, TOOLBAR_BTN_DANGER_CLS)} onClick={handleRevert}>Revert</button>
                )}
                <button type="button" className={cn(TOOLBAR_BTN_CLS, TOOLBAR_BTN_PRIMARY_CLS)} onClick={handleSaveOverride} disabled={saving}>
                  {saving ? 'Saving...' : 'Save Override'}
                </button>
              </div>
            </div>
            <div className={PROMPT_EDITOR_CLS}>
              <CodeMirrorEditor
                content={editContent}
                language="markdown"
                onChange={setEditContent}
                onSave={handleSaveOverride}
              />
            </div>
          </div>
        ) : (
          <div className={PROMPTS_LIST_CLS}>
            {filteredPrompts.length === 0 ? (
              <div className={PROMPT_EMPTY_CLS}>No prompts in this category</div>
            ) : (
              filteredPrompts.map(p => (
                <div
                  key={p.path}
                  className={PROMPT_CARD_CLS}
                  onClick={() => handleSelectPrompt(p)}
                >
                  <div>
                    <div className={PROMPT_CARD_NAME_CLS}>{p.path}</div>
                    {p.description && <div className={PROMPT_CARD_DESC_CLS}>{p.description}</div>}
                  </div>
                  <span className={cn(PROMPT_BADGE_CLS, PROMPT_BADGE_BG[p.source] ?? '')}>
                    {p.source}
                  </span>
                </div>
              ))
            )}
          </div>
        )}
      </div>
    </div>
  )
}

interface VariableDefinition {
  id: string
  name: string
  definition_json: string
  workflow_type: string
  source: string
  enabled: boolean
  description: string | null
}

function VariablesTab() {
  const [variables, setVariables] = useState<VariableDefinition[]>([])
  const [loading, setLoading] = useState(true)
  const [showForm, setShowForm] = useState(false)
  const [formName, setFormName] = useState('')
  const [formValue, setFormValue] = useState('')
  const [formDescription, setFormDescription] = useState('')

  const baseUrl = import.meta.env.VITE_API_BASE_URL || ''

  const fetchVariables = useCallback(async () => {
    try {
      const res = await fetch(`${baseUrl}/api/workflows?workflow_type=variable`)
      if (res.ok) {
        const data = await res.json()
        setVariables(data.definitions || [])
      }
    } catch {
      // silently fail
    } finally {
      setLoading(false)
    }
  }, [baseUrl])

  useEffect(() => {
    fetchVariables()
  }, [fetchVariables])

  const handleCreate = async () => {
    const trimmedName = formName.trim()
    if (!trimmedName) return

    let parsedValue: unknown = formValue
    if (formValue === 'true') parsedValue = true
    else if (formValue === 'false') parsedValue = false
    else if (formValue === 'null') parsedValue = null
    else if (formValue === '[]') parsedValue = []
    else if (/^-?\d+$/.test(formValue)) parsedValue = parseInt(formValue, 10)
    else if (/^-?\d+\.\d+$/.test(formValue)) parsedValue = parseFloat(formValue)

    const defJson = JSON.stringify({
      variable: trimmedName,
      value: parsedValue,
      description: formDescription || undefined,
    })

    try {
      const res = await fetch(`${baseUrl}/api/workflows`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: trimmedName,
          definition_json: defJson,
          workflow_type: 'variable',
          description: formDescription || undefined,
          enabled: true,
          source: 'installed',
        }),
      })
      if (res.ok) {
        setShowForm(false)
        setFormName('')
        setFormValue('')
        setFormDescription('')
        fetchVariables()
      }
    } catch {
      // silently fail
    }
  }

  const handleToggle = async (v: VariableDefinition) => {
    try {
      const res = await fetch(`${baseUrl}/api/workflows/${v.id}/toggle`, { method: 'PUT' })
      if (res.ok) fetchVariables()
    } catch {
      // silently fail
    }
  }

  const handleDelete = async (v: VariableDefinition) => {
    if (!confirm(`Delete variable "${v.name}"?`)) return
    try {
      const res = await fetch(`${baseUrl}/api/workflows/${v.id}`, { method: 'DELETE' })
      if (res.ok) fetchVariables()
    } catch {
      // silently fail
    }
  }

  const getDisplayValue = (defJson: string): string => {
    try {
      const parsed = JSON.parse(defJson)
      const val = parsed.value
      if (val === null || val === undefined) return 'null'
      if (Array.isArray(val)) return JSON.stringify(val)
      return String(val)
    } catch {
      return '-'
    }
  }

  if (loading) return <div className={LOADING_CLS}>Loading variables...</div>

  return (
    <div className={SECRETS_CLS}>
      <div className={SECRETS_HEADER_CLS}>
        <h3 className={SECRETS_HEADER_H3_CLS}>Variable Defaults</h3>
        <button type="button"
          className={cn(TOOLBAR_BTN_CLS, TOOLBAR_BTN_PRIMARY_CLS)}
          onClick={() => {
            setFormName('')
            setFormValue('')
            setFormDescription('')
            setShowForm(true)
          }}
        >
          Add Variable
        </button>
      </div>

      {showForm && (
        <div className={SECRET_FORM_CLS}>
          <div className={SECRET_FORM_ROW_CLS}>
            <input
              className={INPUT_CLS}
              placeholder="Variable name (e.g. my_custom_var)"
              value={formName}
              onChange={e => setFormName(e.target.value)}
            />
          </div>
          <input
            className={INPUT_CLS}
            placeholder="Default value (e.g. true, 42, hello)"
            value={formValue}
            onChange={e => setFormValue(e.target.value)}
          />
          <input
            className={INPUT_CLS}
            placeholder="Description (optional)"
            value={formDescription}
            onChange={e => setFormDescription(e.target.value)}
          />
          <div className={SECRET_FORM_ACTIONS_CLS}>
            <button type="button" className={TOOLBAR_BTN_CLS} onClick={() => setShowForm(false)}>Cancel</button>
            <button type="button" className={cn(TOOLBAR_BTN_CLS, TOOLBAR_BTN_PRIMARY_CLS)} onClick={handleCreate}>Save</button>
          </div>
        </div>
      )}

      {variables.length === 0 ? (
        <div className={cn(EMPTY_CLS, 'p-10')}>
          No variable definitions found. Add session variable defaults here.
        </div>
      ) : (
        <table className={SECRETS_TABLE_CLS}>
          <thead>
            <tr>
              <th className={SECRETS_TH_CLS}>Name</th>
              <th className={SECRETS_TH_CLS}>Default Value</th>
              <th className={SECRETS_TH_CLS}>Description</th>
              <th className={SECRETS_TH_CLS}>Source</th>
              <th className={SECRETS_TH_CLS} style={{ width: 80 }}>Enabled</th>
              <th className={SECRETS_TH_CLS} style={{ width: 80 }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {variables.map(v => (
              <tr key={v.id}>
                <td className={SECRETS_TD_CLS} data-label="Name"><code>{v.name}</code></td>
                <td className={SECRETS_TD_CLS} data-label="Default Value"><code>{getDisplayValue(v.definition_json)}</code></td>
                <td className={SECRETS_TD_CLS} data-label="Description">{v.description || '-'}</td>
                <td className={SECRETS_TD_CLS} data-label="Source">
                  <span className={cn(PROMPT_BADGE_CLS, PROMPT_BADGE_BG[v.source] ?? '')}>{v.source}</span>
                </td>
                <td className={SECRETS_TD_CLS} data-label="Enabled">
                  <button type="button"
                    className={cn(TOGGLE_CLS, v.enabled && TOGGLE_ON_CLS)}
                    onClick={() => handleToggle(v)}
                    aria-label={`Toggle ${v.name}`}
                  />
                </td>
                <td className={SECRETS_TD_CLS} data-label="Actions">
                  {v.source !== 'template' && (
                    <div className={SECRET_ACTIONS_CLS}>
                      <button type="button" className={cn(SECRET_ACTION_BTN_CLS, SECRET_ACTION_DELETE_CLS)} onClick={() => handleDelete(v)}>Delete</button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className={SECRET_HINT_CLS}>
        Variables define default session values. Template variables are bundled with Gobby.
        Custom variables use <code>source: installed</code> and can be deleted.
      </div>
    </div>
  )
}

interface TemplateTabProps {
  content: string
  onFetch: () => Promise<void>
  onSave: (content: string) => Promise<{ ok: boolean; errors?: string[] }>
}

function TemplateTab({ content, onFetch, onSave }: TemplateTabProps) {
  const [localContent, setLocalContent] = useState(content)
  const [errors, setErrors] = useState<string[]>([])
  const [saving, setSaving] = useState(false)
  const [showRestart, setShowRestart] = useState(false)

  useEffect(() => {
    setLocalContent(content)
  }, [content])

  useEffect(() => {
    onFetch()
  }, [onFetch])

  const handleSave = async () => {
    setSaving(true)
    setErrors([])
    const result = await onSave(localContent)
    setSaving(false)
    if (result.ok) {
      setShowRestart(true)
    } else {
      setErrors(result.errors || ['Save failed'])
    }
  }

  return (
    <div className={YAML_CLS}>
      {showRestart && (
        <div className={RESTART_BANNER_CLS}>
          <span>Configuration saved to database. Restart the daemon to apply changes.</span>
          <button type="button" className={RESTART_BTN_CLS} onClick={() => fetch(`${import.meta.env.VITE_API_BASE_URL || ''}/api/admin/restart`, { method: 'POST' }).then(() => setShowRestart(false))}>
            Restart Now
          </button>
        </div>
      )}
      <div className={YAML_EDITOR_CLS}>
        <CodeMirrorEditor
          content={localContent}
          language="yaml"
          onChange={setLocalContent}
          onSave={handleSave}
        />
      </div>
      <div className={YAML_FOOTER_CLS}>
        <div className={YAML_ERRORS_CLS}>
          {errors.map((e, i) => <span key={i}>{e}</span>)}
        </div>
        <button type="button" className={cn(TOOLBAR_BTN_CLS, TOOLBAR_BTN_PRIMARY_CLS)} onClick={handleSave} disabled={saving}>
          {saving ? 'Saving...' : 'Save Template'}
        </button>
      </div>
    </div>
  )
}

const SCROLL_SHADOW =
  'inset 12px 0 10px -12px color-mix(in srgb, var(--text-primary) 28%, transparent)'
const SCROLL_SHADOW_RIGHT =
  'inset -12px 0 10px -12px color-mix(in srgb, var(--text-primary) 28%, transparent)'
const NO_SHADOW = 'inset 12px 0 10px -12px transparent'
const NO_SHADOW_RIGHT = 'inset -12px 0 10px -12px transparent'

export function ConfigurationPage() {
  const [activeTab, setActiveTab] = useState<TabId>('config')
  const tabsRef = useRef<HTMLDivElement | null>(null)
  const [tabScrollState, setTabScrollState] = useState({
    canScrollLeft: false,
    canScrollRight: false,
  })
  const config = useConfiguration()

  const updateTabScrollState = useCallback(() => {
    const tabs = tabsRef.current
    if (!tabs) return

    const maxScrollLeft = tabs.scrollWidth - tabs.clientWidth
    const canScrollLeft = tabs.scrollLeft > 0
    const canScrollRight = tabs.scrollLeft < maxScrollLeft - 1
    setTabScrollState((previous) =>
      previous.canScrollLeft === canScrollLeft &&
      previous.canScrollRight === canScrollRight
        ? previous
        : { canScrollLeft, canScrollRight },
    )
  }, [])

  useEffect(() => {
    config.fetchConfig()
    config.fetchSecrets()
    config.fetchPrompts()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const tabs = tabsRef.current
    if (!tabs) return

    updateTabScrollState()
    tabs.addEventListener('scroll', updateTabScrollState, { passive: true })
    window.addEventListener('resize', updateTabScrollState)
    return () => {
      tabs.removeEventListener('scroll', updateTabScrollState)
      window.removeEventListener('resize', updateTabScrollState)
    }
  }, [updateTabScrollState])

  useEffect(() => {
    updateTabScrollState()
  }, [activeTab, updateTabScrollState])

  const handleExport = async () => {
    const bundle = await config.exportConfig()
    if (bundle) {
      const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `gobby-config-${new Date().toISOString().slice(0, 10)}.json`
      a.click()
      URL.revokeObjectURL(url)
    }
  }

  const handleImport = async () => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = '.json'
    input.onchange = async () => {
      const file = input.files?.[0]
      if (!file) return
      try {
        const text = await file.text()
        const bundle = JSON.parse(text)
        const result = await config.importConfig({
          config_store: bundle.config_store,
          config: bundle.config,
          prompts: bundle.prompts,
        })
        if (result.success) {
          alert(`Import successful: ${result.summary}`)
          config.fetchConfig()
          config.fetchPrompts()
        } else {
          alert(`Import failed: ${result.summary || "Unknown error"}`)
        }
      } catch (e) {
        alert(`Import failed: ${e}`)
      }
    }
    input.click()
  }

  const tabs: { id: TabId; label: string }[] = [
    { id: 'config', label: 'Configuration' },
    { id: 'approvals', label: 'Approvals' },
    { id: 'secrets', label: 'Secrets' },
    { id: 'prompts', label: 'Prompts' },
    { id: 'variables', label: 'Variables' },
    { id: 'template', label: 'Template' },
  ]

  const tabsBoxShadow = `${tabScrollState.canScrollLeft ? SCROLL_SHADOW : NO_SHADOW}, ${tabScrollState.canScrollRight ? SCROLL_SHADOW_RIGHT : NO_SHADOW_RIGHT}`

  return (
    <div className={PAGE_CLS}>
      <h1 className="sr-only">Configuration</h1>
      <div className={TOOLBAR_CLS}>
        <div className={TOOLBAR_LEFT_CLS}>
          <div ref={tabsRef} className={TABS_CLS} style={{ boxShadow: tabsBoxShadow }}>
            {tabs.map(t => (
              <button type="button"
                key={t.id}
                className={cn(TAB_CLS, activeTab === t.id && TAB_ACTIVE_CLS)}
                onClick={() => setActiveTab(t.id)}
              >
                {t.label}
              </button>
            ))}
          </div>
        </div>
        <div className={TOOLBAR_RIGHT_CLS}>
          <button type="button" className={TOOLBAR_BTN_CLS} onClick={handleImport}>Import</button>
          <button type="button" className={TOOLBAR_BTN_CLS} onClick={handleExport}>Export</button>
        </div>
      </div>

      <div className={CONTENT_CLS}>
        {activeTab === 'config' && (
          <ConfigFormTab
            schema={config.schema}
            values={config.configValues}
            onSave={config.saveConfig}
            onReset={config.resetToDefaults}
            secretKeys={config.secretKeys}
            rulesEnforcement={config.rulesEnforcement}
            onToggleRulesEnforcement={config.setRulesEnforcement}
          />
        )}
        {activeTab === 'approvals' && (
          <ApprovalRulesTab
            rules={config.globalApprovalRules}
            defaultRules={config.defaultApprovalRules}
            builtInExemptions={config.builtInApprovalExemptions}
            onSave={config.saveGlobalApprovalRules}
          />
        )}
        {activeTab === 'secrets' && (
          <SecretsTab
            secrets={config.secrets}
            categories={config.secretCategories}
            onSave={config.saveSecret}
            onDelete={config.deleteSecret}
            onRefresh={config.fetchSecrets}
          />
        )}
        {activeTab === 'prompts' && (
          <PromptsTab
            prompts={config.prompts}
            categories={config.promptCategories}
            onGetDetail={config.getPromptDetail}
            onSaveOverride={config.savePromptOverride}
            onDeleteOverride={config.deletePromptOverride}
          />
        )}
        {activeTab === 'variables' && (
          <VariablesTab />
        )}
        {activeTab === 'template' && (
          <TemplateTab
            content={config.templateContent}
            onFetch={config.fetchTemplate}
            onSave={config.saveTemplate}
          />
        )}
      </div>
    </div>
  )
}
