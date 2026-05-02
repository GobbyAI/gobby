import { useState, useCallback, useMemo, forwardRef, useImperativeHandle } from 'react'
import type { WorkflowDetail } from '../../hooks/useWorkflows'
import { useConfirmDialog } from '../../hooks/useConfirmDialog'

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

// Step-type accent colors stay as raw hex pending the colorize pass —
// see .impeccable.md "Blue accent → green migration" tracked separately.
const STEP_TYPES: { value: StepType; label: string; color: string }[] = [
  { value: 'exec', label: 'Exec', color: '#22d3ee' },
  { value: 'prompt', label: 'Prompt', color: '#a78bfa' },
  { value: 'mcp', label: 'MCP', color: '#60a5fa' },
  { value: 'invoke_pipeline', label: 'Pipeline', color: '#c084fc' },
  { value: 'activate_workflow', label: 'Workflow', color: '#2dd4bf' },
]

// ---------------------------------------------------------------------------
// Class constants — Tailwind migration of PipelineEditor.css
// ---------------------------------------------------------------------------

const EDITOR_CLS = 'flex h-full flex-1 flex-col overflow-hidden'
const EDITOR_SIDEBAR_CLS = '!h-auto !overflow-visible'

const TOOLBAR_CLS =
  'flex flex-shrink-0 items-center justify-between gap-4 border-b border-[var(--border)] bg-[var(--bg-secondary)] px-4 py-2.5'
const TOOLBAR_LEFT_CLS = 'flex items-center gap-2.5'
const TOOLBAR_RIGHT_CLS = 'flex items-center gap-2'

const BACK_CLS =
  'cursor-pointer rounded-md border border-[var(--border)] bg-[var(--bg-tertiary)] px-3 py-1.5 text-[length:var(--text-base)] text-[var(--text-primary)] transition-colors duration-150 hover:bg-[var(--border)] pointer-coarse:min-h-11'

const NAME_CLS =
  'w-[240px] cursor-text rounded-md border border-transparent bg-transparent px-2.5 py-1 text-[length:var(--text-base)] font-semibold text-[var(--text-primary)] outline-none transition-colors duration-150 hover:bg-[var(--bg-tertiary)] focus:border-[var(--accent)] focus:bg-[var(--bg-primary)]'

const BADGE_CLS =
  'inline-block rounded-[10px] bg-[var(--color-agent-soft)] px-2 py-0.5 text-[length:var(--text-2xs)] font-medium uppercase tracking-[0.5px] text-[var(--color-agent)]'

const BTN_CLS =
  'cursor-pointer rounded-md border border-[var(--border)] bg-[var(--bg-tertiary)] px-3 py-1.5 text-[length:var(--text-sm)] text-[var(--text-primary)] transition-colors duration-150 hover:bg-[var(--border)] disabled:cursor-not-allowed disabled:opacity-60 pointer-coarse:min-h-11'

const BTN_PRIMARY_CLS =
  '!border-[var(--accent)] !bg-[var(--accent)] font-medium !text-[var(--accent-foreground)] hover:!border-[var(--accent-hover)] hover:!bg-[var(--accent-hover)]'

const META_CLS = 'flex-shrink-0 border-b border-[var(--border)] px-4 py-3'

const LABEL_CLS =
  'mb-1 block text-[length:var(--text-xs)] font-semibold uppercase tracking-[0.5px] text-[var(--text-secondary)]'

const DESCRIPTION_CLS =
  'box-border min-h-[40px] w-full resize-y rounded-md border border-[var(--border)] bg-[var(--bg-primary)] px-2.5 py-2 font-[inherit] text-[length:var(--text-md)] text-[var(--text-primary)] outline-none focus:border-[var(--accent)]'

const STEPS_CLS = 'flex-1 overflow-y-auto px-4 pt-3 pb-5'
const STEPS_SIDEBAR_CLS = '!overflow-visible !pb-0'

const SECTION_HEADER_CLS =
  'mb-2.5 flex items-center gap-2 text-[length:var(--text-xs)] font-semibold uppercase tracking-[0.5px] text-[var(--text-secondary)]'

