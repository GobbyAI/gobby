import { useState, useEffect, useCallback, useMemo } from 'react'
import { useConfiguration } from '../hooks/useConfiguration'
import type { SecretInfo, PromptInfo, PromptDetail } from '../hooks/useConfiguration'
import { CodeMirrorEditor } from './shared/CodeMirrorEditor'
import './ConfigurationPage.css'

// =============================================================================
// Types
// =============================================================================

type TabId = 'config' | 'secrets' | 'prompts' | 'variables' | 'template'

const BACKEND_SECRET_MASK = '********'

// =============================================================================
// Secret field detection
// =============================================================================

const SECRET_PATTERNS = ['api_key', 'api_token', 'api_secret', 'password', 'access_token', 'auth_token', 'secret_key', 'secret', 'credentials', 'private_key', 'client_secret']

function isSecretField(path: string, secretKeys: string[]): boolean {
  if (secretKeys.includes(path)) return true
  const last = path.split('.').pop() || ''
  return SECRET_PATTERNS.some(p => last.includes(p))
}

// =============================================================================
// Helpers
// =============================================================================

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

// =============================================================================
// SchemaField - Renders a single form field from JSON Schema
// =============================================================================

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
      <div className="config-form-field">
        <label className="config-field-label">{formatFieldName(name)}</label>
        {description && <span className="config-field-help">{description}</span>}
        <select
          className="config-select"
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
      <div className="config-form-field">
        <div className="config-toggle-row">
          <div>
            <div className="config-field-label">{formatFieldName(name)}</div>
            {description && <span className="config-field-help">{description}</span>}
          </div>
          <button type="button"
            className={`config-toggle ${value ? 'on' : ''}`}
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
      <div className="config-form-field">
        <label className="config-field-label">{formatFieldName(name)}</label>
        {description && <span className="config-field-help">{description}</span>}
        <input
          type="number"
          className="config-input"
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

  // Default: string input (password for secret fields)
  const secret = isSecretField(fullPath, secretKeys)
  const isMasked = secret && value === BACKEND_SECRET_MASK
  return (
    <div className="config-form-field">
      <label className="config-field-label">
        {formatFieldName(name)}
        {secret && <span className="config-secret-badge">encrypted</span>}
      </label>
      {description && <span className="config-field-help">{description}</span>}
      <input
        type={secret ? 'password' : 'text'}
        className="config-input"
        value={String(value ?? '')}
        placeholder={isMasked ? 'Enter new value to change' : undefined}
        onChange={e => onChange(fullPath, e.target.value)}
      />
    </div>
  )
}

