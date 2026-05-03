import { useState, useCallback, useMemo } from 'react'
import { useMcp } from '../../hooks/useMcp'
import type { McpToolSchema } from '../../hooks/useMcp'
import { useConfirmDialog } from '../../hooks/useConfirmDialog'
import { McpToolDetail } from './McpToolDetail'
import { McpAddServerModal, McpImportModal } from './McpServerForm'
import { cn } from '../../lib/utils'

const TRANSPORTS = ['internal', 'http', 'stdio', 'websocket', 'sse'] as const

const PAGE_CLS = 'flex flex-1 flex-col overflow-hidden px-3 md:px-5'
const ERROR_TOAST_CLS =
  'fixed left-1/2 top-[60px] z-[1000] -translate-x-1/2 cursor-pointer rounded-lg bg-[var(--color-error)] px-5 py-2.5 text-[length:var(--text-sm)] text-[var(--accent-foreground)] shadow-md'

const TOOLBAR_CLS = 'flex flex-wrap items-center justify-between gap-4 gap-y-2 pb-3 pt-4'
const TOOLBAR_LEFT_CLS = 'flex min-w-0 items-center gap-3'
const TOOLBAR_TITLE_CLS = 'm-0 text-[length:var(--font-size-base)] font-semibold'
const TOOLBAR_RIGHT_CLS = 'flex min-w-0 flex-wrap items-center gap-2 gap-y-2'

const SEARCH_CLS =
  'w-[140px] rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-2.5 py-1.5 text-[length:var(--text-sm)] text-[var(--text-primary)] outline-none focus:border-[var(--accent)] md:w-[200px] pointer-coarse:min-h-11'
const TOOLBAR_BTN_CLS =
  'cursor-pointer rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-2.5 py-1.5 text-[length:var(--text-sm)] text-[var(--text-primary)] transition-colors duration-150 hover:bg-[rgba(255,255,255,0.05)] pointer-coarse:min-h-11 pointer-coarse:px-3'
const NEW_BTN_CLS =
  'cursor-pointer rounded-md border-0 bg-[var(--accent)] px-3 py-1.5 text-[length:var(--text-sm)] font-medium text-[var(--accent-foreground)] transition-opacity duration-150 hover:opacity-90 pointer-coarse:min-h-11'

const FILTER_BAR_CLS = 'pb-3'
const FILTER_CHIPS_CLS = 'flex flex-wrap gap-1.5'
const FILTER_CHIP_CLS =
  'cursor-pointer rounded-xl border border-[var(--border)] bg-[var(--bg-secondary)] px-2.5 py-1 text-[length:var(--text-2xs)] font-medium uppercase text-[var(--text-secondary)] transition-all duration-150 hover:border-[var(--text-secondary)] pointer-coarse:min-h-11 pointer-coarse:px-3'
const FILTER_CHIP_ACTIVE_CLS =
  'border-[var(--accent)] bg-[rgba(255,255,255,0.03)] text-[var(--accent)]'

const CONTENT_CLS = 'flex-1 overflow-y-auto'
const LOADING_CLS = 'flex items-center justify-center p-10 text-[length:var(--text-base)] text-[var(--text-secondary)]'
const EMPTY_CLS = 'flex items-center justify-center p-10 text-[length:var(--text-sm)] text-[var(--text-secondary)]'

const SERVER_LIST_CLS = 'flex flex-col gap-2 pb-5'
const SERVER_ROW_CLS = 'overflow-hidden rounded-lg border border-[var(--border)] bg-[var(--bg-secondary)]'
const SERVER_HEADER_CLS =
  'group flex cursor-pointer flex-wrap items-center gap-2.5 gap-y-1.5 px-4 py-3 transition-colors duration-100 hover:bg-[rgba(255,255,255,0.02)]'

const HEALTH_DOT_BG: Record<string, string> = {
  healthy: 'bg-[var(--color-success-foreground)]',
  degraded: 'bg-[var(--color-warning-foreground)]',
  unhealthy: 'bg-[var(--color-error)]',
  unknown: 'bg-[var(--text-muted)]',
}
const HEALTH_DOT_CLS = 'h-2 w-2 shrink-0 rounded-full'

