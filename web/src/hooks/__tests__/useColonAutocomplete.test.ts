import { describe, it, expect } from 'vitest'
import { buildToolIndex } from '../useColonAutocomplete'
import type { McpServer, McpTool } from '../useMcp'

function makeServer(name: string, transport: string): McpServer {
  return { name, state: 'running', connected: true, available: true, transport }
}

function makeTool(name: string, brief: string): McpTool {
  return { name, brief }
}

// ---------------------------------------------------------------------------
// buildToolIndex
// ---------------------------------------------------------------------------
describe('buildToolIndex', () => {
  it('returns empty array when no servers or tools', () => {
    expect(buildToolIndex([], {})).toEqual([])
  })

  it('builds entries from a single server with tools', () => {
    const servers = [makeServer('gobby', 'internal')]
    const toolsByServer = {
      gobby: [makeTool('create_task', 'Create a task'), makeTool('list_tasks', 'List tasks')],
    }
    const result = buildToolIndex(servers, toolsByServer)

    expect(result).toHaveLength(2)
    expect(result[0]).toEqual({
      name: 'create_task',
      brief: 'Create a task',
      serverName: 'gobby',
      transport: 'internal',
    })
    expect(result[1]).toEqual({
      name: 'list_tasks',
      brief: 'List tasks',
      serverName: 'gobby',
      transport: 'internal',
    })
  })

  it('builds entries from multiple servers', () => {
    const servers = [
      makeServer('gobby', 'internal'),
      makeServer('github', 'stdio'),
    ]
    const toolsByServer = {
      gobby: [makeTool('create_task', 'Create a task')],
      github: [makeTool('search_repos', 'Search repos')],
    }
    const result = buildToolIndex(servers, toolsByServer)

    expect(result).toHaveLength(2)
    expect(result[0].serverName).toBe('gobby')
    expect(result[0].transport).toBe('internal')
    expect(result[1].serverName).toBe('github')
    expect(result[1].transport).toBe('stdio')
  })

  it('uses "unknown" transport when server not found in server list', () => {
    const servers: McpServer[] = []
    const toolsByServer = {
      mystery: [makeTool('do_stuff', 'Does stuff')],
    }
    const result = buildToolIndex(servers, toolsByServer)

    expect(result).toHaveLength(1)
    expect(result[0].transport).toBe('unknown')
  })

  it('handles servers with no tools', () => {
    const servers = [makeServer('gobby', 'internal')]
    const toolsByServer: Record<string, McpTool[]> = {}
    const result = buildToolIndex(servers, toolsByServer)

    expect(result).toEqual([])
  })

  it('handles empty tools array for a server', () => {
    const servers = [makeServer('gobby', 'internal')]
    const toolsByServer = { gobby: [] as McpTool[] }
    const result = buildToolIndex(servers, toolsByServer)

    expect(result).toEqual([])
  })
})