const STEP_COUNT_CLS =
  'rounded-[10px] bg-[var(--bg-tertiary)] px-1.5 py-px text-[length:var(--text-2xs)] text-[var(--text-secondary)]'

const EMPTY_CLS = 'p-6 text-center text-[length:var(--text-md)] text-[var(--text-secondary)]'

const STEP_CLS =
  'mb-2 overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)]'

const STEP_HEADER_CLS =
  'flex cursor-pointer items-center gap-2 px-3 py-2.5 transition-colors duration-100 hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11'

const TYPE_BADGE_CLS =
  'inline-block flex-shrink-0 rounded-[10px] px-2 py-0.5 text-[length:var(--text-2xs)] font-medium'

const STEP_ID_CLS =
  'flex-shrink-0 text-[length:var(--text-md)] font-medium text-[var(--text-primary)]'

const STEP_PREVIEW_CLS =
  'min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-[length:var(--text-sm)] text-[var(--text-secondary)]'

const STEP_CHEVRON_CLS =
  'ml-auto flex-shrink-0 text-[length:var(--text-sm)] text-[var(--text-secondary)]'

const STEP_BODY_CLS = 'border-t border-[var(--border)] px-3 pb-3'

const STEP_ACTIONS_CLS = 'flex gap-1.5 py-2'

const STEP_ACTION_CLS =
  'cursor-pointer rounded border border-[var(--border)] bg-transparent px-2.5 py-1 text-[length:var(--text-xs)] text-[var(--text-secondary)] transition-all duration-150 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] disabled:cursor-not-allowed disabled:opacity-40 pointer-coarse:min-h-11'

const STEP_ACTION_DANGER_CLS =
  'hover:!border-[var(--color-destructive)] hover:!bg-[var(--color-destructive)] hover:!text-[var(--color-destructive-foreground)]'

const FIELD_CLS = 'mb-2.5'

const FIELD_LABEL_CLS =
  'mb-1 block text-[length:var(--text-xs)] font-medium text-[var(--text-secondary)]'

const FIELD_INPUT_CLS =
  'box-border w-full rounded border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-1.5 text-[length:var(--text-md)] text-[var(--text-primary)] outline-none focus:border-[var(--accent)]'

const FIELD_TEXTAREA_CLS = `${FIELD_INPUT_CLS} min-h-[50px] resize-y font-[inherit]`

const FIELD_TEXTAREA_MONO_CLS = `${FIELD_TEXTAREA_CLS} font-mono text-[length:var(--text-sm)]`

const FIELD_SELECT_CLS = `${FIELD_INPUT_CLS} cursor-pointer`

const CHECKBOX_LABEL_CLS =
  'flex cursor-pointer items-center gap-1.5 text-[length:var(--text-sm)] [&>input]:w-auto'

const COMMON_CLS = 'mt-2 border-t border-[var(--border)] pt-2'

const KV_CLS = 'flex flex-col gap-1'
const KV_ROW_CLS = 'flex items-center gap-1'
const KV_INPUT_CLS =
  'box-border flex-1 rounded border border-[var(--border)] bg-[var(--bg-primary)] px-2 py-1 text-[length:var(--text-sm)] text-[var(--text-primary)] outline-none focus:border-[var(--accent)]'

const KV_REMOVE_CLS =
  'flex-shrink-0 cursor-pointer rounded border border-[var(--border)] bg-transparent px-1.5 py-0.5 text-[length:var(--text-base)] leading-none text-[var(--text-secondary)] hover:border-[var(--color-destructive)] hover:bg-[var(--color-destructive)] hover:text-[var(--color-destructive-foreground)] pointer-coarse:min-h-11 pointer-coarse:min-w-11'

const KV_ADD_CLS =
  'cursor-pointer rounded border border-dashed border-[var(--border)] bg-transparent px-2 py-1 text-left text-[length:var(--text-xs)] text-[var(--text-secondary)] hover:border-[var(--accent)] hover:text-[var(--text-primary)]'