const SERVER_NAME_CLS = 'min-w-0 flex-1 overflow-hidden text-ellipsis whitespace-nowrap font-medium text-[length:var(--text-base)] md:min-w-0 md:flex-initial md:overflow-visible md:text-clip md:whitespace-nowrap'

const TRANSPORT_BADGE_BG: Record<string, string> = {
  internal: 'bg-[var(--color-success-soft)] text-[var(--color-success-foreground)]',
  http: 'bg-[var(--color-info-soft)] text-[var(--color-info)]',
  stdio: 'bg-[var(--color-warning-soft)] text-[var(--color-warning-foreground)]',
  websocket: 'bg-[var(--color-review-soft)] text-[var(--color-review)]',
  sse: 'bg-[var(--color-error-soft)] text-[var(--color-error)]',
  unknown: 'bg-[color-mix(in_srgb,var(--text-muted)_12%,transparent)] text-[var(--text-muted)]',
}
const BADGE_BASE_CLS = 'rounded-[10px] px-2 py-0.5 text-[length:var(--text-2xs)] font-medium uppercase tracking-[0.3px]'

const STATE_BADGE_BG: Record<string, string> = {
  connected: 'bg-[var(--color-success-soft)] text-[var(--color-success-foreground)]',
  pending: 'bg-[var(--color-warning-soft)] text-[var(--color-warning-foreground)]',
  configured: 'bg-[var(--color-info-soft)] text-[var(--color-info)]',
  failed: 'bg-[var(--color-error-soft)] text-[var(--color-error)]',
  unknown: 'bg-[color-mix(in_srgb,var(--text-muted)_12%,transparent)] text-[var(--text-muted)]',
}
const STATE_BADGE_BASE_CLS = 'rounded-[10px] px-2 py-0.5 text-[length:var(--text-2xs)] font-medium normal-case tracking-normal'

const SERVER_TOOL_COUNT_CLS = 'ml-auto whitespace-nowrap text-[length:var(--text-sm)] text-[var(--text-secondary)]'
const REMOVE_BTN_CLS =
  'cursor-pointer rounded border border-transparent bg-transparent px-1.5 py-0.5 text-[length:var(--text-base)] text-[var(--text-secondary)] opacity-0 transition-all duration-150 hover:border-[var(--color-error)] hover:text-[var(--color-error)] group-hover:opacity-100 pointer-coarse:opacity-100 pointer-coarse:h-11 pointer-coarse:w-11'
const SERVER_CHEVRON_CLS = 'shrink-0 text-[length:var(--text-sm)] text-[var(--text-secondary)] transition-transform duration-200'
const SERVER_CHEVRON_EXPANDED_CLS = 'rotate-90'

const TOOLS_LIST_CLS = 'border-t border-[var(--border)]'
const TOOL_ROW_CLS =
  'flex cursor-pointer items-center gap-3 py-2 pl-10 pr-4 transition-colors duration-100 hover:bg-[rgba(255,255,255,0.02)] [&+&]:border-t [&+&]:border-[var(--border)] pointer-coarse:min-h-11'
const TOOL_NAME_CLS = 'whitespace-nowrap text-[length:var(--text-md)] font-medium'
const TOOL_BRIEF_CLS = 'flex-1 overflow-hidden text-ellipsis whitespace-nowrap text-[length:var(--text-sm)] text-[var(--text-secondary)]'
const TOOL_METRICS_CLS = 'flex gap-3 whitespace-nowrap text-[length:var(--text-xs)] text-[var(--text-secondary)]'
const NO_TOOLS_CLS = 'flex cursor-default items-center px-4 py-2 pl-10 text-[length:var(--text-sm)] text-[var(--text-secondary)]'

