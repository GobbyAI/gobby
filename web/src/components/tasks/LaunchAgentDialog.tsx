import { useState, useEffect, useCallback, useRef } from 'react'
import { useAgentSpawn } from '../../hooks/useAgentSpawn'
import type { AgentDefinition, SpawnResult } from '../../hooks/useAgentSpawn'
import { useDialogFocus } from '../../hooks/useDialogFocus'
import {
  AUTO_REASONING_EFFORT,
  fetchProviderModelCatalog,
  getModelsForProvider,
  getReasoningOptionsForModel,
  type ProviderModelEntry,
} from '../../lib/providerModels'

interface LaunchAgentDialogProps {
  isOpen: boolean
  taskId: string
  taskTitle: string
  taskCategory?: string | null
  projectId?: string | null
  onClose: () => void
  onSpawned?: (result: SpawnResult) => void
}

interface BatchLaunchAgentDialogProps {
  isOpen: boolean
  tasks: Array<{ id: string; title: string; category?: string | null }>
  projectId?: string | null
  onClose: () => void
  onSpawned?: (succeeded: number, failed: number) => void
}

type Isolation = 'none' | 'worktree' | 'clone'

const BACKDROP_CLS = 'fixed inset-0 z-[300] bg-[var(--surface-scrim)]'
const MODAL_CLS =
  'fixed left-1/2 top-1/2 z-[310] flex max-h-[85vh] w-[480px] max-w-[calc(100vw-2rem)] -translate-x-1/2 -translate-y-1/2 flex-col overflow-y-auto rounded-xl border border-[var(--border)] bg-[var(--bg-secondary)] shadow-[var(--shadow-xl)]'
const MODAL_BATCH_CLS = 'w-[540px]'
const HEADER_CLS =
  'flex shrink-0 items-center justify-between border-b border-[var(--border)] px-5 py-3.5'
const TITLE_CLS =
  'flex items-center gap-2 text-[length:calc(var(--font-size-base)*1.05)] font-semibold'
const CLOSE_CLS =
  'flex cursor-pointer items-center rounded border-none bg-transparent p-1 text-[var(--text-muted)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)]'
const BODY_CLS = 'flex flex-1 flex-col gap-3 overflow-y-auto px-5 py-4'
const TASK_CONTEXT_CLS =
  'rounded-md border border-[color-mix(in_srgb,var(--accent)_30%,var(--border))] bg-[color-mix(in_srgb,var(--accent)_8%,var(--bg-tertiary))] px-3 py-2 text-[length:calc(var(--font-size-base)*0.85)] text-[var(--text-secondary)]'
const FIELD_CLS = 'flex flex-col gap-[0.3rem]'
const LABEL_CLS = 'font-medium text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-muted)]'
const SELECT_CLS =
  'rounded-md border border-[var(--border)] bg-[var(--bg-tertiary)] px-[0.6rem] py-[0.4rem] font-[inherit] text-[length:calc(var(--font-size-base)*0.85)] text-[var(--text-primary)] outline-none focus:border-[var(--accent)]'
const TEXTAREA_CLS =
  'min-h-20 resize-y rounded-md border border-[var(--border)] bg-[var(--bg-tertiary)] px-[0.6rem] py-2 font-[inherit] text-[length:calc(var(--font-size-base)*0.8)] text-[var(--text-primary)] outline-none focus:border-[var(--accent)]'
const RADIO_GROUP_CLS = 'flex gap-1'
const RADIO_CLS =
  'pointer-coarse:min-h-11 flex min-h-11 flex-1 cursor-pointer items-center justify-center gap-1 rounded-md border border-[var(--border)] bg-[var(--bg-tertiary)] px-2 py-1.5 text-[length:calc(var(--font-size-base)*0.8)] text-[var(--text-secondary)] transition-all duration-150 hover:border-[var(--accent)] hover:text-[var(--text-primary)] [&_input]:hidden'
const RADIO_ACTIVE_CLS =
  'border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_10%,transparent)] font-medium text-[var(--accent)]'
const PROMPT_TOGGLE_CLS =
  'flex cursor-pointer items-center gap-[0.35rem] border-none bg-transparent py-1 font-[inherit] text-[length:calc(var(--font-size-base)*0.8)] text-[var(--text-secondary)] hover:text-[var(--text-primary)]'
