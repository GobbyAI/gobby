import { useState, useEffect, useCallback } from 'react'
import '../workflows/LaunchAgentModal.css'
import { useAgentSpawn } from '../../hooks/useAgentSpawn'
import type { AgentDefinition, SpawnResult } from '../../hooks/useAgentSpawn'
import {
  AUTO_REASONING_EFFORT,
  fetchProviderModelCatalog,
  getModelsForProvider,
  getReasoningOptionsForModel,
  type ProviderModelEntry,
} from '../../lib/providerModels'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface LaunchAgentDialogProps {
  isOpen: boolean
  taskId: string
  taskTitle: string
  taskCategory?: string | null
  projectId?: string | null
  onClose: () => void
  onSpawned?: (result: SpawnResult) => void
}

// Batch variant props
interface BatchLaunchAgentDialogProps {
  isOpen: boolean
  tasks: Array<{ id: string; title: string; category?: string | null }>
  projectId?: string | null
  onClose: () => void
  onSpawned?: (succeeded: number, failed: number) => void
}

type Isolation = 'none' | 'worktree' | 'clone'

// ---------------------------------------------------------------------------
// Icons
// ---------------------------------------------------------------------------

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

// ---------------------------------------------------------------------------
// Single Launch Dialog
// ---------------------------------------------------------------------------

