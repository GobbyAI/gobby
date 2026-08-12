export const MAX_CONFIG_REVISION = Number.MAX_SAFE_INTEGER

export type ConfigActivation = 'live' | 'restart_required' | 'managed'

export interface ConfigFieldSchema {
  activation?: ConfigActivation
  [key: string]: unknown
}

export interface ConfigSchema {
  properties: Record<string, ConfigFieldSchema>
  [key: string]: unknown
}

export interface ConfigApplyFailure {
  revision: number
  subscriber: string
}

export interface ConfigValuesSnapshot {
  revision: number
  desired: Record<string, unknown>
  active: Record<string, unknown>
  secret_set: Record<string, { desired: boolean; active: boolean }>
  pending_restart_keys: string[]
  failed_live_keys: Record<string, ConfigApplyFailure>
}

export interface ConfigMutationSuccess {
  kind: 'success'
  committed: true
  revision: number
  changedKeys: string[]
  applyStatus: 'applied' | 'pending_restart' | 'failed_live' | 'reconcile_failed'
  pendingRestartKeys: string[]
  failedLiveKeys: Record<string, ConfigApplyFailure>
}

export interface ConfigMutationFailure {
  kind: 'conflict' | 'revision_exhausted' | 'error'
  message: string
  retryable: boolean
}

export type ConfigMutationResult = ConfigMutationSuccess | ConfigMutationFailure

export interface ApprovalRulesResponse {
  rules: string[]
  default_rules: string[]
  built_in_exemptions: string[]
}

export interface ConfigYamlDocument {
  revision: number
  content: string
}

type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

export function parseConfigRevision(value: unknown): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    throw new TypeError('Configuration revision must be a safe non-negative integer')
  }
  return value as number
}

function parseFailureMap(value: unknown): Record<string, ConfigApplyFailure> {
  if (!isRecord(value)) return {}
  const failures: Record<string, ConfigApplyFailure> = {}
  for (const [key, failure] of Object.entries(value)) {
    if (!isRecord(failure) || typeof failure.subscriber !== 'string') continue
    failures[key] = {
      revision: parseConfigRevision(failure.revision),
      subscriber: failure.subscriber,
    }
  }
  return failures
}

function parseStringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((entry): entry is string => typeof entry === 'string')
    : []
}

function parseSnapshot(value: unknown): ConfigValuesSnapshot {
  if (!isRecord(value) || !isRecord(value.desired) || !isRecord(value.active)) {
    throw new TypeError('Invalid configuration values response')
  }
  return {
    revision: parseConfigRevision(value.revision),
    desired: value.desired,
    active: value.active,
    secret_set: isRecord(value.secret_set)
      ? value.secret_set as ConfigValuesSnapshot['secret_set']
      : {},
    pending_restart_keys: parseStringArray(value.pending_restart_keys),
    failed_live_keys: parseFailureMap(value.failed_live_keys),
  }
}

function errorBody(value: unknown): { code: string; message: string; retryable: boolean } {
  if (!isRecord(value) || !isRecord(value.error)) {
    return { code: 'request_failed', message: 'Configuration request failed', retryable: false }
  }
  return {
    code: typeof value.error.code === 'string' ? value.error.code : 'request_failed',
    message: typeof value.error.message === 'string'
      ? value.error.message
      : 'Configuration request failed',
    retryable: value.error.retryable === true,
  }
}

export class ConfigurationClient {
  private snapshot: ConfigValuesSnapshot | null = null
  private watermark = 0
  private generation = 0
  private refreshPromise: Promise<ConfigValuesSnapshot | null> | null = null
  private refreshScheduled = false
  private readonly listeners = new Set<(snapshot: ConfigValuesSnapshot) => void>()

  constructor(private readonly fetcher: Fetcher = (input, init) => fetch(input, init)) {}

  get currentSnapshot(): ConfigValuesSnapshot | null {
    return this.snapshot
  }

  subscribe(listener: (snapshot: ConfigValuesSnapshot) => void): () => void {
    this.listeners.add(listener)
    if (this.snapshot) listener(this.snapshot)
    return () => this.listeners.delete(listener)
  }

  reset(): void {
    this.generation += 1
    this.snapshot = null
    this.watermark = 0
    this.refreshPromise = null
    this.refreshScheduled = false
    this.listeners.clear()
  }

  async fetchSchema(): Promise<ConfigSchema | null> {
    const response = await this.fetcher('/api/config/schema')
    if (!response.ok) return null
    const body: unknown = await response.json()
    if (!isRecord(body) || !isRecord(body.properties)) {
      throw new TypeError('Invalid configuration schema response')
    }
    return body as ConfigSchema
  }

  async fetchValues(): Promise<ConfigValuesSnapshot | null> {
    if (this.refreshPromise) return this.refreshPromise
    const refresh = this.runRefresh()
    this.refreshPromise = refresh
    try {
      return await refresh
    } finally {
      if (this.refreshPromise === refresh) this.refreshPromise = null
    }
  }

