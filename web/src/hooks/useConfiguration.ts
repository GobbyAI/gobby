import { useState, useCallback, useEffect, useRef } from 'react'
import {
  configurationClient,
  type ConfigSchema,
  type ConfigValuesSnapshot,
} from '../api/config'
export type { ConfigApplyFailure } from '../api/config'
import type { ConfigApplyFailure } from '../api/config'
import { useWebSocketConnected, useWebSocketEvent } from './useWebSocketEvent'

// =============================================================================
// Types
// =============================================================================

export interface SecretInfo {
  id: string
  name: string
  category: string
  description: string | null
  created_at: string
  updated_at: string
}

export interface PromptInfo {
  path: string
  description: string
  category: string
  source: 'bundled' | 'overridden'
  has_override: boolean
}

export interface PromptDetail {
  path: string
  description: string
  content: string
  source: 'bundled' | 'overridden'
  has_override: boolean
  bundled_content: string | null
  variables: Record<string, { type: string; required: boolean; default: unknown }>
}

export interface ConfigExportBundle {
  revision: number
  content: string
}

export interface ApprovalRulesPayload {
  rules: string[]
  default_rules: string[]
  built_in_exemptions: string[]
}

function recordAt(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function isSecretInfo(value: unknown): value is SecretInfo {
  const item = recordAt(value)
  return typeof item?.id === 'string'
    && typeof item.name === 'string'
    && typeof item.category === 'string'
    && typeof item.created_at === 'string'
    && typeof item.updated_at === 'string'
}

function isPromptInfo(value: unknown): value is PromptInfo {
  const item = recordAt(value)
  return typeof item?.path === 'string'
    && typeof item.description === 'string'
    && typeof item.category === 'string'
    && (item.source === 'bundled' || item.source === 'overridden')
    && typeof item.has_override === 'boolean'
}

function numberRecord(value: unknown): Record<string, number> {
  const source = recordAt(value)
  if (!source) return {}
  return Object.fromEntries(
    Object.entries(source).filter((entry): entry is [string, number] => {
      return typeof entry[1] === 'number'
    }),
  )
}

// =============================================================================
// Hook
// =============================================================================

export function useConfiguration() {
  const initialSnapshot = configurationClient.currentSnapshot
  // Schema + Config
  const [schema, setSchema] = useState<ConfigSchema | null>(null)
  const [configValues, setConfigValues] = useState<Record<string, unknown>>(
    initialSnapshot?.desired ?? {},
  )
  const [activeConfigValues, setActiveConfigValues] = useState<Record<string, unknown>>(
    initialSnapshot?.active ?? {},
  )
  const [secretKeys, setSecretKeys] = useState<string[]>([])
  const [revision, setRevision] = useState(initialSnapshot?.revision ?? 0)
  const [pendingRestartKeys, setPendingRestartKeys] = useState<string[]>(
    initialSnapshot?.pending_restart_keys ?? [],
  )
  const [failedLiveKeys, setFailedLiveKeys] = useState<Record<string, ConfigApplyFailure>>(
    initialSnapshot?.failed_live_keys ?? {},
  )
  const [mutationError, setMutationError] = useState<{
    message: string
    terminal: boolean
  } | null>(null)
  const [isLoading, setIsLoading] = useState(false)

  // Rules enforcement + approvals (re-derived from every snapshot below)
  const [rulesEnforcement, setRulesEnforcementState] = useState(true)
  const [globalApprovalRules, setGlobalApprovalRules] = useState<string[]>([])
  const [defaultApprovalRules, setDefaultApprovalRules] = useState<string[]>([])
  const [builtInApprovalExemptions, setBuiltInApprovalExemptions] = useState<string[]>([])

  const applySnapshot = useCallback((snapshot: ConfigValuesSnapshot) => {
    setConfigValues(snapshot.desired)
    setActiveConfigValues(snapshot.active)
    setRevision(snapshot.revision)
    setPendingRestartKeys(snapshot.pending_restart_keys)
    setFailedLiveKeys(snapshot.failed_live_keys)
    setSecretKeys(
      Object.entries(snapshot.secret_set)
        .filter(([, state]) => state.desired)
        .map(([key]) => key),
    )
    // Derived flags must track every snapshot (WS-triggered refetches
    // included), not just explicit fetchConfig calls, or they go stale.
    const rules = recordAt(snapshot.desired.rules)
    if (typeof rules?.enforcement_enabled === 'boolean') {
      setRulesEnforcementState(rules.enforcement_enabled)
    }
    const approvals = recordAt(snapshot.desired.tool_approvals)
    if (Array.isArray(approvals?.global_rules)) {
      setGlobalApprovalRules(
        approvals.global_rules.filter((value): value is string => typeof value === 'string'),
      )
    }
  }, [])

  useEffect(() => configurationClient.subscribe(applySnapshot), [applySnapshot])

  useWebSocketEvent('config_event', (event) => {
    if ('revision' in event) configurationClient.observeRevision(event.revision)
  })
  const websocketConnected = useWebSocketConnected()
  const previousConnection = useRef(websocketConnected)
  useEffect(() => {
    // Every disconnected→connected transition resyncs: the first connect
    // covers revisions missed before the socket opened (and retries an
    // initial values fetch that failed while the daemon was unreachable);
    // reconnects cover events lost during the outage.
    if (websocketConnected && !previousConnection.current) {
      void configurationClient.fetchValues()
    }
    previousConnection.current = websocketConnected
  }, [websocketConnected])

  // Template (full defaults + DB overrides as YAML)
  const [templateContent, setTemplateContent] = useState('')

  // Secrets
  const [secrets, setSecrets] = useState<SecretInfo[]>([])
  const [secretCategories, setSecretCategories] = useState<string[]>([])

  // Prompts
  const [prompts, setPrompts] = useState<PromptInfo[]>([])
  const [promptCategories, setPromptCategories] = useState<Record<string, number>>({})

  // =========================================================================
  // Schema + Config
  // =========================================================================

  const setRulesEnforcement = useCallback(async (enabled: boolean): Promise<boolean> => {
    try {
      const result = await configurationClient.patch({
        rules: { enforcement_enabled: enabled },
      })
      if (result.kind === 'success') {
        setRulesEnforcementState(enabled)
        return true
      }
    } catch (e) {
      console.error('Failed to set rules enforcement:', e)
    }
    return false
  }, [])

  const fetchSchema = useCallback(async () => {
    try {
      const data = await configurationClient.fetchSchema()
      if (data) setSchema(data)
    } catch (e) {
      console.error('Failed to fetch config schema:', e)
    }
  }, [])

  const fetchConfigValues = useCallback(async () => {
    try {
      const data = await configurationClient.fetchValues()
      if (data) applySnapshot(data)
    } catch (e) {
      console.error('Failed to fetch config values:', e)
    }
  }, [applySnapshot])

  const fetchGlobalApprovalRules = useCallback(async () => {
    try {
      const data = await configurationClient.fetchApprovalRules()
      if (data) {
        setDefaultApprovalRules(data.default_rules || [])
        setBuiltInApprovalExemptions(data.built_in_exemptions || [])
      }
    } catch (e) {
      console.error('Failed to fetch global approval rules:', e)
    }
  }, [])

  const saveGlobalApprovalRules = useCallback(async (rules: string[]): Promise<boolean> => {
    try {
      const result = await configurationClient.patch({
        tool_approvals: { global_rules: rules },
      })
      if (result.kind === 'success') {
        setGlobalApprovalRules(rules)
        return true
      }
    } catch (e) {
      console.error('Failed to save global approval rules:', e)
    }
    return false
  }, [])

  const fetchConfig = useCallback(async () => {
    setIsLoading(true)
    try {
      await Promise.all([
        fetchSchema(),
        fetchConfigValues(),
        fetchGlobalApprovalRules(),
      ])
    } finally {
      setIsLoading(false)
    }
  }, [fetchSchema, fetchConfigValues, fetchGlobalApprovalRules])

  const saveConfig = useCallback(async (values: Record<string, unknown>): Promise<{
    ok: boolean
    errors?: string[]
    conflict?: boolean
    terminal?: boolean
  }> => {
    try {
      setMutationError(null)
      const result = await configurationClient.patch(values)
      if (result.kind === 'success') return { ok: true }
      const terminal = result.kind === 'revision_exhausted'
      setMutationError({ message: result.message, terminal })
      return {
        ok: false,
        errors: [result.message],
        conflict: result.kind === 'conflict',
        terminal,
      }
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e)
      setMutationError({ message, terminal: false })
      return { ok: false, errors: [message] }
    }
  }, [])

  // =========================================================================
  // Template (full defaults + DB overrides as YAML)
  // =========================================================================

  const fetchTemplate = useCallback(async () => {
    try {
      const content = await configurationClient.fetchTemplate()
      if (content !== null) setTemplateContent(content)
    } catch (e) {
      console.error('Failed to fetch template:', e)
    }
  }, [])

  const saveTemplate = useCallback(async (content: string): Promise<{ ok: boolean; errors?: string[] }> => {
    try {
      const result = await configurationClient.saveTemplate(content)
      if (result.kind === 'success') return { ok: true }
      return { ok: false, errors: [result.message] }
    } catch (e) {
      return { ok: false, errors: [String(e)] }
    }
  }, [])

  // =========================================================================
  // Secrets
  // =========================================================================

  const fetchSecrets = useCallback(async () => {
    try {
      const data = await configurationClient.fetchSecrets()
      if (data) {
        setSecrets(Array.isArray(data.secrets) ? data.secrets.filter(isSecretInfo) : [])
        setSecretCategories(
          Array.isArray(data.categories)
            ? data.categories.filter((value): value is string => typeof value === 'string')
            : [],
        )
      }
    } catch (e) {
      console.error('Failed to fetch secrets:', e)
    }
  }, [])

  const saveSecret = useCallback(async (
    name: string,
    value: string,
    category?: string,
    description?: string,
  ): Promise<boolean> => {
    try {
      if (await configurationClient.saveSecret({ name, value, category, description })) {
        await fetchSecrets()
        return true
      }
    } catch (e) {
      console.error('Failed to save secret:', e)
    }
    return false
  }, [fetchSecrets])

  const deleteSecret = useCallback(async (name: string): Promise<boolean> => {
    try {
      if (await configurationClient.deleteSecret(name)) {
        setSecrets(prev => prev.filter(s => s.name !== name))
        return true
      }
    } catch (e) {
      console.error('Failed to delete secret:', e)
    }
    return false
  }, [])

  // =========================================================================
  // Prompts
  // =========================================================================

  const fetchPrompts = useCallback(async () => {
    try {
      const data = await configurationClient.fetchPrompts()
      if (data) {
        setPrompts(Array.isArray(data.prompts) ? data.prompts.filter(isPromptInfo) : [])
        setPromptCategories(numberRecord(data.categories))
      }
    } catch (e) {
      console.error('Failed to fetch prompts:', e)
    }
  }, [])

  const getPromptDetail = useCallback(async (path: string): Promise<PromptDetail | null> => {
    try {
      const data = await configurationClient.fetchPrompt(path)
      if (data) return data as unknown as PromptDetail
    } catch (e) {
      console.error('Failed to get prompt detail:', e)
    }
    return null
  }, [])

  const savePromptOverride = useCallback(async (path: string, content: string): Promise<boolean> => {
    try {
      if (await configurationClient.savePrompt(path, content)) {
        await fetchPrompts()
        return true
      }
    } catch (e) {
      console.error('Failed to save prompt override:', e)
    }
    return false
  }, [fetchPrompts])

  const deletePromptOverride = useCallback(async (path: string): Promise<boolean> => {
    try {
      if (await configurationClient.deletePrompt(path)) {
        await fetchPrompts()
        return true
      }
    } catch (e) {
      console.error('Failed to delete prompt override:', e)
    }
    return false
  }, [fetchPrompts])

  // =========================================================================
  // Export / Import
  // =========================================================================

  const exportConfig = useCallback(async (): Promise<ConfigExportBundle | null> => {
    try {
      return await configurationClient.exportYaml()
    } catch (e) {
      console.error('Failed to export config:', e)
    }
    return null
  }, [])

  const importConfig = useCallback(async (content: string): Promise<{ success: boolean; summary: string }> => {
    try {
      const result = await configurationClient.importYaml(content)
      return {
        success: result.kind === 'success',
        summary: result.kind === 'success'
          ? `Committed revision ${result.revision}`
          : result.message,
      }
    } catch (e) {
      return { success: false, summary: String(e) }
    }
  }, [])

  const runManagedAction = useCallback(async (
    action: string,
    payload: Record<string, unknown>,
  ): Promise<boolean> => configurationClient.runManagedAction(action, payload), [])

  return {
    // Schema + Config
    schema,
    configValues,
    activeConfigValues,
    secretKeys,
    revision,
    pendingRestartKeys,
    failedLiveKeys,
    mutationError,
    isLoading,
    fetchConfig,
    saveConfig,

    // Rules enforcement
    rulesEnforcement,
    setRulesEnforcement,
    globalApprovalRules,
    defaultApprovalRules,
    builtInApprovalExemptions,
    fetchGlobalApprovalRules,
    saveGlobalApprovalRules,

    // Template (full defaults + DB overrides as YAML)
    templateContent,
    fetchTemplate,
    saveTemplate,

    // Secrets
    secrets,
    secretCategories,
    fetchSecrets,
    saveSecret,
    deleteSecret,

    // Prompts
    prompts,
    promptCategories,
    fetchPrompts,
    getPromptDetail,
    savePromptOverride,
    deletePromptOverride,

    // Export/Import
    exportConfig,
    importConfig,
    runManagedAction,
  }
}