export function LaunchAgentDialog({
  isOpen,
  taskId,
  taskTitle,
  taskCategory,
  projectId,
  onClose,
  onSpawned,
}: LaunchAgentDialogProps) {
  const { spawn, spawning, fetchDefinitions, previewPrompt, getDefaults, saveDefaults } = useAgentSpawn()

  // Form state
  const [agentName, setAgentName] = useState('default')
  const [isolation, setIsolation] = useState<Isolation>('none')
  const [model, setModel] = useState<string>('')
  const [reasoningEffort, setReasoningEffort] = useState<string>(AUTO_REASONING_EFFORT)
  const [reasoningRequired, setReasoningRequired] = useState(false)
  const [promptText, setPromptText] = useState('')
  const [promptExpanded, setPromptExpanded] = useState(false)
  const [rememberDefaults, setRememberDefaults] = useState(false)

  // Data
  const [definitions, setDefinitions] = useState<AgentDefinition[]>([])
  const [providerCatalog, setProviderCatalog] = useState<ProviderModelEntry[]>([])
  const [loadingPrompt, setLoadingPrompt] = useState(false)

  // Result
  const [result, setResult] = useState<SpawnResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  // Load agent definitions and defaults on open
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

  // Load prompt preview on open
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

  useEffect(() => {
    const currentValues = new Set(reasoningOptions.map(option => option.value))
    if (!currentValues.has(reasoningEffort)) {
      setReasoningEffort(AUTO_REASONING_EFFORT)
      setReasoningRequired(false)
    }
  }, [reasoningOptions, reasoningEffort])

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
      // Save defaults if requested
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

  // Success state
  if (result) {
    return (
      <>
        <div className="launch-agent-backdrop" onClick={onClose} />
        <div className="launch-agent-modal">
          <div className="launch-agent-header">
            <span className="launch-agent-title">Agent Launched</span>
            <button className="launch-agent-close" onClick={onClose}><CloseIcon /></button>
          </div>
          <div className="launch-agent-success">
            <div className="launch-agent-success-icon">&#10003;</div>
            <p className="launch-success-text">Agent spawned successfully.</p>
            {result.reasoning?.message && (
              <p className="launch-success-text">{result.reasoning.message}</p>
            )}
            <button className="launch-agent-btn launch-agent-btn--primary" onClick={onClose}>
              Done
            </button>
          </div>
        </div>
      </>
    )
  }

  return (
    <>
      <div className="launch-agent-backdrop" onClick={onClose} />
      <div className="launch-agent-modal">
        <div className="launch-agent-header">
          <span className="launch-agent-title">
            <RocketIcon /> Launch Agent
          </span>
          <button className="launch-agent-close" onClick={onClose}><CloseIcon /></button>
        </div>

        <div className="launch-agent-body">
          {/* Task context */}
          <div className="launch-agent-task-context">
            {taskTitle}
          </div>

          {/* Agent definition picker */}
          <div className="launch-agent-field">
            <label className="launch-agent-label">Agent Definition</label>
            <select
              className="launch-agent-select"
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

          {/* Mode selector */}
          <div className="launch-agent-field">
          </div>

          {/* Isolation picker */}
          <div className="launch-agent-field">
            <label className="launch-agent-label">Isolation</label>
            <div className="launch-agent-radio-group">
              {([['none', 'None'], ['worktree', 'Worktree'], ['clone', 'Clone']] as const).map(([val, label]) => (
                <label key={val} className={`launch-agent-radio ${isolation === val ? 'active' : ''}`}>
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

          {/* Model override */}
          <div className="launch-agent-field">
            <label className="launch-agent-label">Model Override</label>
            <select
              className="launch-agent-select"
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

          <div className="launch-agent-field">
            <label className="launch-agent-label">Reasoning</label>
            <select
              className="launch-agent-select"
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

          <label className="launch-agent-checkbox">
            <input
              type="checkbox"
              checked={reasoningRequired}
              disabled={reasoningEffort === AUTO_REASONING_EFFORT}
              onChange={e => setReasoningRequired(e.target.checked)}
            />
            Require reasoning support
          </label>
          {reasoningDisabled && (
            <div className="launch-agent-field-hint">Reasoning is not available for the selected provider/model.</div>
          )}
          {error && reasoningEffort !== AUTO_REASONING_EFFORT && !reasoningRequired && (
            <div className="launch-agent-field-hint">Unsupported reasoning falls back with a warning unless Require reasoning support is enabled.</div>
          )}

          <div className="launch-agent-field">
            <label className="launch-agent-label">Model Provider</label>
            <div className="launch-agent-task-context">{effectiveProvider}</div>
          </div>

          {/* Prompt preview */}
          <div className="launch-agent-field">
            <button
              className="launch-agent-prompt-toggle"
              onClick={() => setPromptExpanded(!promptExpanded)}
              type="button"
            >
              <span className="launch-agent-prompt-toggle-icon">{promptExpanded ? '\u25BE' : '\u25B8'}</span>
              Prompt Preview
              {loadingPrompt && <span className="launch-agent-loading-dot">...</span>}
            </button>
            {promptExpanded && (
              <textarea
                className="launch-agent-textarea"
                value={promptText}
                onChange={e => setPromptText(e.target.value)}
                rows={8}
                placeholder="Auto-generated prompt will appear here..."
              />
            )}
          </div>

          {/* Remember defaults */}
          <label className="launch-agent-checkbox">
            <input
              type="checkbox"
              checked={rememberDefaults}
              onChange={e => setRememberDefaults(e.target.checked)}
            />
            Remember as default for {taskCategory || 'all'} tasks
          </label>

          {error && <div className="launch-agent-error">{error}</div>}
        </div>

        <div className="launch-agent-footer">
          <button
            className="launch-agent-btn launch-agent-btn--default"
            onClick={onClose}
            disabled={spawning}
          >
            Cancel
          </button>
          <button
            className="launch-agent-btn launch-agent-btn--primary"
            onClick={handleLaunch}
            disabled={spawning}
          >
            {spawning ? 'Launching...' : 'Launch Agent'}
          </button>
        </div>
      </div>
    </>
  )
}

// ---------------------------------------------------------------------------
// Batch Launch Dialog
// ---------------------------------------------------------------------------

export function BatchLaunchAgentDialog({
  isOpen,
  tasks,
  projectId,
  onClose,
  onSpawned,
}: BatchLaunchAgentDialogProps) {
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

  // Success state
  if (batchResult) {
    return (
      <>
        <div className="launch-agent-backdrop" onClick={onClose} />
        <div className="launch-agent-modal">
          <div className="launch-agent-header">
            <span className="launch-agent-title">Batch Launch Complete</span>
            <button className="launch-agent-close" onClick={onClose}><CloseIcon /></button>
          </div>
          <div className="launch-agent-success">
            <div className="launch-agent-success-icon">&#10003;</div>
            <p>{batchResult.succeeded} agent{batchResult.succeeded !== 1 ? 's' : ''} launched successfully.</p>
            {batchResult.failed > 0 && (
              <p className="launch-agent-error">{batchResult.failed} failed to launch.</p>
            )}
            <button className="launch-agent-btn launch-agent-btn--primary" onClick={onClose}>
              Done
            </button>
          </div>
        </div>
      </>
    )
  }

  return (
    <>
      <div className="launch-agent-backdrop" onClick={onClose} />
      <div className="launch-agent-modal launch-agent-modal--batch">
        <div className="launch-agent-header">
          <span className="launch-agent-title">
            <RocketIcon /> Launch Agents ({activeCount} task{activeCount !== 1 ? 's' : ''})
          </span>
          <button className="launch-agent-close" onClick={onClose}><CloseIcon /></button>
        </div>

        <div className="launch-agent-body">
          {/* Task list with exclude toggles */}
          <div className="launch-agent-field">
            <label className="launch-agent-label">Tasks</label>
            <div className="launch-agent-task-list">
              {tasks.map(t => (
                <label key={t.id} className={`launch-agent-task-item ${excludedIds.has(t.id) ? 'excluded' : ''}`}>
                  <input
                    type="checkbox"
                    checked={!excludedIds.has(t.id)}
                    onChange={() => toggleExclude(t.id)}
                  />
                  <span className="launch-agent-task-item-title">{t.title}</span>
                </label>
              ))}
            </div>
          </div>

          {/* Shared config */}
          <div className="launch-agent-field">
            <label className="launch-agent-label">Agent Definition</label>
            <select className="launch-agent-select" value={agentName} onChange={e => setAgentName(e.target.value)}>
              {definitions.length === 0 && <option value="default">default</option>}
              {definitions.map(d => (
                <option key={d.definition.name} value={d.definition.name}>
                  {d.definition.name}
                </option>
              ))}
            </select>
          </div>

          <div className="launch-agent-field">
            <label className="launch-agent-label">Isolation</label>
            <div className="launch-agent-radio-group">
              {([['none', 'None'], ['worktree', 'Worktree'], ['clone', 'Clone']] as const).map(([val, label]) => (
                <label key={val} className={`launch-agent-radio ${isolation === val ? 'active' : ''}`}>
                  <input type="radio" name="batch-isolation" value={val} checked={isolation === val} onChange={() => setIsolation(val)} />
                  {label}
                </label>
              ))}
            </div>
          </div>

          <div className="launch-agent-field">
            <label className="launch-agent-label">Model Override</label>
            <select className="launch-agent-select" value={model} onChange={e => setModel(e.target.value)}>
              <option value="">Default</option>
              {modelOptions.map(option => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>

          <div className="launch-agent-field">
            <label className="launch-agent-label">Reasoning</label>
            <select
              className="launch-agent-select"
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

          <label className="launch-agent-checkbox">
            <input
              type="checkbox"
              checked={reasoningRequired}
              disabled={reasoningEffort === AUTO_REASONING_EFFORT}
              onChange={e => setReasoningRequired(e.target.checked)}
            />
            Require reasoning support
          </label>

          {error && <div className="launch-agent-error">{error}</div>}
        </div>

        <div className="launch-agent-footer">
          <button className="launch-agent-btn launch-agent-btn--default" onClick={onClose} disabled={spawning}>
            Cancel
          </button>
          <button
            className="launch-agent-btn launch-agent-btn--primary"
            onClick={handleBatchLaunch}
            disabled={spawning || activeCount === 0}
          >
            {spawning ? 'Launching...' : `Launch ${activeCount} Agent${activeCount !== 1 ? 's' : ''}`}
          </button>
        </div>
      </div>
    </>
  )
}
