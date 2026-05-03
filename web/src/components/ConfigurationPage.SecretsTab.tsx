import { useState } from 'react'
import type { SecretInfo } from '../hooks/useConfiguration'
import { cn } from '../lib/utils'
import {
  EMPTY_CLS,
  INPUT_CLS,
  SECRET_ACTIONS_CLS,
  SECRET_ACTION_BTN_CLS,
  SECRET_ACTION_DELETE_CLS,
  SECRET_FORM_ACTIONS_CLS,
  SECRET_FORM_CLS,
  SECRET_FORM_ROW_CLS,
  SECRET_HINT_CLS,
  SECRET_MASKED_CLS,
  SECRETS_CLS,
  SECRETS_HEADER_CLS,
  SECRETS_HEADER_H3_CLS,
  SECRETS_TABLE_CLS,
  SECRETS_TD_CLS,
  SECRETS_TH_CLS,
  SELECT_CLS,
  TOOLBAR_BTN_CLS,
  TOOLBAR_BTN_PRIMARY_CLS,
  YAML_ERRORS_CLS,
} from './ConfigurationPage.styles'

interface SecretsTabProps {
  secrets: SecretInfo[]
  categories: string[]
  onSave: (name: string, value: string, category?: string, description?: string) => Promise<boolean>
  onDelete: (name: string) => Promise<boolean>
  onRefresh: () => void | Promise<void>
}

export function SecretsTab({ secrets, categories, onSave, onDelete, onRefresh }: SecretsTabProps) {
  const [showForm, setShowForm] = useState(false)
  const [formName, setFormName] = useState('')
  const [formValue, setFormValue] = useState('')
  const [formCategory, setFormCategory] = useState('general')
  const [formDescription, setFormDescription] = useState('')
  const [editingName, setEditingName] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const handleSubmit = async () => {
    if (!formName.trim() || !formValue.trim()) return
    setError(null)
    try {
      const ok = await onSave(formName.trim(), formValue, formCategory, formDescription || undefined)
      if (ok) {
        setShowForm(false)
        setEditingName(null)
        setFormName('')
        setFormValue('')
        setFormCategory('general')
        setFormDescription('')
        await onRefresh()
      } else {
        setError('Failed to save secret')
      }
    } catch (err) {
      console.error('Failed to save secret:', err)
      setError('Failed to save secret')
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
    setError(null)
    try {
      const ok = await onDelete(name)
      if (ok) {
        await onRefresh()
      } else {
        setError('Failed to delete secret')
      }
    } catch (err) {
      console.error('Failed to delete secret:', err)
      setError('Failed to delete secret')
    }
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
              aria-label="Secret name"
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
            aria-label="Secret value"
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

      {error && <div className={YAML_ERRORS_CLS}>{error}</div>}

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
