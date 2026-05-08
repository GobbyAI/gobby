import { useState, useEffect } from 'react'

interface MCPServer {
  name: string
  state: string
  connected: boolean
  available: boolean
  transport: string
  enabled?: boolean
}

interface CapabilityGroup {
  label: string
  items: CapabilityItem[]
}

interface CapabilityItem {
  name: string
  available: boolean
  detail?: string
}

const STATE_CLS = 'py-2 text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-muted)]'
const ROOT_CLS = 'flex flex-col gap-[0.6rem]'
const SUMMARY_CLS = 'flex items-center gap-[0.35rem] py-[0.3rem]'
const SUMMARY_COUNT_CLS =
  'font-[inherit] text-[length:calc(var(--font-size-base)*0.8)] font-semibold text-[var(--text-primary)]'
const SUMMARY_LABEL_CLS = 'text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-muted)]'
const GROUP_CLS = 'flex flex-col gap-1'
const GROUP_LABEL_CLS =
  'text-[length:calc(var(--font-size-base)*0.7)] font-semibold uppercase tracking-[0.04em] text-[var(--text-muted)]'
const GROUP_ITEMS_CLS = 'flex flex-wrap gap-[0.3rem]'
const ITEM_CLS =
  'inline-flex items-center gap-1 rounded-full border px-[0.45rem] py-[0.15rem] text-[length:calc(var(--font-size-base)*0.7)]'
const ITEM_ACTIVE_CLS =
  'border-[color-mix(in_srgb,var(--color-success-foreground)_25%,transparent)] bg-[color-mix(in_srgb,var(--color-success-foreground)_8%,transparent)] text-[var(--text-primary)]'
const ITEM_INACTIVE_CLS =
  'border-[var(--border)] bg-[color-mix(in_srgb,var(--text-muted)_6%,transparent)] text-[var(--text-muted)] opacity-70'
const ITEM_DOT_CLS = 'h-1.5 w-1.5 shrink-0 rounded-full'
const ITEM_DOT_ACTIVE_CLS = 'bg-[var(--color-success-foreground)]'
const ITEM_DOT_INACTIVE_CLS = 'bg-[var(--text-muted)]'
const ITEM_NAME_CLS = 'font-medium'
const ITEM_DETAIL_CLS = 'text-[length:calc(var(--font-size-base)*0.6)] text-[var(--text-muted)]'

function getBaseUrl(): string {
  return ''
}

function categorizeServers(servers: MCPServer[]): CapabilityGroup[] {
  const groups: CapabilityGroup[] = []

  const taskServer = servers.find(s => s.name === 'gobby-tasks')
  const workflowServer = servers.find(s => s.name === 'gobby-workflows')
  groups.push({
    label: 'Task Management',
    items: [
      { name: 'Tasks', available: taskServer?.available ?? false, detail: taskServer?.state },
      { name: 'Workflows', available: workflowServer?.available ?? false, detail: workflowServer?.state },
    ],
  })

  const memoryServer = servers.find(s => s.name === 'gobby-memory')
  const skillsServer = servers.find(s => s.name === 'gobby-skills')
  groups.push({
    label: 'Memory & Knowledge',
    items: [
      { name: 'Memory', available: memoryServer?.available ?? false, detail: memoryServer?.state },
      { name: 'Skills', available: skillsServer?.available ?? false, detail: skillsServer?.state },
    ],
  })

  const worktreeServer = servers.find(s => s.name === 'gobby-worktrees')
  const cloneServer = servers.find(s => s.name === 'gobby-clones')
  const mergeServer = servers.find(s => s.name === 'gobby-merge')
  const github = servers.find(s => s.name === 'github')
  groups.push({
    label: 'Code & Git',
    items: [
      { name: 'Worktrees', available: worktreeServer?.available ?? false },
      { name: 'Clones', available: cloneServer?.available ?? false },
      { name: 'Merge', available: mergeServer?.available ?? false },
      { name: 'GitHub', available: github?.available ?? false, detail: github?.transport },
    ],
  })

  const agentServer = servers.find(s => s.name === 'gobby-agents')
  const orchestration = servers.find(s => s.name === 'gobby-orchestration')
  const pipelines = servers.find(s => s.name === 'gobby-pipelines')
  groups.push({
    label: 'Orchestration',
    items: [
      { name: 'Agents', available: agentServer?.available ?? false },
      { name: 'Orchestration', available: orchestration?.available ?? false },
      { name: 'Pipelines', available: pipelines?.available ?? false },
    ],
  })

  const external = servers.filter(
    s => !s.name.startsWith('gobby-') && s.name !== 'github'
  )
  if (external.length > 0) {
    groups.push({
      label: 'External Services',
      items: external.map(s => ({
        name: s.name,
        available: s.available,
        detail: s.transport,
      })),
    })
  }

  return groups
}

interface CapabilityScopeProps {
  sessionId?: string
}

export function CapabilityScope({ sessionId: _sessionId }: CapabilityScopeProps) {
  const [servers, setServers] = useState<MCPServer[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    let cancelled = false

    async function fetchCapabilities() {
      setIsLoading(true)
      setError(null)
      try {
        const baseUrl = getBaseUrl()
        const response = await fetch(`${baseUrl}/api/mcp/servers`, { signal: controller.signal })
        if (!response.ok) {
          console.warn(`MCP servers fetch returned ${response.status}`)
          setError('Failed to load capabilities')
        } else {
          const data = await response.json()
          if (!cancelled) setServers(data.servers || [])
        }
      } catch (e) {
        if (!cancelled) {
          console.error('Failed to fetch MCP servers:', e)
          setError('Failed to load capabilities')
        }
      }
      if (!cancelled) setIsLoading(false)
    }

    fetchCapabilities()
    return () => { cancelled = true; controller.abort() }
  }, [])

  if (isLoading) return <div className={STATE_CLS}>Loading capabilities...</div>
  if (error) return <div className={STATE_CLS}>{error}</div>
  if (servers.length === 0) return <div className={STATE_CLS}>No capability data</div>

  const groups = categorizeServers(servers)
  const totalAvailable = servers.filter(s => s.available).length
  const totalServers = servers.length

  return (
    <div className={ROOT_CLS}>
      <div className={SUMMARY_CLS}>
        <span className={SUMMARY_COUNT_CLS}>{totalAvailable}/{totalServers}</span>
        <span className={SUMMARY_LABEL_CLS}>servers available</span>
      </div>

      {groups.map(group => (
        <div key={group.label} className={GROUP_CLS}>
          <div className={GROUP_LABEL_CLS}>{group.label}</div>
          <div className={GROUP_ITEMS_CLS}>
            {group.items.map(item => (
              <div
                key={item.name}
                className={`${ITEM_CLS} ${item.available ? ITEM_ACTIVE_CLS : ITEM_INACTIVE_CLS}`}
              >
                <span className={`${ITEM_DOT_CLS} ${item.available ? ITEM_DOT_ACTIVE_CLS : ITEM_DOT_INACTIVE_CLS}`} />
                <span className={ITEM_NAME_CLS}>{item.name}</span>
                {item.detail && <span className={ITEM_DETAIL_CLS}>{item.detail}</span>}
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}
