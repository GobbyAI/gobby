import { useState, useCallback } from 'react'

import { AUTO_REASONING_EFFORT } from '../lib/providerModels'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface SpawnParams {
  task_id: string
  agent_name?: string
  prompt?: string
  isolation?: 'none' | 'worktree' | 'clone'
  provider?: string
  model?: string
  reasoning_effort?: string | null
  reasoning_required?: boolean
  workflow?: string
  branch_name?: string
  base_branch?: string
  timeout?: number
  max_turns?: number
}

export interface SpawnResult {
  success: boolean
  run_id?: string
  child_session_id?: string
  conversation_id?: string
  isolation?: string
  branch_name?: string
  pid?: number
  message?: string
  reasoning?: {
    requested_effort?: string | null
    effective_effort?: string | null
    required?: boolean
    status?: string
    message?: string | null
  }
  error?: string
}

export interface BatchResult {
  results: SpawnResult[]
  succeeded: number
  failed: number
}

export interface CategoryDefaults {
  agent_name: string
  isolation: 'none' | 'worktree' | 'clone'
  model?: string
  reasoning_effort?: string | null
  reasoning_required?: boolean
}

export interface AgentDefinition {
    definition: {
      name: string
      description?: string
      role?: string
      provider?: string
      model?: string
      reasoning_effort?: string | null
      reasoning_required?: boolean | null
      isolation?: string
    }
  source: string
  db_id: string
}

export interface PromptPreview {
  prompt: string
  preamble: string | null
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

export function useAgentSpawn() {
  const [spawning, setSpawning] = useState(false)
  const [lastResult, setLastResult] = useState<SpawnResult | null>(null)

  const normalizeReasoningEffort = useCallback((value?: string | null): string | null => {
    if (!value) {
      return null
    }
    const normalized = value.trim().toLowerCase()
    if (!normalized || normalized === AUTO_REASONING_EFFORT) {
      return null
    }
    return normalized
  }, [])

  const spawn = useCallback(async (params: SpawnParams): Promise<SpawnResult> => {
    setSpawning(true)
    setLastResult(null)
    try {
      const reasoningEffort = normalizeReasoningEffort(params.reasoning_effort ?? null)
      const res = await fetch('/api/agents/spawn', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...params,
          reasoning_effort: reasoningEffort,
          reasoning_required: reasoningEffort ? Boolean(params.reasoning_required) : false,
        }),
      })
      const data = await res.json()
      if (!res.ok) {
        const result: SpawnResult = {
          success: false,
                    error: data.detail || 'Spawn failed',
        }
        setLastResult(result)
        return result
      }
      const result: SpawnResult = { success: true, ...data }
      setLastResult(result)
      return result
    } catch (e) {
      const result: SpawnResult = {
        success: false,
                error: e instanceof Error ? e.message : 'Network error',
      }
      setLastResult(result)
      return result
    } finally {
      setSpawning(false)
    }
  }, [normalizeReasoningEffort])

  const spawnBatch = useCallback(async (spawns: SpawnParams[]): Promise<BatchResult> => {
    setSpawning(true)
    try {
      const normalizedSpawns = spawns.map((spawn) => {
        const reasoningEffort = normalizeReasoningEffort(spawn.reasoning_effort ?? null)
        return {
          ...spawn,
          reasoning_effort: reasoningEffort,
          reasoning_required: reasoningEffort ? Boolean(spawn.reasoning_required) : false,
        }
      })
      const res = await fetch('/api/agents/spawn/batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ spawns: normalizedSpawns }),
      })
      const data = await res.json()
      if (!res.ok) {
        return { results: [], succeeded: 0, failed: spawns.length }
      }
      return data
    } catch {
      return { results: [], succeeded: 0, failed: spawns.length }
    } finally {
      setSpawning(false)
    }
  }, [normalizeReasoningEffort])

  const getDefaults = useCallback(async (projectId: string): Promise<Record<string, CategoryDefaults>> => {
    try {
      const res = await fetch(`/api/agents/launch-defaults?project_id=${encodeURIComponent(projectId)}`)
      if (res.ok) {
        const data = await res.json()
        return data.defaults || {}
      }
    } catch {
      // ignore
    }
    return {}
  }, [])

  const saveDefaults = useCallback(async (
    projectId: string,
    category: string,
    defaults: CategoryDefaults,
  ): Promise<void> => {
    try {
      const normalizedReasoningEffort = normalizeReasoningEffort(defaults.reasoning_effort ?? null)
      await fetch('/api/agents/launch-defaults', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project_id: projectId,
          category,
          ...defaults,
          reasoning_effort: normalizedReasoningEffort,
          reasoning_required: normalizedReasoningEffort
            ? Boolean(defaults.reasoning_required)
            : false,
        }),
      })
    } catch {
      // ignore
    }
  }, [normalizeReasoningEffort])

  const fetchDefinitions = useCallback(async (projectId?: string): Promise<AgentDefinition[]> => {
    try {
      const params = projectId ? `?project_id=${encodeURIComponent(projectId)}` : ''
      const res = await fetch(`/api/agents/definitions${params}`)
      if (res.ok) {
        const data = await res.json()
        return data.definitions || []
      }
    } catch {
      // ignore
    }
    return []
  }, [])

  const previewPrompt = useCallback(async (taskId: string, agentName: string = 'default'): Promise<PromptPreview | null> => {
    try {
      const params = new URLSearchParams({ task_id: taskId, agent_name: agentName })
      const res = await fetch(`/api/agents/spawn/prompt-preview?${params}`)
      if (res.ok) {
        const data = await res.json()
        return { prompt: data.prompt, preamble: data.preamble }
      }
    } catch {
      // ignore
    }
    return null
  }, [])

  return {
    spawn,
    spawnBatch,
    spawning,
    lastResult,
    getDefaults,
    saveDefaults,
    fetchDefinitions,
    previewPrompt,
  }
}