export function McpPage() {
  const { confirm, ConfirmDialogElement } = useConfirmDialog()
  const {
    servers,
    toolsByServer,
    status,
    isLoading,
    addServer,
    importServer,
    removeServer,
    refreshToolCache,
    fetchToolSchema,
    callTool,
    searchText,
    setSearchText,
  } = useMcp()

  const [transportFilter, setTransportFilter] = useState<string | null>(null)
  const [expandedServers, setExpandedServers] = useState<Set<string>>(new Set())
  const [selectedTool, setSelectedTool] = useState<{ server: string; tool: string } | null>(null)
  const [toolSchema, setToolSchema] = useState<McpToolSchema | null>(null)
  const [schemaLoading, setSchemaLoading] = useState(false)
  const [showAddServer, setShowAddServer] = useState(false)
  const [showImport, setShowImport] = useState(false)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)

  const showError = useCallback((msg: string) => {
    setErrorMessage(msg)
    setTimeout(() => setErrorMessage(null), 4000)
  }, [])

  const toggleExpand = useCallback((name: string) => {
    setExpandedServers(prev => {
      const next = new Set(prev)
      if (next.has(name)) next.delete(name)
      else next.add(name)
      return next
    })
  }, [])

  const handleSelectTool = useCallback(async (serverName: string, toolName: string) => {
    setSelectedTool({ server: serverName, tool: toolName })
    setToolSchema(null)
    setSchemaLoading(true)
    const schema = await fetchToolSchema(serverName, toolName)
    setToolSchema(schema)
    setSchemaLoading(false)
  }, [fetchToolSchema])

  const handleCloseTool = useCallback(() => {
    setSelectedTool(null)
    setToolSchema(null)
  }, [])

  const handleRemoveServer = useCallback(async (name: string, e: React.MouseEvent) => {
    e.stopPropagation()
    if (!await confirm({ title: `Remove "${name}"?`, confirmLabel: 'Remove', destructive: true })) return
    const ok = await removeServer(name)
    if (!ok) showError(`Failed to remove ${name}`)
  }, [confirm, removeServer, showError])

  const handleRefreshTools = useCallback(async () => {
    const ok = await refreshToolCache()
    if (!ok) showError('Failed to refresh tool cache')
  }, [refreshToolCache, showError])

  // Filtering logic
  const filteredServers = useMemo(() => {
    let result = servers

    // Transport chip filter
    if (transportFilter) {
      result = result.filter(s => s.transport === transportFilter)
    }

    // Search filter
    if (searchText.trim()) {
      const q = searchText.toLowerCase()
      result = result.filter(s => {
        if (s.name.toLowerCase().includes(q)) return true
        const tools = toolsByServer[s.name] || []
        return tools.some(t =>
          t.name.toLowerCase().includes(q) ||
          t.brief.toLowerCase().includes(q)
        )
      })
    }

    return result
  }, [servers, transportFilter, searchText, toolsByServer])

  // Filter tools within a server based on search
  const getFilteredTools = useCallback((serverName: string) => {
    const tools = toolsByServer[serverName] || []
    if (!searchText.trim()) return tools
    const q = searchText.toLowerCase()
    return tools.filter(t =>
      t.name.toLowerCase().includes(q) ||
      t.brief.toLowerCase().includes(q)
    )
  }, [toolsByServer, searchText])

  const getHealthClass = useCallback((serverName: string) => {
    const health = status?.server_health?.[serverName]
    if (!health) return 'unknown'
    return health.health
  }, [status])

  return (
    <main className={PAGE_CLS}>
      {ConfirmDialogElement}
      {errorMessage && (
        <div className={ERROR_TOAST_CLS} onClick={() => setErrorMessage(null)}>
          {errorMessage}
        </div>
      )}

      {/* Toolbar */}
      <div className={TOOLBAR_CLS}>
        <div className={TOOLBAR_LEFT_CLS}>
          <h1 className={TOOLBAR_TITLE_CLS}>MCP Servers</h1>
        </div>
        <div className={TOOLBAR_RIGHT_CLS}>
          <input
            className={SEARCH_CLS}
            type="text"
            placeholder="Search servers & tools..."
            value={searchText}
            onChange={e => setSearchText(e.target.value)}
          />
          <button
            className={TOOLBAR_BTN_CLS}
            onClick={handleRefreshTools}
            title="Clear Cache"
          >
            &#x27f3; Clear Cache
          </button>
          <button
            className={TOOLBAR_BTN_CLS}
            onClick={() => setShowImport(true)}
          >
            Import
          </button>
          <button
            className={NEW_BTN_CLS}
            onClick={() => setShowAddServer(true)}
          >
            + Add Server
          </button>
        </div>
      </div>

      {/* Transport filter chips */}
      <div className={FILTER_BAR_CLS}>
        <div className={FILTER_CHIPS_CLS}>
          {TRANSPORTS.map(t => (
            <button
              key={t}
              className={cn(FILTER_CHIP_CLS, transportFilter === t && FILTER_CHIP_ACTIVE_CLS)}
              onClick={() => setTransportFilter(transportFilter === t ? null : t)}
            >
              {t}
            </button>
          ))}
        </div>
      </div>

      {/* Server list */}
      <div className={CONTENT_CLS}>
        {isLoading ? (
          <div className={LOADING_CLS}>Loading...</div>
        ) : filteredServers.length === 0 ? (
          <div className={EMPTY_CLS}>No servers match the current filters.</div>
        ) : (
          <div className={SERVER_LIST_CLS}>
            {filteredServers.map(server => {
              const expanded = expandedServers.has(server.name)
              const tools = getFilteredTools(server.name)
              const allTools = toolsByServer[server.name] || []
              const healthClass = getHealthClass(server.name)

              return (
                <div className={SERVER_ROW_CLS} key={server.name}>
                  <div
                    className={SERVER_HEADER_CLS}
                    onClick={() => toggleExpand(server.name)}
                  >
                    <span className={cn(HEALTH_DOT_CLS, HEALTH_DOT_BG[healthClass] ?? HEALTH_DOT_BG.unknown)} />
                    <span className={SERVER_NAME_CLS}>{server.name}</span>
                    <span className={cn(BADGE_BASE_CLS, TRANSPORT_BADGE_BG[server.transport] ?? TRANSPORT_BADGE_BG.unknown)}>
                      {server.transport}
                    </span>
                    <span className={cn(STATE_BADGE_BASE_CLS, STATE_BADGE_BG[server.state] ?? STATE_BADGE_BG.unknown)}>
                      {server.state}
                    </span>
                    <span className={SERVER_TOOL_COUNT_CLS}>
                      {allTools.length} tool{allTools.length !== 1 ? 's' : ''}
                    </span>
                    {server.transport !== 'internal' && (
                      <button
                        className={REMOVE_BTN_CLS}
                        onClick={e => handleRemoveServer(server.name, e)}
                        title="Remove server"
                      >
                        &times;
                      </button>
                    )}
                    <span className={cn(SERVER_CHEVRON_CLS, expanded && SERVER_CHEVRON_EXPANDED_CLS)}>
                      &#x25B8;
                    </span>
                  </div>
                  {expanded && (
                    <div className={TOOLS_LIST_CLS}>
                      {tools.length === 0 ? (
                        <div className={NO_TOOLS_CLS}>
                          No tools available
                        </div>
                      ) : (
                        tools.map(tool => (
                          <div
                            className={TOOL_ROW_CLS}
                            key={tool.name}
                            onClick={() => handleSelectTool(server.name, tool.name)}
                          >
                            <span className={TOOL_NAME_CLS}>{tool.name}</span>
                            <span className={TOOL_BRIEF_CLS}>{tool.brief}</span>
                            <div className={TOOL_METRICS_CLS}>
                              {(tool.call_count ?? 0) > 0 && (
                                <span>{tool.call_count} calls</span>
                              )}
                              {tool.success_rate != null && (
                                <span>{(tool.success_rate * 100).toFixed(0)}%</span>
                              )}
                              {tool.avg_latency_ms != null && (
                                <span>{tool.avg_latency_ms.toFixed(0)}ms</span>
                              )}
                            </div>
                          </div>
                        ))
                      )}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>

      {/* Detail slide-out */}
      <McpToolDetail
        serverName={selectedTool?.server ?? null}
        toolName={selectedTool?.tool ?? null}
        schema={toolSchema}
        isLoading={schemaLoading}
        onClose={handleCloseTool}
        onCallTool={callTool}
      />

      {/* Modals */}
      {showAddServer && (
        <McpAddServerModal
          onAdd={addServer}
          onClose={() => setShowAddServer(false)}
        />
      )}
      {showImport && (
        <McpImportModal
          onImport={importServer}
          onClose={() => setShowImport(false)}
        />
      )}
    </main>
  )
}
