import { useCallback, useState } from 'react'
import { Button } from '../ui/Button'
import { Input } from '../ui/Input'
import { coarseHitAreaCls } from '../ui/controlStyles'
import { getAgentEditorCaughtError, getAgentEditorResponseError } from './agent-editor-errors'

interface AgentVariablesEditorProps {
  definitionId?: string | null
  variables: Record<string, unknown>
  onVariablesChange: (variables: Record<string, unknown>) => void
}

export function AgentVariablesEditor({
  definitionId,
  variables,
  onVariablesChange,
}: AgentVariablesEditorProps) {
  const [newKey, setNewKey] = useState('')
  const [newValue, setNewValue] = useState('')
  const [adding, setAdding] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const entries = Object.entries(variables)

  const handleSet = useCallback(
    async (key: string, value: string) => {
      setActionError(null)
      let parsed: unknown = value
      try {
        parsed = JSON.parse(value)
      } catch {
        // Keep non-JSON values as strings.
      }
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
        if (!res.ok) {
          throw new Error(await getAgentEditorResponseError(res, 'Failed to set variable'))
        }
        const data = await res.json()
        onVariablesChange(data.variables || { ...variables, [key]: parsed })
      } catch (error) {
        setActionError(getAgentEditorCaughtError(error, 'Failed to set variable'))
      }
    },
    [definitionId, variables, onVariablesChange],
  )

  const handleRemove = useCallback(
    async (key: string) => {
      setActionError(null)
      if (!definitionId) {
        onVariablesChange(Object.fromEntries(entries.filter(([entryKey]) => entryKey !== key)))
        return
      }
      try {
        const res = await fetch(`/api/agents/definitions/${definitionId}/variables`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ remove: [key] }),
        })
        if (!res.ok) {
          throw new Error(await getAgentEditorResponseError(res, 'Failed to remove variable'))
        }
        const data = await res.json()
        onVariablesChange(
          data.variables || Object.fromEntries(entries.filter(([entryKey]) => entryKey !== key)),
        )
      } catch (error) {
        setActionError(getAgentEditorCaughtError(error, 'Failed to remove variable'))
      }
    },
    [definitionId, entries, onVariablesChange],
  )

  const handleAdd = () => {
    if (!newKey.trim()) return
    void handleSet(newKey.trim(), newValue)
    setNewKey('')
    setNewValue('')
    setAdding(false)
  }

  return (
    <div className="flex flex-col gap-2">
      {actionError && (
        <Button
          type="button"
          variant="destructive"
          size="sm"
          dense
          className={`${coarseHitAreaCls} justify-start border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive-foreground`}
          onClick={() => setActionError(null)}
          aria-label={`Dismiss error: ${actionError}`}
        >
          {actionError}
        </Button>
      )}
      {entries.length > 0 ? (
        <div className="flex flex-col gap-1">
          {entries.map(([key, value]) => (
            <div key={key} className="flex items-center gap-2 text-sm">
              <code className="min-w-20 font-semibold text-[var(--text-primary)]">{key}</code>
              <span className="flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-[var(--text-muted)]">
                {typeof value === 'string' ? value : JSON.stringify(value)}
              </span>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                dense
                className={`${coarseHitAreaCls} min-h-0 w-auto px-0.5 text-base leading-none hover:text-[var(--color-error)]`}
                onClick={() => void handleRemove(key)}
                title={`Remove ${key}`}
              >
                &times;
              </Button>
            </div>
          ))}
        </div>
      ) : (
        !adding && <span className="text-sm italic text-[var(--text-muted)]">No variables set</span>
      )}
      {adding ? (
        <div className="flex items-center gap-1.5">
          <Input
            wrapperClassName="flex-1 min-w-0"
            className="px-2 text-sm"
            value={newKey}
            onChange={(event) => setNewKey(event.target.value)}
            placeholder="Key"
            autoFocus
          />
          <Input
            wrapperClassName="flex-1 min-w-0"
            className="px-2 text-sm"
            value={newValue}
            onChange={(event) => setNewValue(event.target.value)}
            placeholder="Value"
            onKeyDown={(event) => {
              if (event.key === 'Enter') handleAdd()
            }}
          />
          <Button
            type="button"
            variant="primary"
            size="sm"
            dense
            className={coarseHitAreaCls}
            onClick={handleAdd}
            disabled={!newKey.trim()}
          >
            Add
          </Button>
          <Button
            type="button"
            size="sm"
            dense
            className={coarseHitAreaCls}
            onClick={() => setAdding(false)}
          >
            Cancel
          </Button>
        </div>
      ) : (
        <Button
          type="button"
          size="sm"
          dense
          className={`${coarseHitAreaCls} self-start`}
          onClick={() => setAdding(true)}
        >
          + Add Variable
        </Button>
      )}
    </div>
  )
}