const ADD_CLS = 'relative mt-2'

const ADD_BTN_CLS =
  'w-full cursor-pointer rounded-lg border border-dashed border-[var(--border)] bg-transparent p-2.5 text-[length:var(--text-md)] text-[var(--text-secondary)] transition-all duration-150 hover:border-[var(--accent)] hover:bg-[var(--bg-secondary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11'

const ADD_DROPDOWN_CLS =
  'absolute bottom-full left-0 z-10 mb-1 min-w-[160px] rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)] p-1 shadow-[var(--shadow-md)]'

const ADD_OPTION_CLS =
  'flex w-full cursor-pointer items-center gap-2 rounded-md border-0 bg-transparent px-2.5 py-2 text-left text-[length:var(--text-md)] text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11'

const ADD_DOT_CLS = 'h-2 w-2 flex-shrink-0 rounded-full'

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
  const base = 'step'
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
  const { confirm, ConfirmDialogElement } = useConfirmDialog()

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

  const handleBack = useCallback(async () => {
    if (isDirty && !await confirm({ title: 'Unsaved changes', description: 'You have unsaved changes. Discard them?', confirmLabel: 'Discard', destructive: true })) return
    onBack()
  }, [isDirty, onBack, confirm])

  const updateStep = useCallback(
    (index: number, updates: Partial<PipelineStep>) => {
      setSteps((prev) => prev.map((s, i) => (i === index ? { ...s, ...updates } : s)))
      markDirty()
    },
    [markDirty],
  )

  const deleteStep = useCallback(
    async (index: number) => {
      if (!await confirm({ title: 'Delete step?', confirmLabel: 'Delete', destructive: true })) return
      setSteps((prev) => prev.filter((_, i) => i !== index))
      setExpandedId(null)
      markDirty()
    },
    [markDirty, confirm],
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

  const handleSave = useCallback(async () => {
    const ids = steps.map((s) => s.id)
    const dupes = ids.filter((id, i) => ids.indexOf(id) !== i)
    if (dupes.length > 0) {
      window.alert(`Duplicate step IDs: ${dupes.join(', ')}`)
      return
    }

    setSaving(true)
    try {
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

  return (
    <div className={`${EDITOR_CLS}${inSidebar ? ` ${EDITOR_SIDEBAR_CLS}` : ''}`}>
      {ConfirmDialogElement}
      {!inSidebar && (
        <div className={TOOLBAR_CLS}>
          <div className={TOOLBAR_LEFT_CLS}>
            <button type="button" className={BACK_CLS} onClick={handleBack}>
              &larr;
            </button>
            <input
              className={NAME_CLS}
              type="text"
              value={name}
              onChange={(e) => { setName(e.target.value); markDirty() }}
              placeholder="Pipeline name"
            />
            <span className={BADGE_CLS}>pipeline</span>
          </div>
          <div className={TOOLBAR_RIGHT_CLS}>
            <button type="button" className={BTN_CLS} onClick={onExport}>
              Export YAML
            </button>
            <button
              type="button"
              className={`${BTN_CLS} ${BTN_PRIMARY_CLS}`}
              onClick={handleSave}
              disabled={saving}
            >
              {saving ? 'Saving...' : 'Save'}
            </button>
          </div>
        </div>
      )}

      <div className={META_CLS}>
        <label className={LABEL_CLS}>Description</label>
        <textarea
          className={DESCRIPTION_CLS}
          value={description}
          onChange={(e) => { setDescription(e.target.value); markDirty() }}
          placeholder="Pipeline description..."
          rows={2}
        />
      </div>

      <div className={`${STEPS_CLS}${inSidebar ? ` ${STEPS_SIDEBAR_CLS}` : ''}`}>
        <div className={SECTION_HEADER_CLS}>
          Steps
          <span className={STEP_COUNT_CLS}>{steps.length}</span>
        </div>

        {steps.length === 0 && (
          <div className={EMPTY_CLS}>No steps yet. Add one below.</div>
        )}

        {steps.map((step, idx) => {
          const type = detectStepType(step)
          const isExpanded = expandedId === step.id

          return (
            <div className={STEP_CLS} key={step.id}>
              <div
                className={STEP_HEADER_CLS}
                onClick={() => setExpandedId(isExpanded ? null : step.id)}
              >
                <span
                  className={TYPE_BADGE_CLS}
                  style={{ background: getTypeColor(type) + '22', color: getTypeColor(type) }}
                >
                  {type}
                </span>
                <span className={STEP_ID_CLS}>{step.id}</span>
                <span className={STEP_PREVIEW_CLS}>{getStepPreview(step)}</span>
                <span className={STEP_CHEVRON_CLS}>{isExpanded ? '▾' : '▸'}</span>
              </div>

              {isExpanded && (
                <div className={STEP_BODY_CLS}>
                  <div className={STEP_ACTIONS_CLS}>
                    <button
                      type="button"
                      className={STEP_ACTION_CLS}
                      onClick={() => moveStep(idx, -1)}
                      disabled={idx === 0}
                      title="Move up"
                    >
                      &uarr;
                    </button>
                    <button
                      type="button"
                      className={STEP_ACTION_CLS}
                      onClick={() => moveStep(idx, 1)}
                      disabled={idx === steps.length - 1}
                      title="Move down"
                    >
                      &darr;
                    </button>
                    <button
                      type="button"
                      className={`${STEP_ACTION_CLS} ${STEP_ACTION_DANGER_CLS}`}
                      onClick={() => deleteStep(idx)}
                      title="Delete step"
                    >
                      Delete
                    </button>
                  </div>

                  <div className={FIELD_CLS}>
                    <label className={FIELD_LABEL_CLS}>Step ID</label>
                    <input
                      type="text"
                      className={FIELD_INPUT_CLS}
                      value={step.id}
                      onChange={(e) => updateStep(idx, { id: e.target.value })}
                    />
                  </div>

                  <div className={FIELD_CLS}>
                    <label className={FIELD_LABEL_CLS}>Type</label>
                    <select
                      className={FIELD_SELECT_CLS}
                      value={type}
                      onChange={(e) => changeStepType(idx, e.target.value as StepType)}
                    >
                      {STEP_TYPES.map((t) => (
                        <option key={t.value} value={t.value}>{t.label}</option>
                      ))}
                    </select>
                  </div>

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

                  <CommonFields step={step} type={type} onChange={(u) => updateStep(idx, u)} />
                </div>
              )}
            </div>
          )
        })}

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
    <div className={FIELD_CLS}>
      <label className={FIELD_LABEL_CLS}>Command</label>
      <textarea
        className={FIELD_TEXTAREA_MONO_CLS}
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
    <div className={FIELD_CLS}>
      <label className={FIELD_LABEL_CLS}>Prompt</label>
      <textarea
        className={FIELD_TEXTAREA_CLS}
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
      <div className={FIELD_CLS}>
        <label className={FIELD_LABEL_CLS}>Server</label>
        <input
          type="text"
          className={FIELD_INPUT_CLS}
          value={(mcp.server as string) ?? ''}
          onChange={(e) => setMcpField('server', e.target.value)}
        />
      </div>
      <div className={FIELD_CLS}>
        <label className={FIELD_LABEL_CLS}>Tool</label>
        <input
          type="text"
          className={FIELD_INPUT_CLS}
          value={(mcp.tool as string) ?? ''}
          onChange={(e) => setMcpField('tool', e.target.value)}
        />
      </div>
      <div className={FIELD_CLS}>
        <label className={FIELD_LABEL_CLS}>Arguments</label>
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
      <div className={FIELD_CLS}>
        <label className={FIELD_LABEL_CLS}>Pipeline Name</label>
        <input type="text" className={FIELD_INPUT_CLS} value={name} onChange={(e) => setName(e.target.value)} placeholder="pipeline-name" />
      </div>
      <div className={FIELD_CLS}>
        <label className={FIELD_LABEL_CLS}>Arguments</label>
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
      <div className={FIELD_CLS}>
        <label className={FIELD_LABEL_CLS}>Workflow Name</label>
        <input
          type="text"
          className={FIELD_INPUT_CLS}
          value={(aw.name as string) ?? ''}
          onChange={(e) => setAwField('name', e.target.value)}
        />
      </div>
      <div className={FIELD_CLS}>
        <label className={FIELD_LABEL_CLS}>Session ID</label>
        <input
          type="text"
          className={FIELD_INPUT_CLS}
          value={(aw.session_id as string) ?? ''}
          onChange={(e) => setAwField('session_id', e.target.value)}
          placeholder="Optional"
        />
      </div>
      <div className={FIELD_CLS}>
        <label className={FIELD_LABEL_CLS}>Variables</label>
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
    <div className={COMMON_CLS}>
      <div className={FIELD_CLS}>
        <label className={FIELD_LABEL_CLS}>Condition</label>
        <input
          type="text"
          className={FIELD_INPUT_CLS}
          value={stripTemplateWrapper((step.condition as string) ?? '')}
          onChange={(e) => {
            const val = e.target.value.trim()
            onChange({ condition: val ? wrapTemplateExpr(val) : undefined })
          }}
          placeholder="e.g. inputs.mode == 'deploy'"
        />
      </div>

      <div className={FIELD_CLS}>
        <label className={FIELD_LABEL_CLS}>Input</label>
        <input
          type="text"
          className={FIELD_INPUT_CLS}
          value={(step.input as string) ?? ''}
          onChange={(e) => onChange({ input: e.target.value || undefined })}
          placeholder="e.g. $prev_step.output"
        />
      </div>

      {type === 'prompt' && (
        <div className={FIELD_CLS}>
          <label className={FIELD_LABEL_CLS}>Tools</label>
          <input
            type="text"
            className={FIELD_INPUT_CLS}
            value={Array.isArray(step.tools) ? (step.tools as string[]).join(', ') : ''}
            onChange={(e) => {
              const val = e.target.value.trim()
              onChange({ tools: val ? val.split(',').map((s) => s.trim()).filter(Boolean) : undefined })
            }}
            placeholder="Comma-separated tool list"
          />
        </div>
      )}

      <div className={FIELD_CLS}>
        <label className={CHECKBOX_LABEL_CLS}>
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
          <div className={FIELD_CLS}>
            <label className={FIELD_LABEL_CLS}>Approval Message</label>
            <input
              type="text"
              className={FIELD_INPUT_CLS}
              value={(approval.message as string) ?? ''}
              onChange={(e) =>
                onChange({ approval: { ...approval, message: e.target.value } })
              }
              placeholder="Approval prompt message"
            />
          </div>
          <div className={FIELD_CLS}>
            <label className={FIELD_LABEL_CLS}>Timeout (seconds)</label>
            <input
              type="number"
              className={FIELD_INPUT_CLS}
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
    <div className={KV_CLS}>
      {pairs.map((p, i) => (
        <div key={i} className={KV_ROW_CLS}>
          <input
            type="text"
            className={KV_INPUT_CLS}
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
            className={KV_INPUT_CLS}
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
            className={KV_REMOVE_CLS}
            onClick={() => onChange(pairs.filter((_, j) => j !== i))}
          >
            &times;
          </button>
        </div>
      ))}
      <button
        type="button"
        className={KV_ADD_CLS}
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
    <div className={ADD_CLS}>
      <button
        type="button"
        className={ADD_BTN_CLS}
        onClick={() => setOpen(!open)}
      >
        + Add Step
      </button>
      {open && (
        <div className={ADD_DROPDOWN_CLS}>
          {STEP_TYPES.map((t) => (
            <button
              key={t.value}
              type="button"
              className={ADD_OPTION_CLS}
              onClick={() => { onAdd(t.value); setOpen(false) }}
            >
              <span
                className={ADD_DOT_CLS}
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