  observeRevision(value: unknown): void {
    // Called from the WebSocket event dispatcher with untrusted payload data;
    // a malformed revision must be ignored, never thrown into the dispatcher.
    let revision: number
    try {
      revision = parseConfigRevision(value)
    } catch {
      return
    }
    if (revision <= this.watermark) return
    this.watermark = revision
    if (this.refreshScheduled) return
    this.refreshScheduled = true
    queueMicrotask(() => {
      this.refreshScheduled = false
      void this.fetchValues()
    })
  }

  async patch(
    values: Record<string, unknown>,
    unset: readonly string[] = [],
  ): Promise<ConfigMutationResult> {
    const snapshot = this.snapshot ?? await this.fetchValues()
    if (snapshot === null) {
      return { kind: 'error', message: 'Configuration state is unavailable', retryable: true }
    }
    const response = await this.fetcher('/api/config/values', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        expected_revision: snapshot.revision,
        values,
        unset,
      }),
    })
    const body: unknown = await response.json()
    if (!response.ok) {
      const error = errorBody(body)
      if (error.code === 'revision_conflict') {
        const refreshed = await this.fetchValues()
        if (!refreshed || refreshed.revision <= snapshot.revision) await this.fetchValues()
      }
      return {
        kind: error.code === 'revision_conflict'
          ? 'conflict'
          : error.code === 'revision_exhausted'
            ? 'revision_exhausted'
            : 'error',
        message: error.message,
        retryable: error.retryable,
      }
    }
    if (!isRecord(body) || body.committed !== true) {
      return { kind: 'error', message: 'Invalid configuration mutation response', retryable: false }
    }
    const revision = parseConfigRevision(body.revision)
    const pendingRestartKeys = parseStringArray(body.pending_restart_keys)
    const failedLiveKeys = parseFailureMap(body.failed_live_keys)
    // The mutation response has no values payload, so bump the snapshot's
    // revision (subsequent patches must not reuse the stale one) without
    // publishing the torn revision/values pair, then refetch so subscribers
    // see the committed values.
    this.snapshot = {
      ...snapshot,
      revision,
      pending_restart_keys: pendingRestartKeys,
      failed_live_keys: failedLiveKeys,
    }
    await this.refreshAfterCommittedMutation()
    return {
      kind: 'success',
      committed: true,
      revision,
      changedKeys: parseStringArray(body.changed_keys),
      applyStatus: body.apply_status === 'pending_restart'
        || body.apply_status === 'failed_live'
        || body.apply_status === 'reconcile_failed'
        ? body.apply_status
        : 'applied',
      pendingRestartKeys,
      failedLiveKeys,
    }
  }

  /**
   * Patch for fire-and-forget last-write-wins surfaces (ui_settings, project
   * selection): one revision conflict means another writer committed first, so
   * refresh has already happened inside `patch` — retry exactly once on the
   * fresh revision instead of silently losing the write.
   */
  async patchLastWriteWins(values: Record<string, unknown>): Promise<ConfigMutationResult> {
    const result = await this.patch(values)
    if (result.kind !== 'conflict') return result
    return this.patch(values)
  }

  async fetchApprovalRules(): Promise<ApprovalRulesResponse | null> {
    const body = await this.fetchRecord('/api/config/tool-approvals/global')
    if (!body) return null
    return {
      rules: parseStringArray(body.rules),
      default_rules: parseStringArray(body.default_rules),
      built_in_exemptions: parseStringArray(body.built_in_exemptions),
    }
  }

  async fetchTemplate(): Promise<string | null> {
    return (await this.fetchYamlDocument('/api/config/template', 'GET'))?.content ?? null
  }

  async saveTemplate(content: string): Promise<ConfigMutationResult> {
    return this.replaceYaml('/api/config/template', 'PUT', content)
  }

  async exportYaml(): Promise<ConfigYamlDocument | null> {
    return this.fetchYamlDocument('/api/config/export', 'POST')
  }

  async importYaml(content: string): Promise<ConfigMutationResult> {
    return this.replaceYaml('/api/config/import', 'POST', content)
  }

  async fetchSecrets(): Promise<Record<string, unknown> | null> {
    return this.fetchRecord('/api/config/secrets')
  }

  async saveSecret(payload: Record<string, unknown>): Promise<boolean> {
    return (await this.fetcher('/api/config/secrets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })).ok
  }

  async deleteSecret(name: string): Promise<boolean> {
    return (await this.fetcher(`/api/config/secrets/${encodeURIComponent(name)}`, {
      method: 'DELETE',
    })).ok
  }

  async fetchPrompts(): Promise<Record<string, unknown> | null> {
    return this.fetchRecord('/api/config/prompts')
  }

  async fetchPrompt(path: string): Promise<Record<string, unknown> | null> {
    return this.fetchRecord(`/api/config/prompts/${encodeURIComponent(path)}`)
  }

  async savePrompt(path: string, content: string): Promise<boolean> {
    return (await this.fetcher(`/api/config/prompts/${encodeURIComponent(path)}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    })).ok
  }

  async deletePrompt(path: string): Promise<boolean> {
    return (await this.fetcher(`/api/config/prompts/${encodeURIComponent(path)}`, {
      method: 'DELETE',
    })).ok
  }

  async previewValidationDetection(
    command: string,
    config: Record<string, unknown>,
  ): Promise<{ ok: boolean; body: Record<string, unknown> }> {
    const response = await this.fetcher('/api/config/validation-detection/preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ command, config }),
    })
    const body: unknown = await response.json()
    return { ok: response.ok, body: isRecord(body) ? body : {} }
  }

  async runManagedAction(
    action: string,
    payload: Record<string, unknown>,
  ): Promise<boolean> {
    if (!action.startsWith('/api/') || action.includes('://')) {
      throw new TypeError('Managed configuration action must be a local API path')
    }
    return (await this.fetcher(action, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })).ok
  }

  private async runRefresh(): Promise<ConfigValuesSnapshot | null> {
    const generation = this.generation
    let retriedWatermark: number | null = null
    while (true) {
      const response = await this.fetcher('/api/config/values')
      if (generation !== this.generation) return this.snapshot
      if (!response.ok) return this.snapshot
      const next = parseSnapshot(await response.json())
      if (generation !== this.generation) return this.snapshot
      this.watermark = Math.max(this.watermark, next.revision)
      if (this.snapshot === null || next.revision >= this.snapshot.revision) {
        this.snapshot = next
        this.publish(next)
      }
      if (next.revision >= this.watermark || retriedWatermark === this.watermark) {
        return this.snapshot
      }
      retriedWatermark = this.watermark
    }
  }

  private async refreshAfterCommittedMutation(): Promise<void> {
    try {
      await this.fetchValues()
    } catch {
      // The write is already committed. Keep the pre-refresh watermark so the
      // daemon's own-commit event can retry convergence without turning the
      // successful mutation into a UI error.
    }
  }

  private async fetchRecord(
    path: string,
    init?: RequestInit,
  ): Promise<Record<string, unknown> | null> {
    const response = await this.fetcher(path, init)
    if (!response.ok) return null
    const body: unknown = await response.json()
    return isRecord(body) ? body : null
  }

  private async fetchYamlDocument(
    path: string,
    method: 'GET' | 'POST',
  ): Promise<ConfigYamlDocument | null> {
    const body = await this.fetchRecord(path, method === 'GET' ? undefined : { method })
    if (!body || typeof body.content !== 'string') return null
    const revision = parseConfigRevision(body.revision)
    if (revision > this.watermark) this.observeRevision(revision)
    return { revision, content: body.content }
  }

  private async replaceYaml(
    path: string,
    method: 'PUT' | 'POST',
    content: string,
  ): Promise<ConfigMutationResult> {
    const snapshot = this.snapshot ?? await this.fetchValues()
    if (!snapshot) {
      return { kind: 'error', message: 'Configuration state is unavailable', retryable: true }
    }
    const response = await this.fetcher(path, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ expected_revision: snapshot.revision, content }),
    })
    const body: unknown = await response.json()
    if (!response.ok) {
      const error = errorBody(body)
      if (error.code === 'revision_conflict') {
        const refreshed = await this.fetchValues()
        if (!refreshed || refreshed.revision <= snapshot.revision) await this.fetchValues()
      }
      return {
        kind: error.code === 'revision_conflict'
          ? 'conflict'
          : error.code === 'revision_exhausted'
            ? 'revision_exhausted'
            : 'error',
        message: error.message,
        retryable: error.retryable,
      }
    }
    if (!isRecord(body) || body.committed !== true) {
      return { kind: 'error', message: 'Invalid configuration mutation response', retryable: false }
    }
    const revision = parseConfigRevision(body.revision)
    const pendingRestartKeys = parseStringArray(body.pending_restart_keys)
    const failedLiveKeys = parseFailureMap(body.failed_live_keys)
    // A YAML replace rewrites values wholesale and its response carries none of
    // them: bump the revision without publishing the torn snapshot, then
    // refetch so subscribers converge on the committed document.
    this.snapshot = {
      ...snapshot,
      revision,
      pending_restart_keys: pendingRestartKeys,
      failed_live_keys: failedLiveKeys,
    }
    await this.refreshAfterCommittedMutation()
    return {
      kind: 'success',
      committed: true,
      revision,
      changedKeys: parseStringArray(body.changed_keys),
      applyStatus: body.apply_status === 'pending_restart'
        || body.apply_status === 'failed_live'
        || body.apply_status === 'reconcile_failed'
        ? body.apply_status
        : 'applied',
      pendingRestartKeys,
      failedLiveKeys,
    }
  }

  private publish(snapshot: ConfigValuesSnapshot): void {
    for (const listener of this.listeners) listener(snapshot)
  }
}

export const configurationClient = new ConfigurationClient()
