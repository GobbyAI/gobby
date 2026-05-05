import { useState, useCallback } from 'react'
import {
  AGENT_BTN_CLS,
  AGENT_BTN_PRIMARY_CLS,
  AGENT_EDIT_INPUT_CLS,
  AGENT_RULES_ADD_BTN_CLS,
  AGENT_RULES_CHIP_REMOVE_CLS,
  AGENT_RULES_EMPTY_CLS,
  AGENT_VARS_ADD_ROW_CLS,
  AGENT_VARS_EDITOR_CLS,
  AGENT_VARS_KEY_CLS,
  AGENT_VARS_LIST_CLS,
  AGENT_VARS_ROW_CLS,
  AGENT_VARS_VALUE_CLS,
} from './agents-styles'

interface AgentVariablesEditorProps {
  definitionId?: string | null
  variables: Record<string, unknown>
  onVariablesChange: (variables: Record<string, unknown>) => void
}

export function AgentVariablesEditor({ definitionId, variables, onVariablesChange }: AgentVariablesEditorProps) {
  const [newKey, setNewKey] = useState('')
  const [newValue, setNewValue] = useState('')
  const [adding, setAdding] = useState(false)

  const entries = Object.entries(variables)

  const handleSet = useCallback(async (key: string, value: string) => {
    let parsed: unknown = value
    try { parsed = JSON.parse(value) } catch { /* keep as string */ }
    if (!definitionId) {
      onVariablesChange({ ...variables, [key]: parsed })
      return
    }
    try {
      const res = await fetch(`/api/agents/definitions/${definitionId}/variables`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ set: { [key]: parsed } }),
      })
      if (res.ok) {
        const data = await res.json()
        onVariablesChange(data.variables || { ...variables, [key]: parsed })
      }
    } catch (e) {
      console.error('Failed to set variable:', e)
    }
  }, [definitionId, variables, onVariablesChange])

  const handleRemove = useCallback(async (key: string) => {
    if (!definitionId) {
      onVariablesChange(Object.fromEntries(entries.filter(([k]) => k !== key)))
      return
    }
    try {
      const res = await fetch(`/api/agents/definitions/${definitionId}/variables`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ remove: [key] }),
      })
      if (res.ok) {
        const data = await res.json()
        onVariablesChange(data.variables || Object.fromEntries(entries.filter(([k]) => k !== key)))
      }
    } catch (e) {
      console.error('Failed to remove variable:', e)
    }
  }, [definitionId, entries, onVariablesChange])

  const handleAdd = () => {
    if (!newKey.trim()) return
    handleSet(newKey.trim(), newValue)
    setNewKey('')
    setNewValue('')
    setAdding(false)
  }

  return (
    <div className={AGENT_VARS_EDITOR_CLS}>
      {entries.length > 0 ? (
        <div className={AGENT_VARS_LIST_CLS}>
          {entries.map(([key, val]) => (
            <div key={key} className={AGENT_VARS_ROW_CLS}>
              <code className={AGENT_VARS_KEY_CLS}>{key}</code>
              <span className={AGENT_VARS_VALUE_CLS}>{typeof val === 'string' ? val : JSON.stringify(val)}</span>
              <button
                type="button"
                className={AGENT_RULES_CHIP_REMOVE_CLS}
                onClick={() => handleRemove(key)}
                title={`Remove ${key}`}
              >
                &times;
              </button>
            </div>
          ))}
        </div>
      ) : !adding && (
        <span className={AGENT_RULES_EMPTY_CLS}>No variables set</span>
      )}
      {adding ? (
        <div className={AGENT_VARS_ADD_ROW_CLS}>
          <input
            className={AGENT_EDIT_INPUT_CLS}
            value={newKey}
            onChange={e => setNewKey(e.target.value)}
            placeholder="Key"
            autoFocus
          />
          <input
            className={AGENT_EDIT_INPUT_CLS}
            value={newValue}
            onChange={e => setNewValue(e.target.value)}
            placeholder="Value"
            onKeyDown={e => { if (e.key === 'Enter') handleAdd() }}
          />
          <button type="button" className={`${AGENT_BTN_CLS} ${AGENT_BTN_PRIMARY_CLS}`} onClick={handleAdd} disabled={!newKey.trim()}>Add</button>
          <button type="button" className={AGENT_BTN_CLS} onClick={() => setAdding(false)}>Cancel</button>
        </div>
      ) : (
        <button
          type="button"
          className={`${AGENT_BTN_CLS} ${AGENT_RULES_ADD_BTN_CLS}`}
          onClick={() => setAdding(true)}
        >
          + Add Variable
        </button>
      )}
    </div>
  )
}