const PROMPT_TOGGLE_ICON_CLS = 'text-[0.7em]'
const LOADING_DOT_CLS = 'text-[var(--text-muted)]'
const CHECKBOX_CLS =
  'pointer-coarse:min-h-11 flex min-h-11 cursor-pointer items-center gap-2 text-[length:calc(var(--font-size-base)*0.8)] text-[var(--text-secondary)] [&_input]:h-4 [&_input]:w-4 [&_input]:[accent-color:var(--accent)]'
const FIELD_HINT_CLS = 'text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-muted)]'
const ERROR_CLS = 'py-1 text-[length:calc(var(--font-size-base)*0.8)] text-[var(--color-error)]'
const FOOTER_CLS =
  'flex shrink-0 justify-end gap-2 border-t border-[var(--border)] px-5 py-3'
const BTN_CLS =
  'pointer-coarse:min-h-11 cursor-pointer rounded-md border border-[var(--border)] px-4 py-2 font-[inherit] text-[length:calc(var(--font-size-base)*0.85)] font-medium transition-all duration-150 disabled:cursor-not-allowed disabled:opacity-50'
const BTN_PRIMARY_CLS =
  'border-[var(--accent)] bg-[var(--accent)] text-[var(--accent-foreground)] hover:bg-[var(--accent-hover)] disabled:hover:bg-[var(--accent)]'
const BTN_DEFAULT_CLS =
  'bg-[var(--bg-tertiary)] text-[var(--text-primary)] hover:bg-[var(--bg-secondary)] disabled:hover:bg-[var(--bg-tertiary)]'
const SUCCESS_CLS = 'flex flex-col items-center gap-3 px-5 py-8 text-center'
const SUCCESS_ICON_CLS = 'text-[length:var(--text-4xl)] text-[var(--color-success-foreground)]'
const SUCCESS_TEXT_CLS = 'text-[length:calc(var(--font-size-base)*0.9)] text-[var(--text-secondary)]'
const TASK_LIST_CLS =
  'flex max-h-[200px] flex-col gap-px overflow-y-auto rounded-md border border-[var(--border)] p-1'
const TASK_ITEM_CLS =
  'pointer-coarse:min-h-11 flex min-h-11 cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-[length:calc(var(--font-size-base)*0.8)] text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] [&_input]:h-4 [&_input]:w-4 [&_input]:[accent-color:var(--accent)]'
const TASK_ITEM_EXCLUDED_CLS = 'opacity-40 [&>span]:line-through'
const TASK_ITEM_TITLE_CLS = 'overflow-hidden text-ellipsis whitespace-nowrap'

function CloseIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  )
}

function RocketIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z" />
      <path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z" />
      <path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0" />
      <path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5" />
    </svg>
  )
}

