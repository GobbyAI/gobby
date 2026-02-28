import { useState, useCallback, useMemo, forwardRef, useImperativeHandle } from 'react'
import type { WorkflowDetail } from '../../hooks/useWorkflows'
import './PipelineEditor.css'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type StepType = 'exec' | 'prompt' | 'mcp' | 'invoke_pipeline' | 'activate_workflow'

interface PipelineStep {
  id: string
  [key: string]: unknown
}

interface KVPair {
  key: string
  value: string
}

const STEP_TYPES: { value: StepType; label: string; color: string }[] = [
  { value: 'exec', label: 'Exec', color: '#22d3ee' },
  { value: 'prompt', label: 'Prompt', color: '#a78bfa' },
  { value: 'mcp', label: 'MCP', color: '#60a5fa' },
  { value: 'invoke_pipeline', label: 'Pipeline', color: '#c084fc' },
  { value: 'activate_workflow', label: 'Workflow', color: '#2dd4bf' },
]

function stripTemplateWrapper(s: string): string {
  const m = s.match(/^\$\{\{\s*(.*?)\s*\}\}$/)
  return m ? m[1].trim() : s
}

function wrapTemplateExpr(s: string): string {
  return `\${{ ${s} }}`
}

function detectStepType(step: PipelineStep): StepType {
  if (step.exec != null) return 'exec'
  if (step.prompt != null) return 'prompt'
  if (step.mcp != null) return 'mcp'
  if (step.invoke_pipeline != null) return 'invoke_pipeline'
  if (step.activate_workflow != null) return 'activate_workflow'
  return 'exec'
}

function getTypeColor(type: StepType): string {
  return STEP_TYPES.find((t) => t.value === type)?.color ?? '#666'
}

function getStepPreview(step: PipelineStep): string {
  const type = detectStepType(step)
  let preview = ''
  if (type === 'exec') preview = (step.exec as string) ?? ''
  else if (type === 'prompt') preview = (step.prompt as string) ?? ''
  else if (type === 'mcp') {
    const mcp = step.mcp as Record<string, unknown> | undefined
    preview = mcp ? `${mcp.server ?? ''}/${mcp.tool ?? ''}` : ''
  } else if (type === 'invoke_pipeline') {
    const ip = step.invoke_pipeline
    preview = typeof ip === 'string' ? ip : (ip as Record<string, unknown>)?.name as string ?? ''
  } else if (type === 'activate_workflow') {
    const aw = step.activate_workflow as Record<string, unknown> | undefined
    preview = (aw?.name as string) ?? ''
  }
  return preview.length > 60 ? preview.slice(0, 57) + '...' : preview
}