// =============================================================================
// SchemaSection - Collapsible section for sub-configs
// =============================================================================

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
    <div className="config-form-section">
      <div className="config-section-header" onClick={() => setOpen(!open)}>
        <div>
          <span className="config-section-title">{formatFieldName(name)}</span>
          {description && <span className="config-field-help" style={{ marginLeft: 8 }}>{description}</span>}
        </div>
        <span className={`config-section-toggle ${open ? 'open' : ''}`}>&#9654;</span>
      </div>
      <div className={`config-section-body ${open ? '' : 'collapsed'}`}>
        {Object.entries(props).map(([fieldName, fieldSchema]) => {
          const fs = fieldSchema as Record<string, unknown>
          const fieldType = getSchemaType(fs)

          // Nested object = sub-section (but only one level deep to avoid excess nesting)
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

// =============================================================================
// ConfigFormTab
// =============================================================================

// TODO: Replace schema-driven rendering with hand-crafted sections when config stabilizes

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

  if (!schema) return <div className="config-loading">Loading schema...</div>

  const properties = getSchemaProperties(schema)
  const defs = (schema.$defs || schema.definitions || {}) as Record<string, Record<string, unknown>>

  // Separate top-level primitives from object sections
  const primitiveFields: [string, Record<string, unknown>][] = []
  const objectSections: [string, Record<string, unknown>][] = []

  for (const [name, fieldSchema] of Object.entries(properties)) {
    const fs = fieldSchema as Record<string, unknown>

    // Resolve $ref
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
        <div className="config-restart-banner">
          <span>Configuration saved. Restart the daemon to apply changes.</span>
          <button type="button" onClick={() => fetch(`${import.meta.env.VITE_API_BASE_URL || ''}/api/admin/restart`, { method: 'POST' }).then(() => setShowRestart(false))}>
            Restart Now
          </button>
        </div>
      )}
      <div className="config-form">
        {errors.length > 0 && (
          <div style={{ color: 'var(--color-error)', fontSize: 13, marginBottom: 12 }}>
            {errors.map((e, i) => <div key={i}>{e}</div>)}
          </div>
        )}

        {/* Rules enforcement toggle */}
        {onToggleRulesEnforcement && (
          <div className="config-form-section">
            <div className="config-section-header" style={{ cursor: 'default' }}>
              <span className="config-section-title">Rules Engine</span>
            </div>
            <div className="config-section-body">
              <div className="config-form-field">
                <div className="config-toggle-row">
                  <div>
                    <div className="config-field-label">Rules Enforcement</div>
                    <span className="config-field-help">
                      Enable or disable the rule engine globally. When disabled, no rules will be evaluated.
                    </span>
                  </div>
                  <button type="button"
                    className={`config-toggle ${rulesEnforcement ? 'on' : ''}`}
                    onClick={() => onToggleRulesEnforcement(!rulesEnforcement)}
                    aria-label="Toggle rules enforcement"
                  />
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Top-level primitive fields */}
        {primitiveFields.length > 0 && (
          <div className="config-form-section">
            <div className="config-section-header" style={{ cursor: 'default' }}>
              <span className="config-section-title">General</span>
            </div>
            <div className="config-section-body">
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

        {/* Object sub-config sections */}
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
      <div className="config-form-footer">
        <button type="button" className="config-toolbar-btn danger" onClick={handleReset}>Reset to Defaults</button>
        <button type="button" className="config-toolbar-btn primary" onClick={handleSave} disabled={saving}>
          {saving ? 'Saving...' : 'Save Configuration'}
        </button>
      </div>
    </>
  )
}

// =============================================================================
// SecretsTab
// =============================================================================

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
    <div className="config-secrets">
      <div className="config-secrets-header">
        <h3>Secrets Store</h3>
        <button type="button"
          className="config-toolbar-btn primary"
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
        <div className="config-secret-form">
          <div className="config-secret-form-row">
            <input
              className="config-input"
              placeholder="Secret name (e.g. anthropic_key)"
              value={formName}
              onChange={e => setFormName(e.target.value)}
              disabled={editingName !== null}
            />
            <select
              className="config-select"
              value={formCategory}
              onChange={e => setFormCategory(e.target.value)}
            >
              {categories.map(c => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </div>
          <input
            className="config-input"
            type="password"
            placeholder={editingName ? 'Enter new value' : 'Secret value'}
            value={formValue}
            onChange={e => setFormValue(e.target.value)}
          />
          <input
            className="config-input"
            placeholder="Description (optional)"
            value={formDescription}
            onChange={e => setFormDescription(e.target.value)}
          />
          <div className="config-secret-form-actions">
            <button type="button" className="config-toolbar-btn" onClick={() => setShowForm(false)}>Cancel</button>
            <button type="button" className="config-toolbar-btn primary" onClick={handleSubmit}>
              {editingName ? 'Update' : 'Save'}
            </button>
          </div>
        </div>
      )}

      {secrets.length === 0 ? (
        <div className="config-empty" style={{ padding: 40 }}>
          No secrets stored yet. Add API keys and sensitive values here.
        </div>
      ) : (
        <table className="config-secrets-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Category</th>
              <th>Value</th>
              <th>Description</th>
              <th style={{ width: 120 }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {secrets.map(s => (
              <tr key={s.id}>
                <td><code>{s.name}</code></td>
                <td>{s.category}</td>
                <td><span className="config-secret-masked">encrypted</span></td>
                <td>{s.description || '-'}</td>
                <td>
                  <div className="config-secret-actions">
                    <button type="button" onClick={() => handleEdit(s)}>Update</button>
                    <button type="button" className="delete" onClick={() => handleDelete(s.name)}>Delete</button>
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="config-secret-hint">
        Use <code>$secret:NAME</code> in MCP server headers or env vars to reference secrets.
        The daemon resolves them at connection time — agents never see raw values.
      </div>
    </div>
  )
}

// =============================================================================
// PromptsTab
// =============================================================================

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
    <div className="config-prompts">
      {/* Category sidebar */}
      <div className="config-prompts-sidebar">
        <div className="config-prompts-sidebar-title">Categories</div>
        <div
          className={`config-prompt-category ${selectedCategory === null ? 'active' : ''}`}
          onClick={() => setSelectedCategory(null)}
        >
          <span>All</span>
          <span className="config-prompt-category-count">{prompts.length}</span>
        </div>
        {categoryList.map(([cat, count]) => (
          <div
            key={cat}
            className={`config-prompt-category ${selectedCategory === cat ? 'active' : ''}`}
            onClick={() => setSelectedCategory(cat)}
          >
            <span>{formatFieldName(cat)}</span>
            <span className="config-prompt-category-count">{count}</span>
          </div>
        ))}
      </div>

      {/* Main area */}
      <div className="config-prompts-main">
        {selectedPrompt ? (
          <div className="config-prompt-detail">
            <div className="config-prompt-detail-header">
              <div>
                <div className="config-prompt-detail-title">{selectedPrompt.path}</div>
                {selectedPrompt.description && (
                  <span className="config-field-help">{selectedPrompt.description}</span>
                )}
              </div>
              <div className="config-prompt-detail-actions">
                <button type="button" className="config-toolbar-btn" onClick={() => setSelectedPrompt(null)}>Back</button>
                {selectedPrompt.has_override && (
                  <button type="button" className="config-toolbar-btn danger" onClick={handleRevert}>Revert</button>
                )}
                <button type="button" className="config-toolbar-btn primary" onClick={handleSaveOverride} disabled={saving}>
                  {saving ? 'Saving...' : 'Save Override'}
                </button>
              </div>
            </div>
            <div className="config-prompt-editor">
              <CodeMirrorEditor
                content={editContent}
                language="markdown"
                onChange={setEditContent}
                onSave={handleSaveOverride}
              />
            </div>
          </div>
        ) : (
          <div className="config-prompts-list">
            {filteredPrompts.length === 0 ? (
              <div className="config-prompt-empty">No prompts in this category</div>
            ) : (
              filteredPrompts.map(p => (
                <div
                  key={p.path}
                  className="config-prompt-card"
                  onClick={() => handleSelectPrompt(p)}
                >
                  <div>
                    <div className="config-prompt-card-name">{p.path}</div>
                    {p.description && <div className="config-prompt-card-desc">{p.description}</div>}
                  </div>
                  <span className={`config-prompt-badge ${p.source}`}>{p.source}</span>
                </div>
              ))
            )}
          </div>
        )}
      </div>
    </div>
  )
}

// =============================================================================
// VariablesTab
// =============================================================================

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

    // Auto-detect value type
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

  if (loading) return <div className="config-loading">Loading variables...</div>

  return (
    <div className="config-secrets">
      <div className="config-secrets-header">
        <h3>Variable Defaults</h3>
        <button type="button"
          className="config-toolbar-btn primary"
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
        <div className="config-secret-form">
          <div className="config-secret-form-row">
            <input
              className="config-input"
              placeholder="Variable name (e.g. my_custom_var)"
              value={formName}
              onChange={e => setFormName(e.target.value)}
            />
          </div>
          <input
            className="config-input"
            placeholder="Default value (e.g. true, 42, hello)"
            value={formValue}
            onChange={e => setFormValue(e.target.value)}
          />
          <input
            className="config-input"
            placeholder="Description (optional)"
            value={formDescription}
            onChange={e => setFormDescription(e.target.value)}
          />
          <div className="config-secret-form-actions">
            <button type="button" className="config-toolbar-btn" onClick={() => setShowForm(false)}>Cancel</button>
            <button type="button" className="config-toolbar-btn primary" onClick={handleCreate}>Save</button>
          </div>
        </div>
      )}

      {variables.length === 0 ? (
        <div className="config-empty" style={{ padding: 40 }}>
          No variable definitions found. Add session variable defaults here.
        </div>
      ) : (
        <table className="config-secrets-table">
          <thead>
            <tr>
              <th>Name</th>
              <th>Default Value</th>
              <th>Description</th>
              <th>Source</th>
              <th style={{ width: 80 }}>Enabled</th>
              <th style={{ width: 80 }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {variables.map(v => (
              <tr key={v.id}>
                <td><code>{v.name}</code></td>
                <td><code>{getDisplayValue(v.definition_json)}</code></td>
                <td>{v.description || '-'}</td>
                <td><span className={`config-prompt-badge ${v.source}`}>{v.source}</span></td>
                <td>
                  <button type="button"
                    className={`config-toggle ${v.enabled ? 'on' : ''}`}
                    onClick={() => handleToggle(v)}
                    aria-label={`Toggle ${v.name}`}
                  />
                </td>
                <td>
                  {v.source !== 'template' && (
                    <div className="config-secret-actions">
                      <button type="button" className="delete" onClick={() => handleDelete(v)}>Delete</button>
                    </div>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="config-secret-hint">
        Variables define default session values. Template variables are bundled with Gobby.
        Custom variables use <code>source: installed</code> and can be deleted.
      </div>
    </div>
  )
}

// =============================================================================
// TemplateTab
// =============================================================================

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
    <div className="config-yaml">
      {showRestart && (
        <div className="config-restart-banner">
          <span>Configuration saved to database. Restart the daemon to apply changes.</span>
          <button type="button" onClick={() => fetch(`${import.meta.env.VITE_API_BASE_URL || ''}/api/admin/restart`, { method: 'POST' }).then(() => setShowRestart(false))}>
            Restart Now
          </button>
        </div>
      )}
      <div className="config-yaml-editor">
        <CodeMirrorEditor
          content={localContent}
          language="yaml"
          onChange={setLocalContent}
          onSave={handleSave}
        />
      </div>
      <div className="config-yaml-footer">
        <div className="config-yaml-errors">
          {errors.map((e, i) => <span key={i}>{e}</span>)}
        </div>
        <button type="button" className="config-toolbar-btn primary" onClick={handleSave} disabled={saving}>
          {saving ? 'Saving...' : 'Save Template'}
        </button>
      </div>
    </div>
  )
}

// =============================================================================
// ConfigurationPage (main export)
// =============================================================================

export function ConfigurationPage() {
  const [activeTab, setActiveTab] = useState<TabId>('config')
  const config = useConfiguration()

  // Initial data load
  useEffect(() => {
    config.fetchConfig()
    config.fetchSecrets()
    config.fetchPrompts()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

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
          config: bundle.config,  // Legacy support
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
    { id: 'secrets', label: 'Secrets' },
    { id: 'prompts', label: 'Prompts' },
    { id: 'variables', label: 'Variables' },
    { id: 'template', label: 'Template' },
  ]

  return (
    <div className="config-page">
      <div className="config-toolbar">
        <div className="config-toolbar-left">
          <div className="config-tabs">
            {tabs.map(t => (
              <button type="button"
                key={t.id}
                className={`config-tab ${activeTab === t.id ? 'active' : ''}`}
                onClick={() => setActiveTab(t.id)}
              >
                {t.label}
              </button>
            ))}
          </div>
        </div>
        <div className="config-toolbar-right">
          <button type="button" className="config-toolbar-btn" onClick={handleImport}>Import</button>
          <button type="button" className="config-toolbar-btn" onClick={handleExport}>Export</button>
        </div>
      </div>

      <div className="config-content">
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
