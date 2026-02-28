import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { useWebSocketEvent } from './useWebSocketEvent'

export interface McpServer {
  name: string
  state: string
  connected: boolean
  available: boolean
  transport: string
  enabled?: boolean
  note?: string
}

export interface McpTool {
  name: string
  brief: string
  call_count?: number
  success_rate?: number | null
  avg_latency_ms?: number | null
}

export interface McpServerHealth {
  state: string
  health: string
  failures: number
}

export interface McpStatus {
  total_servers: number
  connected_servers: number
  cached_tools: number
  server_health: Record<string, McpServerHealth>
}

export interface McpToolSchema {
  name: string
  description?: string
  inputSchema: Record<string, unknown> | null
}

function getBaseUrl(): string {
  return ''
}

export function useMcp() {
  const [servers, setServers] = useState<McpServer[]>([])
  const [toolsByServer, setToolsByServer] = useState<Record<string, McpTool[]>>({})
  const [status, setStatus] = useState<McpStatus | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [searchText, setSearchText] = useState('')
  const mcpDebounceRef = useRef<number | null>(null)

  const fetchServers = useCallback(async () => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/mcp/servers`)
      if (response.ok) {
        const data = await response.json()
        setServers(data.servers || [])
      }
    } catch (e) {
      console.error('Failed to fetch MCP servers:', e)
    }
  }, [])

  const fetchTools = useCallback(async () => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/mcp/tools?include_metrics=true`)
      if (response.ok) {
        const data = await response.json()
        setToolsByServer(data.tools || {})
      }
    } catch (e) {
      console.error('Failed to fetch MCP tools:', e)
    }
  }, [])

  const fetchStatus = useCallback(async () => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/mcp/status`)
      if (response.ok) {
        const data = await response.json()
        setStatus(data)
      }
    } catch (e) {
      console.error('Failed to fetch MCP status:', e)
    }
  }, [])

  const refreshAll = useCallback(async () => {
    setIsLoading(true)
    await Promise.all([fetchServers(), fetchTools(), fetchStatus()])
    setIsLoading(false)
  }, [fetchServers, fetchTools, fetchStatus])

  const addServer = useCallback(async (params: {
    name: string
    transport: string
    url?: string
    command?: string
    args?: string[]
    enabled?: boolean
  }): Promise<boolean> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/mcp/servers`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params),
      })
      if (response.ok) {
        const data = await response.json()
        if (data.success) {
          await fetchServers()
          return true
        }
      }
    } catch (e) {
      console.error('Failed to add MCP server:', e)
    }
    return false
  }, [fetchServers])

  const importServer = useCallback(async (params: {
    from_project?: string
    github_url?: string
    query?: string
    servers?: string[]
  }): Promise<boolean> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/mcp/servers/import`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(params),
      })
      if (response.ok) {
        const data = await response.json()
        if (data.success) {
          await refreshAll()
          return true
        }
      }
    } catch (e) {
      console.error('Failed to import MCP server:', e)
    }
    return false
  }, [refreshAll])

  const removeServer = useCallback(async (name: string): Promise<boolean> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/mcp/servers/${encodeURIComponent(name)}`, {
        method: 'DELETE',
      })
      if (response.ok) {
        const data = await response.json()
        if (data.success) {
          await fetchServers()
          return true
        }
      }
    } catch (e) {
      console.error('Failed to remove MCP server:', e)
    }
    return false
  }, [fetchServers])

  const refreshToolCache = useCallback(async (): Promise<boolean> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/mcp/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      })
      if (response.ok) {
        const data = await response.json()
        if (data.success) {
          await fetchTools()
          return true
        }
      }
    } catch (e) {
      console.error('Failed to refresh MCP tools:', e)
    }
    return false
  }, [fetchTools])

  const fetchToolSchema = useCallback(async (
    serverName: string,
    toolName: string,
  ): Promise<McpToolSchema | null> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/mcp/tools/schema`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ server_name: serverName, tool_name: toolName }),
      })
      if (response.ok) {
        const data = await response.json()
        if (data.success) {
          return {
            name: data.name,
            description: data.description,
            inputSchema: data.inputSchema,
          }
        }
      }
    } catch (e) {
      console.error('Failed to fetch tool schema:', e)
    }
    return null
  }, [])

  const callTool = useCallback(async (
    serverName: string,
    toolName: string,
    args: Record<string, unknown>,
  ): Promise<{ success: boolean; result?: unknown; error?: string }> => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/mcp/tools/call`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          server_name: serverName,
          tool_name: toolName,
          arguments: args,
        }),
      })
      const data = await response.json()
      return {
        success: data.success,
        result: data.result,
        error: data.error || data.result?.error,
      }
    } catch (e) {
      return { success: false, error: String(e) }
    }
  }, [])

  const totalToolCount = useMemo(() => {
    return Object.values(toolsByServer).reduce((sum, tools) => sum + tools.length, 0)
  }, [toolsByServer])

  // Auto-fetch on mount
  useEffect(() => {
    refreshAll()
  }, [refreshAll])

  // Cleanup debounce timer on unmount
  useEffect(() => {
    return () => {
      if (mcpDebounceRef.current) window.clearTimeout(mcpDebounceRef.current)
    }
  }, [])

  // Real-time updates via WebSocket (debounced to avoid redundant fetches)
  useWebSocketEvent(
    'mcp_event',
    useCallback(() => {
      if (mcpDebounceRef.current) window.clearTimeout(mcpDebounceRef.current)
      mcpDebounceRef.current = window.setTimeout(() => {
        refreshAll()
      }, 500)
    }, [refreshAll]),
  )

  return {
    servers,
    toolsByServer,
    status,
    isLoading,
    totalToolCount,
    fetchServers,
    fetchTools,
    fetchStatus,
    refreshAll,
    addServer,
    importServer,
    removeServer,
    refreshToolCache,
    fetchToolSchema,
    callTool,
    searchText,
    setSearchText,
  }
}
