import { useState, useCallback } from 'react'
import './AgentStepsEditor.css'

// ---------------------------------------------------------------------------
// Types (mirrors WorkflowStep / WorkflowTransition from definitions.py)
// ---------------------------------------------------------------------------

export interface WorkflowTransition {
  to: string
  when: string
  on_transition?: Record<string, unknown>[]
}

export interface WorkflowStep {
  name: string
  description?: string | null
  status_message?: string | null
  allowed_tools?: string[] | 'all'
  blocked_tools?: string[]
  allowed_mcp_tools?: string[] | 'all'
  blocked_mcp_tools?: string[]
  transitions?: WorkflowTransition[]
  exit_when?: string | null
  on_enter?: Record<string, unknown>[]
  on_exit?: Record<string, unknown>[]
  on_mcp_success?: Record<string, unknown>[]
  on_mcp_error?: Record<string, unknown>[]
}

interface AgentStepsEditorProps {
  steps: WorkflowStep[]
  onChange: (steps: WorkflowStep[]) => void
  stepNames?: string[]  // all step names for transition dropdown
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function createDefaultStep(existing: WorkflowStep[]): WorkflowStep {
  const names = new Set(existing.map(s => s.name))
  let n = existing.length + 1
  while (names.has(`step-${n}`)) n++
  return {
    name: `step-${n}`,
    allowed_tools: 'all',
    blocked_tools: [],
    allowed_mcp_tools: 'all',
    blocked_mcp_tools: [],
    transitions: [],
  }
}

function getStepPreview(step: WorkflowStep): string {
  const parts: string[] = []
  if (step.description) {
    const desc = step.description.length > 50 ? step.description.slice(0, 47) + '...' : step.description
    parts.push(desc)
  }
  if (step.transitions && step.transitions.length > 0) {
    parts.push(`${step.transitions.length} transition${step.transitions.length > 1 ? 's' : ''}`)
  }
  return parts.join(' \u2014 ')
}

// ---------------------------------------------------------------------------
// Chip Input — reusable list-of-strings editor
// ---------------------------------------------------------------------------

function ChipInput({ values, onChange, placeholder }: {
  values: string[]
  onChange: (values: string[]) => void
  placeholder?: string
}) {
  const [input, setInput] = useState('')

  const handleAdd = () => {
    const v = input.trim()
    if (v && !values.includes(v)) {
      onChange([...values, v])
    }
    setInput('')
  }

  return (
    <div className="step-chip-input">
      <div className="step-chips">
        {values.map(v => (
          <span key={v} className="step-chip">
            {v}
            <button type="button" className="step-chip-remove" onClick={() => onChange(values.filter(x => x !== v))}>&times;</button>
          </span>
        ))}
      </div>
      <div className="step-chip-add-row">
        <input
          className="agent-edit-input step-chip-field"
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); handleAdd() } }}
          placeholder={placeholder}
        />
        <button type="button" className="agent-defs-btn step-chip-add-btn" onClick={handleAdd} disabled={!input.trim()}>+</button>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Tool Gating Section
// ---------------------------------------------------------------------------

