import { useState, useCallback } from 'react'

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
    <div className="agent-vars-editor">
      {entries.length > 0 ? (
        <div className="agent-vars-list">
          {entries.map(([key, val]) => (
            <div key={key} className="agent-vars-row">
              <code className="agent-vars-key">{key}</code>
              <span className="agent-vars-value">{typeof val === 'string' ? val : JSON.stringify(val)}</span>
              <button
                type="button"
                className="agent-rules-chip-remove"
                onClick={() => handleRemove(key)}
                title={`Remove ${key}`}
              >
                &times;
              </button>
            </div>
          ))}
        </div>
      ) : !adding && (
        <span className="agent-rules-empty">No variables set</span>
      )}
      {adding ? (
        <div className="agent-vars-add-row">
          <input
            className="agent-edit-input"
            value={newKey}
            onChange={e => setNewKey(e.target.value)}
            placeholder="Key"
            autoFocus
          />
          <input
            className="agent-edit-input"
            value={newValue}
            onChange={e => setNewValue(e.target.value)}
            placeholder="Value"
            onKeyDown={e => { if (e.key === 'Enter') handleAdd() }}
          />
          <button type="button" className="agent-defs-btn agent-defs-btn--primary" onClick={handleAdd} disabled={!newKey.trim()}>Add</button>
          <button type="button" className="agent-defs-btn" onClick={() => setAdding(false)}>Cancel</button>
        </div>
      ) : (
        <button
          type="button"
          className="agent-defs-btn agent-rules-add-btn"
          onClick={() => setAdding(true)}
        >
          + Add Variable
        </button>
      )}
    </div>
  )
}