export function LaunchAgentDialog({
  isOpen,
  taskId,
  taskTitle,
  taskCategory,
  projectId,
  onClose,
  onSpawned,
}: LaunchAgentDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  useDialogFocus({ ref: dialogRef, isOpen, onClose })
  const { spawn, spawning, fetchDefinitions, previewPrompt, getDefaults, saveDefaults } = useAgentSpawn()

  const [agentName, setAgentName] = useState('default')
  const [isolation, setIsolation] = useState<Isolation>('none')
  const [model, setModel] = useState<string>('')
  const [reasoningEffort, setReasoningEffort] = useState<string>(AUTO_REASONING_EFFORT)
  const [reasoningRequired, setReasoningRequired] = useState(false)
  const [promptText, setPromptText] = useState('')
  const [promptExpanded, setPromptExpanded] = useState(false)
  const [rememberDefaults, setRememberDefaults] = useState(false)

  const [definitions, setDefinitions] = useState<AgentDefinition[]>([])
  const [providerCatalog, setProviderCatalog] = useState<ProviderModelEntry[]>([])
  const [loadingPrompt, setLoadingPrompt] = useState(false)

  const [result, setResult] = useState<SpawnResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!isOpen) return
    setResult(null)
    setError(null)
    setAgentName('default')
    setIsolation('none')
    setModel('')
    setReasoningEffort(AUTO_REASONING_EFFORT)
    setReasoningRequired(false)

    fetchDefinitions(projectId || undefined).then(setDefinitions)
    fetchProviderModelCatalog().then(setProviderCatalog)

    if (projectId) {
      getDefaults(projectId).then(allDefaults => {
        const cat = taskCategory || '_default'
        const catDefaults = allDefaults[cat]
        if (catDefaults) {
          setAgentName(catDefaults.agent_name || 'default')
          setIsolation(catDefaults.isolation || 'none')
          setModel(catDefaults.model || '')
          setReasoningEffort(catDefaults.reasoning_effort || AUTO_REASONING_EFFORT)
          setReasoningRequired(Boolean(catDefaults.reasoning_required))
        }
      })
    }
  }, [isOpen, projectId, taskCategory, fetchDefinitions, getDefaults])

  const selectedDefinition = definitions.find(d => d.definition.name === agentName)
  const effectiveProvider = selectedDefinition?.definition.provider && selectedDefinition.definition.provider !== 'inherit'
    ? selectedDefinition.definition.provider
    : 'claude'
  const effectiveModel = model || selectedDefinition?.definition.model || ''
  const modelOptions = getModelsForProvider(providerCatalog, effectiveProvider)
  const reasoningOptions = getReasoningOptionsForModel(providerCatalog, effectiveProvider, effectiveModel)
  const reasoningDisabled = reasoningOptions.length === 1 && Boolean(reasoningOptions[0]?.disabled)

  useEffect(() => {
    const currentValues = new Set(reasoningOptions.map(option => option.value))
    if (!currentValues.has(reasoningEffort)) {
      setReasoningEffort(AUTO_REASONING_EFFORT)
      setReasoningRequired(false)
    }
  }, [reasoningOptions, reasoningEffort])

  useEffect(() => {
    if (!isOpen || !taskId) return
    setLoadingPrompt(true)
    previewPrompt(taskId, agentName).then(preview => {
      if (preview) {
        setPromptText(preview.prompt)
      }
      setLoadingPrompt(false)
    })
  }, [isOpen, taskId]) // eslint-disable-line react-hooks/exhaustive-deps

  const handleAgentChange = useCallback((nextAgentName: string) => {
    const nextDefinition = definitions.find(d => d.definition.name === nextAgentName)
    setAgentName(nextAgentName)
    setModel(nextDefinition?.definition.model || '')
    setReasoningEffort(nextDefinition?.definition.reasoning_effort || AUTO_REASONING_EFFORT)
    setReasoningRequired(Boolean(nextDefinition?.definition.reasoning_required))
  }, [definitions])

  const handleLaunch = useCallback(async () => {
    setError(null)
    const spawnResult = await spawn({
      task_id: taskId,
      agent_name: agentName,
      prompt: promptText || undefined,
      isolation: isolation !== 'none' ? isolation : undefined,
      model: model || undefined,
      reasoning_effort: reasoningEffort,
      reasoning_required: reasoningEffort !== AUTO_REASONING_EFFORT ? reasoningRequired : false,
    })

    if (spawnResult.success) {
      if (rememberDefaults && projectId) {
        const cat = taskCategory || '_default'
        await saveDefaults(projectId, cat, {
          agent_name: agentName,
          isolation,
          model: model || undefined,
          reasoning_effort: reasoningEffort,
          reasoning_required: reasoningEffort !== AUTO_REASONING_EFFORT ? reasoningRequired : false,
        })
      }
      setResult(spawnResult)
      onSpawned?.(spawnResult)
    } else {
      setError(spawnResult.error || 'Launch failed')
    }
  }, [taskId, agentName, promptText, isolation, model, reasoningEffort, reasoningRequired, rememberDefaults, projectId, taskCategory, spawn, saveDefaults, onSpawned])

  if (!isOpen) return null

  if (result) {
    return (
      <div className={BACKDROP_CLS} onClick={onClose}>
        <div
          ref={dialogRef}
          className={MODAL_CLS}
          role="dialog"
          aria-modal="true"
          aria-labelledby="launch-agent-result-title"
          tabIndex={-1}
          onClick={e => e.stopPropagation()}
        >
          <div className={HEADER_CLS}>
            <h2 id="launch-agent-result-title" className={TITLE_CLS}>Agent Launched</h2>
            <button className={CLOSE_CLS} onClick={onClose} aria-label="Close"><CloseIcon /></button>
          </div>
          <div className={SUCCESS_CLS}>
            <div className={SUCCESS_ICON_CLS}>✓</div>
            <p className={SUCCESS_TEXT_CLS}>Agent spawned successfully.</p>
            {result.reasoning?.message && (
              <p className={SUCCESS_TEXT_CLS}>{result.reasoning.message}</p>
            )}
            <button className={`${BTN_CLS} ${BTN_PRIMARY_CLS}`} onClick={onClose}>
              Done
            </button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className={BACKDROP_CLS} onClick={onClose}>
      <div
        ref={dialogRef}
        className={MODAL_CLS}
        role="dialog"
        aria-modal="true"
        aria-labelledby="launch-agent-title"
        tabIndex={-1}
        onClick={e => e.stopPropagation()}
      >
        <div className={HEADER_CLS}>
          <h2 id="launch-agent-title" className={TITLE_CLS}>
            <RocketIcon /> Launch Agent
          </h2>
          <button className={CLOSE_CLS} onClick={onClose} aria-label="Close"><CloseIcon /></button>
        </div>

        <div className={BODY_CLS}>
          <div className={TASK_CONTEXT_CLS}>
            {taskTitle}
          </div>

          <div className={FIELD_CLS}>
            <label className={LABEL_CLS}>Agent Definition</label>
            <select
              className={SELECT_CLS}
              value={agentName}
              onChange={e => handleAgentChange(e.target.value)}
            >
              {definitions.length === 0 && <option value="default">default</option>}
              {definitions.map(d => (
                <option key={d.definition.name} value={d.definition.name}>
                  {d.definition.name}{d.definition.description ? ` — ${d.definition.description}` : ''}
                </option>
              ))}
            </select>
          </div>

          <div className={FIELD_CLS}>
            <label className={LABEL_CLS}>Isolation</label>
            <div className={RADIO_GROUP_CLS}>
              {([['none', 'None'], ['worktree', 'Worktree'], ['clone', 'Clone']] as const).map(([val, label]) => (
                <label
                  key={val}
                  className={isolation === val ? `${RADIO_CLS} ${RADIO_ACTIVE_CLS}` : RADIO_CLS}
                >
                  <input
                    type="radio"
                    name="isolation"
                    value={val}
                    checked={isolation === val}
                    onChange={() => setIsolation(val)}
                  />
                  {label}
                </label>
              ))}
            </div>
          </div>

          <div className={FIELD_CLS}>
            <label className={LABEL_CLS}>Model Override</label>
            <select
              className={SELECT_CLS}
              value={model}
              onChange={e => setModel(e.target.value)}
            >
              <option value="">Default (from agent definition)</option>
              {modelOptions.map(option => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>

          <div className={FIELD_CLS}>
            <label className={LABEL_CLS}>Reasoning</label>
            <select
              className={SELECT_CLS}
              value={reasoningEffort}
              onChange={e => {
                const nextReasoning = e.target.value
                setReasoningEffort(nextReasoning)
                if (nextReasoning === AUTO_REASONING_EFFORT) {
                  setReasoningRequired(false)
                }
              }}
              disabled={reasoningDisabled}
            >
              {reasoningOptions.map(option => (
                <option key={option.value} value={option.value} disabled={option.disabled}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>

          <label className={CHECKBOX_CLS}>
            <input
              type="checkbox"
              checked={reasoningRequired}
              disabled={reasoningEffort === AUTO_REASONING_EFFORT}
              onChange={e => setReasoningRequired(e.target.checked)}
            />
            Require reasoning support
          </label>
          {reasoningDisabled && (
            <div className={FIELD_HINT_CLS}>Reasoning is not available for the selected provider/model.</div>
          )}
          {error && reasoningEffort !== AUTO_REASONING_EFFORT && !reasoningRequired && (
            <div className={FIELD_HINT_CLS}>Unsupported reasoning falls back with a warning unless Require reasoning support is enabled.</div>
          )}

          <div className={FIELD_CLS}>
            <label className={LABEL_CLS}>Model Provider</label>
            <div className={TASK_CONTEXT_CLS}>{effectiveProvider}</div>
          </div>

          <div className={FIELD_CLS}>
            <button
              className={PROMPT_TOGGLE_CLS}
              onClick={() => setPromptExpanded(!promptExpanded)}
              type="button"
            >
              <span className={PROMPT_TOGGLE_ICON_CLS}>{promptExpanded ? '▾' : '▸'}</span>
              Prompt Preview
              {loadingPrompt && <span className={LOADING_DOT_CLS}>...</span>}
            </button>
            {promptExpanded && (
              <textarea
                className={TEXTAREA_CLS}
                value={promptText}
                onChange={e => setPromptText(e.target.value)}
                rows={8}
                placeholder="Auto-generated prompt will appear here..."
              />
            )}
          </div>

          <label className={CHECKBOX_CLS}>
            <input
              type="checkbox"
              checked={rememberDefaults}
              onChange={e => setRememberDefaults(e.target.checked)}
            />
            Remember as default for {taskCategory || 'all'} tasks
          </label>

          {error && <div className={ERROR_CLS}>{error}</div>}
        </div>

        <div className={FOOTER_CLS}>
          <button
            className={`${BTN_CLS} ${BTN_DEFAULT_CLS}`}
            onClick={onClose}
            disabled={spawning}
          >
            Cancel
          </button>
          <button
            className={`${BTN_CLS} ${BTN_PRIMARY_CLS}`}
            onClick={handleLaunch}
            disabled={spawning}
          >
            {spawning ? 'Launching...' : 'Launch Agent'}
          </button>
        </div>
      </div>
    </div>
  )
}

export function BatchLaunchAgentDialog({
  isOpen,
  tasks,
  projectId,
  onClose,
  onSpawned,
}: BatchLaunchAgentDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  useDialogFocus({ ref: dialogRef, isOpen, onClose })
  const { spawnBatch, spawning, fetchDefinitions } = useAgentSpawn()

  const [agentName, setAgentName] = useState('default')
  const [isolation, setIsolation] = useState<Isolation>('none')
  const [model, setModel] = useState<string>('')
  const [reasoningEffort, setReasoningEffort] = useState<string>(AUTO_REASONING_EFFORT)
  const [reasoningRequired, setReasoningRequired] = useState(false)
  const [excludedIds, setExcludedIds] = useState<Set<string>>(new Set())
  const [definitions, setDefinitions] = useState<AgentDefinition[]>([])
  const [providerCatalog, setProviderCatalog] = useState<ProviderModelEntry[]>([])
  const [batchResult, setBatchResult] = useState<{ succeeded: number; failed: number } | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!isOpen) return
    setBatchResult(null)
    setError(null)
    setExcludedIds(new Set())
    setAgentName('default')
    setIsolation('none')
    setModel('')
    setReasoningEffort(AUTO_REASONING_EFFORT)
    setReasoningRequired(false)
    fetchDefinitions(projectId || undefined).then(setDefinitions)
    fetchProviderModelCatalog().then(setProviderCatalog)
  }, [isOpen, projectId, fetchDefinitions])

  const selectedDefinition = definitions.find(d => d.definition.name === agentName)
  const effectiveProvider = selectedDefinition?.definition.provider && selectedDefinition.definition.provider !== 'inherit'
    ? selectedDefinition.definition.provider
    : 'claude'
  const effectiveModel = model || selectedDefinition?.definition.model || ''
  const modelOptions = getModelsForProvider(providerCatalog, effectiveProvider)
  const reasoningOptions = getReasoningOptionsForModel(providerCatalog, effectiveProvider, effectiveModel)
  const reasoningDisabled = reasoningOptions.length === 1 && Boolean(reasoningOptions[0]?.disabled)

  const toggleExclude = useCallback((id: string) => {
    setExcludedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }, [])

  const handleBatchLaunch = useCallback(async () => {
    setError(null)
    const activeTasks = tasks.filter(t => !excludedIds.has(t.id))
    if (activeTasks.length === 0) {
      setError('No tasks selected')
      return
    }

    const spawns = activeTasks.map(t => ({
      task_id: t.id,
      agent_name: agentName,
      isolation: isolation !== 'none' ? isolation : undefined as any,
      model: model || undefined,
      reasoning_effort: reasoningEffort,
      reasoning_required: reasoningEffort !== AUTO_REASONING_EFFORT ? reasoningRequired : false,
    }))

    const result = await spawnBatch(spawns)
    setBatchResult({ succeeded: result.succeeded, failed: result.failed })
    onSpawned?.(result.succeeded, result.failed)
  }, [tasks, excludedIds, agentName, isolation, model, reasoningEffort, reasoningRequired, spawnBatch, onSpawned])

  if (!isOpen) return null

  const activeCount = tasks.length - excludedIds.size

  if (batchResult) {
    return (
      <div className={BACKDROP_CLS} onClick={onClose}>
        <div
          ref={dialogRef}
          className={MODAL_CLS}
          role="dialog"
          aria-modal="true"
          aria-labelledby="batch-launch-result-title"
          tabIndex={-1}
          onClick={e => e.stopPropagation()}
        >
          <div className={HEADER_CLS}>
            <h2 id="batch-launch-result-title" className={TITLE_CLS}>Batch Launch Complete</h2>
            <button className={CLOSE_CLS} onClick={onClose} aria-label="Close"><CloseIcon /></button>
          </div>
          <div className={SUCCESS_CLS}>
            <div className={SUCCESS_ICON_CLS}>✓</div>
            <p className={SUCCESS_TEXT_CLS}>{batchResult.succeeded} agent{batchResult.succeeded !== 1 ? 's' : ''} launched successfully.</p>
            {batchResult.failed > 0 && (
              <p className={ERROR_CLS}>{batchResult.failed} failed to launch.</p>
            )}
            <button className={`${BTN_CLS} ${BTN_PRIMARY_CLS}`} onClick={onClose}>
              Done
            </button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className={BACKDROP_CLS} onClick={onClose}>
      <div
        ref={dialogRef}
        className={`${MODAL_CLS} ${MODAL_BATCH_CLS}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby="batch-launch-title"
        tabIndex={-1}
        onClick={e => e.stopPropagation()}
      >
        <div className={HEADER_CLS}>
          <h2 id="batch-launch-title" className={TITLE_CLS}>
            <RocketIcon /> Launch Agents ({activeCount} task{activeCount !== 1 ? 's' : ''})
          </h2>
          <button className={CLOSE_CLS} onClick={onClose} aria-label="Close"><CloseIcon /></button>
        </div>

        <div className={BODY_CLS}>
          <div className={FIELD_CLS}>
            <label className={LABEL_CLS}>Tasks</label>
            <div className={TASK_LIST_CLS}>
              {tasks.map(t => (
                <label
                  key={t.id}
                  className={excludedIds.has(t.id) ? `${TASK_ITEM_CLS} ${TASK_ITEM_EXCLUDED_CLS}` : TASK_ITEM_CLS}
                >
                  <input
                    type="checkbox"
                    checked={!excludedIds.has(t.id)}
                    onChange={() => toggleExclude(t.id)}
                  />
                  <span className={TASK_ITEM_TITLE_CLS}>{t.title}</span>
                </label>
              ))}
            </div>
          </div>

          <div className={FIELD_CLS}>
            <label className={LABEL_CLS}>Agent Definition</label>
            <select className={SELECT_CLS} value={agentName} onChange={e => setAgentName(e.target.value)}>
              {definitions.length === 0 && <option value="default">default</option>}
              {definitions.map(d => (
                <option key={d.definition.name} value={d.definition.name}>
                  {d.definition.name}
                </option>
              ))}
            </select>
          </div>

          <div className={FIELD_CLS}>
            <label className={LABEL_CLS}>Isolation</label>
            <div className={RADIO_GROUP_CLS}>
              {([['none', 'None'], ['worktree', 'Worktree'], ['clone', 'Clone']] as const).map(([val, label]) => (
                <label
                  key={val}
                  className={isolation === val ? `${RADIO_CLS} ${RADIO_ACTIVE_CLS}` : RADIO_CLS}
                >
                  <input type="radio" name="batch-isolation" value={val} checked={isolation === val} onChange={() => setIsolation(val)} />
                  {label}
                </label>
              ))}
            </div>
          </div>

          <div className={FIELD_CLS}>
            <label className={LABEL_CLS}>Model Override</label>
            <select className={SELECT_CLS} value={model} onChange={e => setModel(e.target.value)}>
              <option value="">Default</option>
              {modelOptions.map(option => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>

          <div className={FIELD_CLS}>
            <label className={LABEL_CLS}>Reasoning</label>
            <select
              className={SELECT_CLS}
              value={reasoningEffort}
              onChange={e => {
                const nextReasoning = e.target.value
                setReasoningEffort(nextReasoning)
                if (nextReasoning === AUTO_REASONING_EFFORT) {
                  setReasoningRequired(false)
                }
              }}
              disabled={reasoningDisabled}
            >
              {reasoningOptions.map(option => (
                <option key={option.value} value={option.value} disabled={option.disabled}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>

          <label className={CHECKBOX_CLS}>
            <input
              type="checkbox"
              checked={reasoningRequired}
              disabled={reasoningEffort === AUTO_REASONING_EFFORT}
              onChange={e => setReasoningRequired(e.target.checked)}
            />
            Require reasoning support
          </label>

          {error && <div className={ERROR_CLS}>{error}</div>}
        </div>

        <div className={FOOTER_CLS}>
          <button className={`${BTN_CLS} ${BTN_DEFAULT_CLS}`} onClick={onClose} disabled={spawning}>
            Cancel
          </button>
          <button
            className={`${BTN_CLS} ${BTN_PRIMARY_CLS}`}
            onClick={handleBatchLaunch}
            disabled={spawning || activeCount === 0}
          >
            {spawning ? 'Launching...' : `Launch ${activeCount} Agent${activeCount !== 1 ? 's' : ''}`}
          </button>
        </div>
      </div>
    </div>
  )
}
