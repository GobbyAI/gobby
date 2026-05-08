import { vi } from 'vitest'

export function createAgentsFetchStub({
  definitions = [],
  branches = [],
  sourceControlStatus = { is_git_repo: false },
  workflows = [],
}: {
  definitions?: unknown[]
  branches?: unknown[]
  sourceControlStatus?: Record<string, unknown>
  workflows?: unknown[]
} = {}) {
  return vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/agents/definitions')) {
      return Response.json({ status: 'success', definitions })
    }
    if (url.includes('/api/source-control/branches')) {
      return Response.json({ branches })
    }
    if (url.includes('/api/source-control/status')) {
      return Response.json(sourceControlStatus)
    }
    if (url.includes('/api/workflows')) {
      return Response.json({ workflows })
    }
    return Response.json({})
  })
}
