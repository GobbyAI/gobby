import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useConfiguration } from '../hooks/useConfiguration'
import type { PromptInfo, PromptDetail } from '../hooks/useConfiguration'
import { CodeMirrorEditor } from './shared/CodeMirrorEditor'
import { useDaemonRestart } from '../hooks/useDaemonRestart'
import { cn } from '../lib/utils'
import {
  CONTENT_CLS,
  EMPTY_CLS,
  ERRORS_CLS,
  FIELD_HELP_CLS,
  FIELD_LABEL_CLS,
  FORM_CLS,
  FORM_FIELD_CLS,
  FORM_FOOTER_CLS,
  FORM_SECTION_CLS,
  INPUT_CLS,
  LOADING_CLS,
  PAGE_CLS,
  PROMPT_BADGE_BG,
  PROMPT_BADGE_CLS,
  PROMPT_CARD_CLS,
  PROMPT_CARD_DESC_CLS,
  PROMPT_CARD_NAME_CLS,
  PROMPT_CATEGORY_ACTIVE_CLS,
  PROMPT_CATEGORY_CLS,
  PROMPT_CATEGORY_COUNT_CLS,
  PROMPT_DETAIL_ACTIONS_CLS,
  PROMPT_DETAIL_CLS,
  PROMPT_DETAIL_HEADER_CLS,
  PROMPT_DETAIL_TITLE_CLS,
  PROMPT_EDITOR_CLS,
  PROMPT_EMPTY_CLS,
  PROMPTS_CLS,
  PROMPTS_LIST_CLS,
  PROMPTS_MAIN_CLS,
  PROMPTS_SIDEBAR_CLS,
  PROMPTS_SIDEBAR_TITLE_CLS,
  RESTART_BANNER_CLS,
  RESTART_BTN_CLS,
  SECRET_ACTION_BTN_CLS,
  SECRET_ACTION_DELETE_CLS,
  SECRET_ACTIONS_CLS,
  SECRET_FORM_ACTIONS_CLS,
  SECRET_FORM_CLS,
  SECRET_FORM_ROW_CLS,
  SECRET_HINT_CLS,
  SECRETS_CLS,
  SECRETS_HEADER_CLS,
  SECRETS_HEADER_H3_CLS,
  SECRETS_TABLE_CLS,
  SECRETS_TD_CLS,
  SECRETS_TH_CLS,
  SECTION_BODY_CLS,
  SECTION_HEADER_STATIC_CLS,
  SECTION_TITLE_CLS,
  TAB_ACTIVE_CLS,
  TAB_CLS,
  TABS_CLS,
  TOGGLE_CLS,
  TOGGLE_ON_CLS,
  TOGGLE_ROW_CLS,
  TOOLBAR_BTN_CLS,
  TOOLBAR_BTN_DANGER_CLS,
  TOOLBAR_BTN_PRIMARY_CLS,
  TOOLBAR_CLS,
  TOOLBAR_LEFT_CLS,
  TOOLBAR_RIGHT_CLS,
} from './ConfigurationPage.styles'
import {
  type ApprovalRuleRow,
  createApprovalRuleRow,
  formatFieldName,
  getSchemaProperties,
  getSchemaType,
  toApprovalRuleRows,
} from './ConfigurationPage.helpers'
import { SchemaField, SchemaSection } from './ConfigurationPage.SchemaField'
import { SecretsTab } from './ConfigurationPage.SecretsTab'
import { TemplateTab } from './ConfigurationPage.TemplateTab'
import { ValidationDetectionEditor } from './ValidationDetectionEditor'
import { Heading } from './shared/Heading'

type TabId = 'config' | 'approvals' | 'secrets' | 'prompts' | 'variables' | 'template'

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
  const { showRestart, restartError, markRestartRequired, restartDaemon } = useDaemonRestart()

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
      markRestartRequired()
    } else {
      setErrors(result.errors || ['Save failed'])
    }
  }

  const handleReset = async () => {
    if (!confirm('Reset all configuration to defaults? This cannot be undone.')) return
    const ok = await onReset()
    if (ok) markRestartRequired()
  }

  if (!schema) return <div className={LOADING_CLS}>Loading schema...</div>

  const properties = getSchemaProperties(schema)
  const defs = (schema.$defs || schema.definitions || {}) as Record<string, Record<string, unknown>>

  const primitiveFields: [string, Record<string, unknown>][] = []
  const objectSections: [string, Record<string, unknown>][] = []

  for (const [name, fieldSchema] of Object.entries(properties)) {
    const fs = fieldSchema as Record<string, unknown>
    if (name === 'validation_detection') continue

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
          <button
            type="button"
            className={RESTART_BTN_CLS}
            onClick={() => { void restartDaemon() }}
          >
            Restart Now
          </button>
        </div>
      )}
      <div className={FORM_CLS}>
        {(errors.length > 0 || restartError) && (
          <div className={ERRORS_CLS}>
            {errors.map((e, i) => <div key={i}>{e}</div>)}
            {restartError && <div>{restartError}</div>}
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

        <ValidationDetectionEditor
          key={JSON.stringify(initialValues.validation_detection ?? null)}
          value={localValues.validation_detection}
          onChange={(value) => handleChange('validation_detection', value)}
        />

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
        <Heading level={3} className={SECRETS_HEADER_H3_CLS}>Variable Defaults</Heading>
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
                  <span
                    className={cn(
                      PROMPT_BADGE_CLS,
                      v.source === 'bundled' || v.source === 'overridden'
                        ? PROMPT_BADGE_BG[v.source]
                        : ''
                    )}
                  >
                    {v.source}
                  </span>
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
      <Heading level={1} className="sr-only">Configuration</Heading>
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