function createDefaultStep(type: StepType, existingIds: string[]): PipelineStep {
  let base = 'step'
  let n = existingIds.length + 1
  while (existingIds.includes(`${base}-${n}`)) n++
  const id = `${base}-${n}`

  const step: PipelineStep = { id }
  if (type === 'exec') step.exec = ''
  else if (type === 'prompt') step.prompt = ''
  else if (type === 'mcp') step.mcp = { server: '', tool: '', arguments: {} }
  else if (type === 'invoke_pipeline') step.invoke_pipeline = ''
  else if (type === 'activate_workflow') step.activate_workflow = { name: '', session_id: '' }
  return step
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export interface PipelineEditorHandle {
  save: () => Promise<void>
  isDirty: boolean
}

interface PipelineEditorProps {
  pipeline: WorkflowDetail
  updateWorkflow: (
    id: string,
    params: { name?: string; definition_json?: string; description?: string },
  ) => Promise<WorkflowDetail | null>
  onBack: () => void
  onExport: () => void
  inSidebar?: boolean
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export const PipelineEditor = forwardRef<PipelineEditorHandle, PipelineEditorProps>(function PipelineEditor({ pipeline, updateWorkflow, onBack, onExport, inSidebar }, ref) {
  // Parse definition
  const initDef = useMemo(() => {
    try {
      return JSON.parse(pipeline.definition_json) as Record<string, unknown>
    } catch {
      return {} as Record<string, unknown>
    }
  }, [pipeline.definition_json])

  const initSteps = useMemo(
    () => (Array.isArray(initDef.steps) ? (initDef.steps as PipelineStep[]) : []),
    [initDef],
  )

  const [name, setName] = useState(pipeline.name)
  const [description, setDescription] = useState(pipeline.description ?? '')
  const [steps, setSteps] = useState<PipelineStep[]>(initSteps)
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [isDirty, setDirty] = useState(false)

  const markDirty = useCallback(() => setDirty(true), [])

  const handleBack = useCallback(() => {
    if (isDirty && !window.confirm('You have unsaved changes. Discard them?')) return
    onBack()
  }, [isDirty, onBack])

  // ---- Step mutations ----

  const updateStep = useCallback(
    (index: number, updates: Partial<PipelineStep>) => {
      setSteps((prev) => prev.map((s, i) => (i === index ? { ...s, ...updates } : s)))
      markDirty()
    },
    [markDirty],
  )

  const deleteStep = useCallback(
    (index: number) => {
      if (!window.confirm('Delete this step?')) return
      setSteps((prev) => prev.filter((_, i) => i !== index))
      setExpandedId(null)
      markDirty()
    },
    [markDirty],
  )

  const moveStep = useCallback(
    (index: number, direction: -1 | 1) => {
      setSteps((prev) => {
        const next = [...prev]
        const target = index + direction
        if (target < 0 || target >= next.length) return prev
        ;[next[index], next[target]] = [next[target], next[index]]
        return next
      })
      markDirty()
    },
    [markDirty],
  )

  const addStep = useCallback(
    (type: StepType) => {
      const ids = steps.map((s) => s.id)
      const step = createDefaultStep(type, ids)
      setSteps((prev) => [...prev, step])
      setExpandedId(step.id)
      markDirty()
    },
    [steps, markDirty],
  )

  const changeStepType = useCallback(
    (index: number, newType: StepType) => {
      setSteps((prev) =>
        prev.map((s, i) => {
          if (i !== index) return s
          // Strip old type-specific field, add new one
          const cleaned = { ...s }
          for (const t of ['exec', 'prompt', 'mcp', 'invoke_pipeline', 'activate_workflow']) {
            delete cleaned[t]
          }
          if (newType === 'exec') cleaned.exec = ''
          else if (newType === 'prompt') cleaned.prompt = ''
          else if (newType === 'mcp') cleaned.mcp = { server: '', tool: '', arguments: {} }
          else if (newType === 'invoke_pipeline') cleaned.invoke_pipeline = ''
          else if (newType === 'activate_workflow') cleaned.activate_workflow = { name: '', session_id: '' }
          return cleaned
        }),
      )
      markDirty()
    },
    [markDirty],
  )

  // ---- Save ----

  const handleSave = useCallback(async () => {
    // Validate unique IDs
    const ids = steps.map((s) => s.id)
    const dupes = ids.filter((id, i) => ids.indexOf(id) !== i)
    if (dupes.length > 0) {
      window.alert(`Duplicate step IDs: ${dupes.join(', ')}`)
      return
    }

    setSaving(true)
    try {
      // Reconstruct definition preserving unmanaged top-level fields
      const def: Record<string, unknown> = { ...initDef }
      def.name = name.trim() || pipeline.name
      def.description = description.trim() || undefined
      def.steps = steps
      await updateWorkflow(pipeline.id, {
        name: name.trim() || pipeline.name,
        description: description.trim() || undefined,
        definition_json: JSON.stringify(def),
      })
      setDirty(false)
    } catch (e) {
      window.alert(`Save failed: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setSaving(false)
    }
  }, [steps, name, description, initDef, pipeline, updateWorkflow])

  useImperativeHandle(ref, () => ({
    save: handleSave,
    isDirty,
  }), [handleSave, isDirty])

  // ---- Render ----

  return (
    <div className={`pipeline-editor${inSidebar ? ' pipeline-editor--sidebar' : ''}`}>
      {/* Header — hidden when inside sidebar */}
      {!inSidebar && (
        <div className="pipeline-editor-toolbar">
          <div className="pipeline-editor-toolbar-left">
            <button type="button" className="pipeline-editor-back" onClick={handleBack}>
              &larr;
            </button>
            <input
              className="pipeline-editor-name"
              type="text"
              value={name}
              onChange={(e) => { setName(e.target.value); markDirty() }}
              placeholder="Pipeline name"
            />
            <span className="pipeline-editor-badge">pipeline</span>
          </div>
          <div className="pipeline-editor-toolbar-right">
            <button type="button" className="pipeline-editor-btn" onClick={onExport}>
              Export YAML
            </button>
            <button
              type="button"
              className="pipeline-editor-btn pipeline-editor-btn--primary"
              onClick={handleSave}
              disabled={saving}
            >
              {saving ? 'Saving...' : 'Save'}
            </button>
          </div>
        </div>
      )}

      {/* Description */}
      <div className="pipeline-editor-meta">
        <label className="pipeline-editor-label">Description</label>
        <textarea
          className="pipeline-editor-description"
          value={description}
          onChange={(e) => { setDescription(e.target.value); markDirty() }}
          placeholder="Pipeline description..."
          rows={2}
        />
      </div>

      {/* Steps */}
      <div className="pipeline-editor-steps">
        <div className="pipeline-editor-section-header">
          Steps
          <span className="pipeline-editor-step-count">{steps.length}</span>
        </div>

        {steps.length === 0 && (
          <div className="pipeline-editor-empty">No steps yet. Add one below.</div>
        )}

        {steps.map((step, idx) => {
          const type = detectStepType(step)
          const isExpanded = expandedId === step.id

          return (
            <div className="pipeline-editor-step" key={step.id}>
              {/* Collapsed header */}
              <div
                className="pipeline-editor-step-header"
                onClick={() => setExpandedId(isExpanded ? null : step.id)}
              >
                <span
                  className="pipeline-editor-type-badge"
                  style={{ background: getTypeColor(type) + '22', color: getTypeColor(type) }}
                >
                  {type}
                </span>
                <span className="pipeline-editor-step-id">{step.id}</span>
                <span className="pipeline-editor-step-preview">{getStepPreview(step)}</span>
                <span className="pipeline-editor-step-chevron">{isExpanded ? '\u25BE' : '\u25B8'}</span>
              </div>

              {/* Expanded body */}
              {isExpanded && (
                <div className="pipeline-editor-step-body">
                  {/* Actions row */}
                  <div className="pipeline-editor-step-actions">
                    <button
                      type="button"
                      className="pipeline-editor-step-action"
                      onClick={() => moveStep(idx, -1)}
                      disabled={idx === 0}
                      title="Move up"
                    >
                      &uarr;
                    </button>
                    <button
                      type="button"
                      className="pipeline-editor-step-action"
                      onClick={() => moveStep(idx, 1)}
                      disabled={idx === steps.length - 1}
                      title="Move down"
                    >
                      &darr;
                    </button>
                    <button
                      type="button"
                      className="pipeline-editor-step-action pipeline-editor-step-action--danger"
                      onClick={() => deleteStep(idx)}
                      title="Delete step"
                    >
                      Delete
                    </button>
                  </div>

                  {/* Step ID */}
                  <div className="pipeline-editor-field">
                    <label>Step ID</label>
                    <input
                      type="text"
                      value={step.id}
                      onChange={(e) => updateStep(idx, { id: e.target.value })}
                    />
                  </div>

                  {/* Type selector */}
                  <div className="pipeline-editor-field">
                    <label>Type</label>
                    <select
                      value={type}
                      onChange={(e) => changeStepType(idx, e.target.value as StepType)}
                    >
                      {STEP_TYPES.map((t) => (
                        <option key={t.value} value={t.value}>{t.label}</option>
                      ))}
                    </select>
                  </div>

                  {/* Type-specific fields */}
                  {type === 'exec' && (
                    <ExecFields step={step} onChange={(u) => updateStep(idx, u)} />
                  )}
                  {type === 'prompt' && (
                    <PromptFields step={step} onChange={(u) => updateStep(idx, u)} />
                  )}
                  {type === 'mcp' && (
                    <McpFields step={step} onChange={(u) => updateStep(idx, u)} />
                  )}
                  {type === 'invoke_pipeline' && (
                    <InvokePipelineFields step={step} onChange={(u) => updateStep(idx, u)} />
                  )}
                  {type === 'activate_workflow' && (
                    <ActivateWorkflowFields step={step} onChange={(u) => updateStep(idx, u)} />
                  )}

                  {/* Common optional fields */}
                  <CommonFields step={step} type={type} onChange={(u) => updateStep(idx, u)} />
                </div>
              )}
            </div>
          )
        })}

        {/* Add step */}
        <AddStepButton onAdd={addStep} />
      </div>
    </div>
  )
})

// ---------------------------------------------------------------------------
// Type-specific field components
// ---------------------------------------------------------------------------

function ExecFields({ step, onChange }: { step: PipelineStep; onChange: (u: Partial<PipelineStep>) => void }) {
  return (
    <div className="pipeline-editor-field">
      <label>Command</label>
      <textarea
        className="pipeline-editor-mono"
        value={(step.exec as string) ?? ''}
        onChange={(e) => onChange({ exec: e.target.value })}
        placeholder="shell command"
        rows={3}
      />
    </div>
  )
}

function PromptFields({ step, onChange }: { step: PipelineStep; onChange: (u: Partial<PipelineStep>) => void }) {
  return (
    <div className="pipeline-editor-field">
      <label>Prompt</label>
      <textarea
        value={(step.prompt as string) ?? ''}
        onChange={(e) => onChange({ prompt: e.target.value })}
        placeholder="LLM prompt text"
        rows={4}
      />
    </div>
  )
}

function McpFields({ step, onChange }: { step: PipelineStep; onChange: (u: Partial<PipelineStep>) => void }) {
  const mcp = (step.mcp as Record<string, unknown>) ?? {}
  const args = (mcp.arguments as Record<string, string>) ?? {}

  const setMcpField = (key: string, value: unknown) => {
    onChange({ mcp: { ...mcp, [key]: value } })
  }

  const argPairs: KVPair[] = Object.entries(args).map(([key, value]) => ({ key, value: String(value) }))

  const setArgs = (pairs: KVPair[]) => {
    const obj: Record<string, string> = {}
    for (const p of pairs) if (p.key.trim()) obj[p.key] = p.value
    setMcpField('arguments', obj)
  }

  return (
    <>
      <div className="pipeline-editor-field">
        <label>Server</label>
        <input
          type="text"
          value={(mcp.server as string) ?? ''}
          onChange={(e) => setMcpField('server', e.target.value)}
        />
      </div>
      <div className="pipeline-editor-field">
        <label>Tool</label>
        <input
          type="text"
          value={(mcp.tool as string) ?? ''}
          onChange={(e) => setMcpField('tool', e.target.value)}
        />
      </div>
      <div className="pipeline-editor-field">
        <label>Arguments</label>
        <KeyValueEditor pairs={argPairs} onChange={setArgs} />
      </div>
    </>
  )
}

function InvokePipelineFields({ step, onChange }: { step: PipelineStep; onChange: (u: Partial<PipelineStep>) => void }) {
  const raw = step.invoke_pipeline
  const isObject = typeof raw === 'object' && raw !== null
  const name = isObject ? ((raw as Record<string, unknown>).name as string) ?? '' : (raw as string) ?? ''
  const args = isObject ? ((raw as Record<string, unknown>).arguments as Record<string, string>) ?? {} : {}
  const argPairs: KVPair[] = Object.entries(args).map(([key, value]) => ({ key, value: String(value) }))

  const setName = (n: string) => {
    if (isObject || argPairs.length > 0) {
      onChange({ invoke_pipeline: { ...(isObject ? raw : {}), name: n } })
    } else {
      onChange({ invoke_pipeline: n })
    }
  }

  const setArgs = (pairs: KVPair[]) => {
    const obj: Record<string, string> = {}
    for (const p of pairs) if (p.key.trim()) obj[p.key] = p.value
    if (Object.keys(obj).length === 0 && !isObject) {
      return
    }
    onChange({ invoke_pipeline: { name, arguments: obj } })
  }

  return (
    <>
      <div className="pipeline-editor-field">
        <label>Pipeline Name</label>
        <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="pipeline-name" />
      </div>
      <div className="pipeline-editor-field">
        <label>Arguments</label>
        <KeyValueEditor pairs={argPairs} onChange={setArgs} />
      </div>
    </>
  )
}

function ActivateWorkflowFields({ step, onChange }: { step: PipelineStep; onChange: (u: Partial<PipelineStep>) => void }) {
  const aw = (step.activate_workflow as Record<string, unknown>) ?? {}
  const vars = (aw.variables as Record<string, string>) ?? {}

  const setAwField = (key: string, value: unknown) => {
    onChange({ activate_workflow: { ...aw, [key]: value } })
  }

  const varPairs: KVPair[] = Object.entries(vars).map(([key, value]) => ({ key, value: String(value) }))

  const setVars = (pairs: KVPair[]) => {
    const obj: Record<string, string> = {}
    for (const p of pairs) if (p.key.trim()) obj[p.key] = p.value
    setAwField('variables', obj)
  }

  return (
    <>
      <div className="pipeline-editor-field">
        <label>Workflow Name</label>
        <input
          type="text"
          value={(aw.name as string) ?? ''}
          onChange={(e) => setAwField('name', e.target.value)}
        />
      </div>
      <div className="pipeline-editor-field">
        <label>Session ID</label>
        <input
          type="text"
          value={(aw.session_id as string) ?? ''}
          onChange={(e) => setAwField('session_id', e.target.value)}
          placeholder="Optional"
        />
      </div>
      <div className="pipeline-editor-field">
        <label>Variables</label>
        <KeyValueEditor pairs={varPairs} onChange={setVars} />
      </div>
    </>
  )
}

// ---------------------------------------------------------------------------
// Common optional fields
// ---------------------------------------------------------------------------

function CommonFields({
  step,
  type,
  onChange,
}: {
  step: PipelineStep
  type: StepType
  onChange: (u: Partial<PipelineStep>) => void
}) {
  const approval = step.approval as Record<string, unknown> | undefined

  return (
    <div className="pipeline-editor-common">
      <div className="pipeline-editor-field">
        <label>Condition</label>
        <input
          type="text"
          value={stripTemplateWrapper((step.condition as string) ?? '')}
          onChange={(e) => {
            const val = e.target.value.trim()
            onChange({ condition: val ? wrapTemplateExpr(val) : undefined })
          }}
          placeholder="e.g. inputs.mode == 'deploy'"
        />
      </div>

      <div className="pipeline-editor-field">
        <label>Input</label>
        <input
          type="text"
          value={(step.input as string) ?? ''}
          onChange={(e) => onChange({ input: e.target.value || undefined })}
          placeholder="e.g. $prev_step.output"
        />
      </div>

      {type === 'prompt' && (
        <div className="pipeline-editor-field">
          <label>Tools</label>
          <input
            type="text"
            value={Array.isArray(step.tools) ? (step.tools as string[]).join(', ') : ''}
            onChange={(e) => {
              const val = e.target.value.trim()
              onChange({ tools: val ? val.split(',').map((s) => s.trim()).filter(Boolean) : undefined })
            }}
            placeholder="Comma-separated tool list"
          />
        </div>
      )}

      <div className="pipeline-editor-field">
        <label className="pipeline-editor-checkbox-label">
          <input
            type="checkbox"
            checked={!!approval?.required}
            onChange={(e) => {
              if (e.target.checked) {
                onChange({ approval: { required: true, message: '', timeout: 0 } })
              } else {
                onChange({ approval: undefined })
              }
            }}
          />
          Requires approval
        </label>
      </div>

      {!!approval?.required && (
        <>
          <div className="pipeline-editor-field">
            <label>Approval Message</label>
            <input
              type="text"
              value={(approval.message as string) ?? ''}
              onChange={(e) =>
                onChange({ approval: { ...approval, message: e.target.value } })
              }
              placeholder="Approval prompt message"
            />
          </div>
          <div className="pipeline-editor-field">
            <label>Timeout (seconds)</label>
            <input
              type="number"
              value={(approval.timeout as number) ?? 0}
              onChange={(e) =>
                onChange({ approval: { ...approval, timeout: Number(e.target.value) || 0 } })
              }
              min={0}
            />
          </div>
        </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Key-value pair editor
// ---------------------------------------------------------------------------

function KeyValueEditor({
  pairs,
  onChange,
}: {
  pairs: KVPair[]
  onChange: (pairs: KVPair[]) => void
}) {
  return (
    <div className="pipeline-editor-kv">
      {pairs.map((p, i) => (
        <div key={i} className="pipeline-editor-kv-row">
          <input
            type="text"
            value={p.key}
            onChange={(e) => {
              const next = [...pairs]
              next[i] = { ...next[i], key: e.target.value }
              onChange(next)
            }}
            placeholder="key"
          />
          <input
            type="text"
            value={p.value}
            onChange={(e) => {
              const next = [...pairs]
              next[i] = { ...next[i], value: e.target.value }
              onChange(next)
            }}
            placeholder="value"
          />
          <button
            type="button"
            className="pipeline-editor-kv-remove"
            onClick={() => onChange(pairs.filter((_, j) => j !== i))}
          >
            &times;
          </button>
        </div>
      ))}
      <button
        type="button"
        className="pipeline-editor-kv-add"
        onClick={() => onChange([...pairs, { key: '', value: '' }])}
      >
        + Add
      </button>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Add step button with type dropdown
// ---------------------------------------------------------------------------

function AddStepButton({ onAdd }: { onAdd: (type: StepType) => void }) {
  const [open, setOpen] = useState(false)

  return (
    <div className="pipeline-editor-add">
      <button
        type="button"
        className="pipeline-editor-add-btn"
        onClick={() => setOpen(!open)}
      >
        + Add Step
      </button>
      {open && (
        <div className="pipeline-editor-add-dropdown">
          {STEP_TYPES.map((t) => (
            <button
              key={t.value}
              type="button"
              className="pipeline-editor-add-option"
              onClick={() => { onAdd(t.value); setOpen(false) }}
            >
              <span
                className="pipeline-editor-add-dot"
                style={{ background: t.color }}
              />
              {t.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