function ToolGatingSection({ step, onChange }: { step: WorkflowStep; onChange: (s: Partial<WorkflowStep>) => void }) {
  const isAllowedAll = step.allowed_tools === 'all'
  const isMcpAllowedAll = step.allowed_mcp_tools === 'all'

  return (
    <div className="step-section">
      <h5 className="step-section-label">Tool Gating</h5>

      {/* Allowed Tools */}
      <div className="step-field">
        <label className="step-field-label">
          Allowed Tools
          <select
            className="agent-edit-input step-toggle-select"
            value={isAllowedAll ? 'all' : 'list'}
            onChange={e => onChange({ allowed_tools: e.target.value === 'all' ? 'all' : [] })}
          >
            <option value="all">All</option>
            <option value="list">Specific list</option>
          </select>
        </label>
        {!isAllowedAll && (
          <ChipInput
            values={step.allowed_tools as string[]}
            onChange={v => onChange({ allowed_tools: v })}
            placeholder="Tool name..."
          />
        )}
      </div>

      {/* Blocked Tools */}
      <div className="step-field">
        <label className="step-field-label">Blocked Tools</label>
        <ChipInput
          values={step.blocked_tools || []}
          onChange={v => onChange({ blocked_tools: v })}
          placeholder="Tool to block..."
        />
      </div>

      {/* Allowed MCP Tools */}
      <div className="step-field">
        <label className="step-field-label">
          Allowed MCP Tools
          <select
            className="agent-edit-input step-toggle-select"
            value={isMcpAllowedAll ? 'all' : 'list'}
            onChange={e => onChange({ allowed_mcp_tools: e.target.value === 'all' ? 'all' : [] })}
          >
            <option value="all">All</option>
            <option value="list">Specific list</option>
          </select>
        </label>
        {!isMcpAllowedAll && (
          <ChipInput
            values={step.allowed_mcp_tools as string[]}
            onChange={v => onChange({ allowed_mcp_tools: v })}
            placeholder="server:tool..."
          />
        )}
      </div>

      {/* Blocked MCP Tools */}
      <div className="step-field">
        <label className="step-field-label">Blocked MCP Tools</label>
        <ChipInput
          values={step.blocked_mcp_tools || []}
          onChange={v => onChange({ blocked_mcp_tools: v })}
          placeholder="server:tool..."
        />
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Transitions Section
// ---------------------------------------------------------------------------

function TransitionsSection({ step, onChange, allStepNames }: {
  step: WorkflowStep
  onChange: (s: Partial<WorkflowStep>) => void
  allStepNames: string[]
}) {
  const transitions = step.transitions || []

  const updateTransition = (idx: number, updates: Partial<WorkflowTransition>) => {
    const next = transitions.map((t, i) => i === idx ? { ...t, ...updates } : t)
    onChange({ transitions: next })
  }

  const addTransition = () => {
    const otherNames = allStepNames.filter(n => n !== step.name)
    onChange({ transitions: [...transitions, { to: otherNames[0] || '', when: '' }] })
  }

  const removeTransition = (idx: number) => {
    onChange({ transitions: transitions.filter((_, i) => i !== idx) })
  }

  return (
    <div className="step-section">
      <h5 className="step-section-label">Transitions</h5>
      {transitions.map((t, idx) => (
        <div key={idx} className="step-transition-row">
          <select
            className="agent-edit-input step-transition-to"
            value={t.to}
            onChange={e => updateTransition(idx, { to: e.target.value })}
          >
            <option value="">(select step)</option>
            {allStepNames.filter(n => n !== step.name).map(n => (
              <option key={n} value={n}>{n}</option>
            ))}
          </select>
          <input
            className="agent-edit-input step-transition-when"
            value={t.when}
            onChange={e => updateTransition(idx, { when: e.target.value })}
            placeholder="when expression..."
          />
          <button type="button" className="step-chip-remove" onClick={() => removeTransition(idx)}>&times;</button>
        </div>
      ))}
      <button type="button" className="agent-defs-btn agent-rules-add-btn" onClick={addTransition}>+ Add Transition</button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Advanced Section (JSON editors for hooks)
// ---------------------------------------------------------------------------

function AdvancedSection({ step, onChange }: { step: WorkflowStep; onChange: (s: Partial<WorkflowStep>) => void }) {
  const [expanded, setExpanded] = useState(false)

  const fields: { key: keyof WorkflowStep; label: string }[] = [
    { key: 'on_enter', label: 'on_enter' },
    { key: 'on_exit', label: 'on_exit' },
    { key: 'on_mcp_success', label: 'on_mcp_success' },
    { key: 'on_mcp_error', label: 'on_mcp_error' },
  ]

  return (
    <div className="step-section">
      <button type="button" className="step-advanced-toggle" onClick={() => setExpanded(!expanded)}>
        <span className="step-chevron">{expanded ? '\u25BE' : '\u25B8'}</span>
        Advanced
      </button>
      {expanded && (
        <div className="step-advanced-fields">
          {fields.map(({ key, label }) => {
            const val = step[key] as Record<string, unknown>[] | undefined
            return (
              <div key={key} className="step-field">
                <label className="step-field-label">{label}</label>
                <textarea
                  className="agent-edit-input agent-edit-textarea step-json-editor"
                  value={val && val.length > 0 ? JSON.stringify(val, null, 2) : ''}
                  onChange={e => {
                    const text = e.target.value.trim()
                    if (!text) {
                      onChange({ [key]: [] })
                      return
                    }
                    try {
                      const parsed = JSON.parse(text)
                      if (Array.isArray(parsed)) onChange({ [key]: parsed })
                    } catch {
                      // Parse errors expected while user is typing — no-op
                    }
                  }}
                  rows={3}
                  placeholder="[]"
                />
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main Component
// ---------------------------------------------------------------------------

export function AgentStepsEditor({ steps, onChange }: AgentStepsEditorProps) {
  const [expandedName, setExpandedName] = useState<string | null>(null)

  const allStepNames = steps.map(s => s.name)

  const updateStep = useCallback((idx: number, updates: Partial<WorkflowStep>) => {
    onChange(steps.map((s, i) => i === idx ? { ...s, ...updates } : s))
  }, [steps, onChange])

  const deleteStep = useCallback((idx: number) => {
    const name = steps[idx].name
    onChange(steps.filter((_, i) => i !== idx))
    if (expandedName === name) setExpandedName(null)
  }, [steps, onChange, expandedName])

  const moveStep = useCallback((idx: number, dir: -1 | 1) => {
    const target = idx + dir
    if (target < 0 || target >= steps.length) return
    const next = [...steps]
    ;[next[idx], next[target]] = [next[target], next[idx]]
    onChange(next)
  }, [steps, onChange])

  const addStep = useCallback(() => {
    const step = createDefaultStep(steps)
    onChange([...steps, step])
    setExpandedName(step.name)
  }, [steps, onChange])

  return (
    <div className="step-editor">
      {steps.length === 0 && (
        <span className="agent-rules-empty">No steps defined</span>
      )}

      {steps.map((step, idx) => {
        const isExpanded = expandedName === step.name

        return (
          <div className={`step-card${isExpanded ? ' step-card--expanded' : ''}`} key={`${step.name}-${idx}`}>
            {/* Header */}
            <div
              className="step-card-header"
              onClick={() => setExpandedName(isExpanded ? null : step.name)}
            >
              <span className="step-name-badge">{step.name}</span>
              <span className="step-preview">{getStepPreview(step)}</span>
              <span className="step-chevron">{isExpanded ? '\u25BE' : '\u25B8'}</span>
            </div>

            {/* Expanded body */}
            {isExpanded && (
              <div className="step-card-body">
                {/* Actions */}
                <div className="step-actions">
                  <button type="button" className="agent-defs-btn" onClick={() => moveStep(idx, -1)} disabled={idx === 0} title="Move up">&uarr;</button>
                  <button type="button" className="agent-defs-btn" onClick={() => moveStep(idx, 1)} disabled={idx === steps.length - 1} title="Move down">&darr;</button>
                  <button type="button" className="agent-defs-btn agent-defs-btn--danger" onClick={() => deleteStep(idx)}>Delete</button>
                </div>

                {/* Identity */}
                <div className="step-field">
                  <label className="step-field-label">Name</label>
                  <input
                    className="agent-edit-input"
                    value={step.name}
                    onChange={e => {
                      const newName = e.target.value
                      updateStep(idx, { name: newName })
                      setExpandedName(newName)
                    }}
                  />
                </div>
                <div className="step-field">
                  <label className="step-field-label">Description</label>
                  <textarea
                    className="agent-edit-input agent-edit-textarea"
                    value={step.description || ''}
                    onChange={e => updateStep(idx, { description: e.target.value || null })}
                    placeholder="What this step does..."
                    rows={2}
                  />
                </div>
                <div className="step-field">
                  <label className="step-field-label">Status Message</label>
                  <textarea
                    className="agent-edit-input agent-edit-textarea"
                    value={step.status_message || ''}
                    onChange={e => updateStep(idx, { status_message: e.target.value || null })}
                    placeholder="Shown while step is active..."
                    rows={2}
                  />
                </div>

                {/* Tool Gating */}
                <ToolGatingSection step={step} onChange={updates => updateStep(idx, updates)} />

                {/* Transitions */}
                <TransitionsSection
                  step={step}
                  onChange={updates => updateStep(idx, updates)}
                  allStepNames={allStepNames}
                />

                {/* Exit When */}
                <div className="step-field">
                  <label className="step-field-label">Exit When</label>
                  <input
                    className="agent-edit-input"
                    value={step.exit_when || ''}
                    onChange={e => updateStep(idx, { exit_when: e.target.value || null })}
                    placeholder="Expression to auto-exit this step..."
                  />
                </div>

                {/* Advanced */}
                <AdvancedSection step={step} onChange={updates => updateStep(idx, updates)} />
              </div>
            )}
          </div>
        )
      })}

      <button type="button" className="agent-defs-btn agent-rules-add-btn" onClick={addStep}>+ Add Step</button>
    </div>
  )
}
