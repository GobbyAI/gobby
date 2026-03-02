import { useState, useCallback, useMemo } from 'react'
import { useMcp } from '../../hooks/useMcp'
import type { McpToolSchema } from '../../hooks/useMcp'
import { useConfirmDialog } from '../../hooks/useConfirmDialog'
import { McpOverview } from './McpOverview'
import { McpToolDetail } from './McpToolDetail'
import { McpAddServerModal, McpImportModal } from './McpServerForm'
import './McpPage.css'

type OverviewFilter = 'total' | 'connected' | 'tools' | 'internal' | null

const TRANSPORTS = ['internal', 'http', 'stdio', 'websocket', 'sse'] as const

export function McpPage() {
  const { confirm, ConfirmDialogElement } = useConfirmDialog()
  const {
    servers,
    toolsByServer,
    status,
    isLoading,
    totalToolCount,
    addServer,
    importServer,
    removeServer,
    refreshToolCache,
    fetchToolSchema,
    callTool,
    searchText,
    setSearchText,
  } = useMcp()

  const [overviewFilter, setOverviewFilter] = useState<OverviewFilter>(null)
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
  }, [removeServer, showError])

  const handleRefreshTools = useCallback(async () => {
    const ok = await refreshToolCache()
    if (!ok) showError('Failed to refresh tool cache')
  }, [refreshToolCache, showError])

  // Filtering logic
  const filteredServers = useMemo(() => {
    let result = servers

    // Overview filter
    if (overviewFilter === 'connected') {
      result = result.filter(s => s.connected)
    } else if (overviewFilter === 'internal') {
      result = result.filter(s => s.transport === 'internal')
    }

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
  }, [servers, overviewFilter, transportFilter, searchText, toolsByServer])

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
    <main className="mcp-page">
      {ConfirmDialogElement}
      {errorMessage && (
        <div className="mcp-error-toast" onClick={() => setErrorMessage(null)}>
          {errorMessage}
        </div>
      )}

      {/* Toolbar */}
      <div className="mcp-toolbar">
        <div className="mcp-toolbar-left">
          <h2 className="mcp-toolbar-title">MCP Servers</h2>
        </div>
        <div className="mcp-toolbar-right">
          <input
            className="mcp-search"
            type="text"
            placeholder="Search servers & tools..."
            value={searchText}
            onChange={e => setSearchText(e.target.value)}
          />
          <button
            className="mcp-toolbar-btn"
            onClick={handleRefreshTools}
            title="Clear Cache"
          >
            &#x27f3; Clear Cache
          </button>
          <button
            className="mcp-toolbar-btn"
            onClick={() => setShowImport(true)}
          >
            Import
          </button>
          <button
            className="mcp-new-btn"
            onClick={() => setShowAddServer(true)}
          >
            + Add Server
          </button>
        </div>
      </div>

      {/* Overview cards */}
      <McpOverview
        servers={servers}
        status={status}
        totalToolCount={totalToolCount}
        activeFilter={overviewFilter}
        onFilter={f => setOverviewFilter(f as OverviewFilter)}
      />

      {/* Transport filter chips */}
      <div className="mcp-filter-bar">
        <div className="mcp-filter-chips">
          {TRANSPORTS.map(t => (
            <button
              key={t}
              className={`mcp-filter-chip ${transportFilter === t ? 'mcp-filter-chip--active' : ''}`}
              onClick={() => setTransportFilter(transportFilter === t ? null : t)}
            >
              {t}
            </button>
          ))}
        </div>
      </div>

      {/* Server list */}
      <div className="mcp-content">
        {isLoading ? (
          <div className="mcp-loading">Loading...</div>
        ) : filteredServers.length === 0 ? (
          <div className="mcp-empty">No servers match the current filters.</div>
        ) : (
          <div className="mcp-server-list">
            {filteredServers.map(server => {
              const expanded = expandedServers.has(server.name)
              const tools = getFilteredTools(server.name)
              const allTools = toolsByServer[server.name] || []
              const healthClass = getHealthClass(server.name)

              return (
                <div className="mcp-server-row" key={server.name}>
                  <div
                    className="mcp-server-header"
                    onClick={() => toggleExpand(server.name)}
                  >
                    <span className={`mcp-health-dot mcp-health-dot--${healthClass}`} />
                    <span className="mcp-server-name">{server.name}</span>
                    <span className={`mcp-transport-badge mcp-transport-badge--${server.transport}`}>
                      {server.transport}
                    </span>
                    <span className={`mcp-state-badge mcp-state-badge--${server.state}`}>
                      {server.state}
                    </span>
                    <span className="mcp-server-tool-count">
                      {allTools.length} tool{allTools.length !== 1 ? 's' : ''}
                    </span>
                    {server.transport !== 'internal' && (
                      <button
                        className="mcp-remove-btn"
                        onClick={e => handleRemoveServer(server.name, e)}
                        title="Remove server"
                      >
                        &times;
                      </button>
                    )}
                    <span className={`mcp-server-chevron ${expanded ? 'expanded' : ''}`}>
                      &#x25B8;
                    </span>
                  </div>
                  {expanded && (
                    <div className="mcp-tools-list">
                      {tools.length === 0 ? (
                        <div className="mcp-tool-row" style={{ color: 'var(--text-secondary)', cursor: 'default' }}>
                          No tools available
                        </div>
                      ) : (
                        tools.map(tool => (
                          <div
                            className="mcp-tool-row"
                            key={tool.name}
                            onClick={() => handleSelectTool(server.name, tool.name)}
                          >
                            <span className="mcp-tool-name">{tool.name}</span>
                            <span className="mcp-tool-brief">{tool.brief}</span>
                            <div className="mcp-tool-metrics">
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
