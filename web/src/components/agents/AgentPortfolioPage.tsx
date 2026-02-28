import { useState, useEffect, useCallback, useMemo } from 'react'

// =============================================================================
// Types
// =============================================================================

interface SessionData {
  id: string
  ref: string
  source: string
  title: string | null
  status: string
  model: string | null
  message_count: number
  created_at: string
  updated_at: string
  usage_input_tokens: number
  usage_output_tokens: number
  usage_total_cost_usd: number
  had_edits: boolean
  agent_depth: number
  parent_session_id: string | null
}

interface TaskData {
  id: string
  ref: string
  title: string
  status: string
  priority: number
  type: string
  category: string | null
  assignee: string | null
  agent_name: string | null
  created_at: string
  updated_at: string
  closed_at: string | null
  closed_in_session_id: string | null
  created_in_session_id: string | null
  validation_fail_count: number
  escalated_at: string | null
}

interface AgentProfile {
  id: string
  name: string
  source: string
  sessionCount: number
  sessions: SessionData[]
  tasksAssigned: TaskData[]
  tasksClosed: TaskData[]
  tasksEscalated: TaskData[]
  totalTokensIn: number
  totalTokensOut: number
  totalCost: number
  avgDurationMinutes: number
  successRate: number
  categoryBreakdown: Record<string, number>
  failureModes: string[]
  lastActive: string
}

type SortField = 'name' | 'tasks' | 'success' | 'cost' | 'lastActive'

// =============================================================================
// Helpers
// =============================================================================

function getBaseUrl(): string {
  return ''
}

function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const minutes = Math.floor(diff / (1000 * 60))
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days}d ago`
  return `${Math.floor(days / 30)}mo ago`
}

function formatDuration(minutes: number): string {
  if (minutes < 60) return `${Math.round(minutes)}m`
  const hours = Math.floor(minutes / 60)
  const mins = Math.round(minutes % 60)
  return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(n)
}

function formatCost(usd: number): string {
  if (usd >= 0.01) return `$${usd.toFixed(2)}`
  return `$${usd.toFixed(3)}`
}

function identifyAgent(session: SessionData): { id: string; name: string; source: string } {
  // Group by source (claude, gemini, codex, web-chat)
  // For agent-depth > 0, prefix with "sub-"
  const prefix = session.agent_depth > 0 ? 'sub-' : ''
  const source = session.source || 'unknown'
  const id = `${prefix}${source}`
  const name = `${prefix ? 'Sub-' : ''}${source.charAt(0).toUpperCase() + source.slice(1)}`
  return { id, name, source }
}

// =============================================================================
// Sub-components
// =============================================================================

function SuccessBar({ rate }: { rate: number }) {
  const pct = Math.round(rate * 100)
  const color = pct >= 80 ? '#22c55e' : pct >= 50 ? '#f59e0b' : '#ef4444'
  return (
    <div className="agent-success-bar">
      <div className="agent-success-bar-fill" style={{ width: `${pct}%`, background: color }} />
      <span className="agent-success-bar-label">{pct}%</span>
    </div>
  )
}

function CategoryChart({ breakdown }: { breakdown: Record<string, number> }) {
  const entries = Object.entries(breakdown).sort((a, b) => b[1] - a[1])
  const total = entries.reduce((sum, [, n]) => sum + n, 0)
  if (total === 0) return <span className="agent-muted">No tasks</span>

  const COLORS: Record<string, string> = {
    code: '#3b82f6',
    test: '#22c55e',
    docs: '#a855f7',
    config: '#f59e0b',
    refactor: '#06b6d4',
    research: '#ec4899',
    planning: '#737373',
    manual: '#f97316',
  }

  return (
    <div className="agent-category-chart">
      <div className="agent-category-bar">
        {entries.map(([cat, count]) => (
          <div
            key={cat}
            className="agent-category-segment"
            style={{
              width: `${(count / total) * 100}%`,
              background: COLORS[cat] || '#525252',
            }}
            title={`${cat}: ${count}`}
          />
        ))}
      </div>
      <div className="agent-category-legend">
        {entries.slice(0, 4).map(([cat, count]) => (
          <span key={cat} className="agent-category-label">
            <span className="agent-category-dot" style={{ background: COLORS[cat] || '#525252' }} />
            {cat} ({count})
          </span>
        ))}
      </div>
    </div>
  )
}

function AgentCard({
  agent,
  isExpanded,
  onToggle,
}: {
  agent: AgentProfile
  isExpanded: boolean
  onToggle: () => void
}) {
  return (
    <div className={`agent-card ${isExpanded ? 'agent-card--expanded' : ''}`}>
      <button className="agent-card-header" onClick={onToggle}>
        <div className="agent-card-identity">
          <span className="agent-card-icon">{agent.source === 'claude' ? '\u2728' : agent.source === 'gemini' ? '\u2666' : agent.source === 'codex' ? '\u{1F4E6}' : '\u{1F916}'}</span>
          <span className="agent-card-name">{agent.name}</span>
          <span className="agent-card-sessions">{agent.sessionCount} sessions</span>
        </div>
        <div className="agent-card-stats">
          <span className="agent-stat">
            <span className="agent-stat-value">{agent.tasksClosed.length}</span>
            <span className="agent-stat-label">closed</span>
          </span>
          <span className="agent-stat">
            <SuccessBar rate={agent.successRate} />
          </span>
          <span className="agent-stat">
            <span className="agent-stat-value">{formatDuration(agent.avgDurationMinutes)}</span>
            <span className="agent-stat-label">avg time</span>
          </span>
          <span className="agent-stat">
            <span className="agent-stat-value">{formatCost(agent.totalCost)}</span>
            <span className="agent-stat-label">cost</span>
          </span>
          <span className="agent-card-time">{relativeTime(agent.lastActive)}</span>
        </div>
        <span className="agent-card-chevron">{isExpanded ? '\u25BE' : '\u25B8'}</span>
      </button>

      {isExpanded && (
        <div className="agent-card-detail">
          <div className="agent-detail-grid">
            {/* Token usage */}
            <div className="agent-detail-section">
              <h4 className="agent-detail-title">Token Usage</h4>
              <div className="agent-detail-row">
                <span>Input</span>
                <span className="agent-detail-value">{formatTokens(agent.totalTokensIn)}</span>
              </div>
              <div className="agent-detail-row">
                <span>Output</span>
                <span className="agent-detail-value">{formatTokens(agent.totalTokensOut)}</span>
              </div>
              <div className="agent-detail-row">
                <span>Total Cost</span>
                <span className="agent-detail-value">{formatCost(agent.totalCost)}</span>
              </div>
            </div>

            {/* Task breakdown */}
            <div className="agent-detail-section">
              <h4 className="agent-detail-title">Task Breakdown</h4>
              <div className="agent-detail-row">
                <span>Assigned</span>
                <span className="agent-detail-value">{agent.tasksAssigned.length}</span>
              </div>
              <div className="agent-detail-row">
                <span>Closed</span>
                <span className="agent-detail-value">{agent.tasksClosed.length}</span>
              </div>
              <div className="agent-detail-row">
                <span>Escalated</span>
                <span className="agent-detail-value agent-detail-value--danger">
                  {agent.tasksEscalated.length}
                </span>
              </div>
              <div className="agent-detail-row">
                <span>Escalated</span>
                <span className="agent-detail-value agent-detail-value--warn">
                  {agent.tasksEscalated.length}
                </span>
              </div>
            </div>

            {/* Domain strengths */}
            <div className="agent-detail-section agent-detail-section--wide">
              <h4 className="agent-detail-title">Domain Strengths</h4>
              <CategoryChart breakdown={agent.categoryBreakdown} />
            </div>

            {/* Failure modes */}
            {agent.failureModes.length > 0 && (
              <div className="agent-detail-section agent-detail-section--wide">
                <h4 className="agent-detail-title">Common Failure Patterns</h4>
                <div className="agent-failure-list">
                  {agent.failureModes.map((mode, i) => (
                    <span key={i} className="agent-failure-tag">{mode}</span>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* Recent closed tasks */}
          {agent.tasksClosed.length > 0 && (
            <div className="agent-detail-section">
              <h4 className="agent-detail-title">Recent Completed Tasks</h4>
              <div className="agent-recent-tasks">
                {agent.tasksClosed.slice(0, 8).map(task => (
                  <div key={task.id} className="agent-recent-task">
                    <span className="agent-recent-task-ref">{task.ref}</span>
                    <span className="agent-recent-task-title">{task.title}</span>
                    {task.category && <span className="agent-recent-task-cat">{task.category}</span>}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// =============================================================================
// AgentPortfolioPage
// =============================================================================

export function AgentPortfolioPage() {
  const [sessions, setSessions] = useState<SessionData[]>([])
  const [tasks, setTasks] = useState<TaskData[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [sortField, setSortField] = useState<SortField>('tasks')
  const [expandedAgent, setExpandedAgent] = useState<string | null>(null)
  const [filterSource, setFilterSource] = useState<string | null>(null)

  const fetchData = useCallback(async () => {
    setIsLoading(true)
    try {
      setError(null)
      const baseUrl = getBaseUrl()
      const [sessRes, taskRes] = await Promise.all([
        fetch(`${baseUrl}/api/sessions?limit=500`),
        fetch(`${baseUrl}/api/tasks?limit=500`),
      ])

      if (sessRes.ok) {
        const data = await sessRes.json()
        setSessions(data.sessions || [])
      }
      if (taskRes.ok) {
        const data = await taskRes.json()
        setTasks(data.tasks || [])
      }
      if (!sessRes.ok || !taskRes.ok) {
        setError('Failed to load some agent data')
      }
    } catch (e) {
      console.error('Failed to fetch agent data:', e)
      setError('Failed to fetch agent data')
    } finally {
      setIsLoading(false)
    }
  }, [])

  useEffect(() => { fetchData() }, [fetchData])

  // Build agent profiles from sessions + tasks
  const agents = useMemo((): AgentProfile[] => {
    // Group sessions by agent identity
    const agentMap = new Map<string, { name: string; source: string; sessions: SessionData[] }>()

    for (const session of sessions) {
      const { id, name, source } = identifyAgent(session)
      if (!agentMap.has(id)) {
        agentMap.set(id, { name, source, sessions: [] })
      }
      agentMap.get(id)!.sessions.push(session)
    }

    // Build session ID sets for each agent
    const profiles: AgentProfile[] = []

    for (const [id, group] of agentMap) {
      const sessionIds = new Set(group.sessions.map(s => s.id))

      // Tasks assigned to sessions of this agent
      const assigned = tasks.filter(t => t.assignee && sessionIds.has(t.assignee))
      const closed = tasks.filter(
        t => t.closed_in_session_id && sessionIds.has(t.closed_in_session_id) && (t.status === 'closed' || t.status === 'review_approved')
      )
      const escalated = tasks.filter(
        t => (t.assignee && sessionIds.has(t.assignee)) && t.escalated_at !== null
      )

      // Aggregate tokens/cost
      let totalIn = 0, totalOut = 0, totalCost = 0
      for (const s of group.sessions) {
        totalIn += s.usage_input_tokens || 0
        totalOut += s.usage_output_tokens || 0
        totalCost += s.usage_total_cost_usd || 0
      }

      // Average task duration (created_at → closed_at)
      const durations: number[] = []
      for (const t of closed) {
        if (t.closed_at && t.created_at) {
          const dur = (new Date(t.closed_at).getTime() - new Date(t.created_at).getTime()) / (1000 * 60)
          if (dur > 0 && dur < 60 * 24 * 7) durations.push(dur) // Exclude outliers > 7d
        }
      }
      const avgDuration = durations.length > 0
        ? durations.reduce((a, b) => a + b, 0) / durations.length
        : 0

      // Success rate: closed / (assigned non-open)
      const attempted = assigned.filter(t => t.status !== 'open')
      const successRate = attempted.length > 0 ? closed.length / attempted.length : 0

      // Category breakdown from closed tasks
      const cats: Record<string, number> = {}
      for (const t of closed) {
        const cat = t.category || 'uncategorized'
        cats[cat] = (cats[cat] || 0) + 1
      }

      // Failure modes from failed/escalated
      const modes: string[] = []
      const failedWithValidation = tasks.filter(
        t => (t.assignee && sessionIds.has(t.assignee)) && t.validation_fail_count > 0
      )
      if (failedWithValidation.length > 0) modes.push(`Validation failures (${failedWithValidation.length})`)
      if (escalated.length > 0) modes.push(`Escalations (${escalated.length})`)

      // Last active
      const lastActive = group.sessions.reduce((latest, s) => {
        return new Date(s.updated_at).getTime() > new Date(latest).getTime() ? s.updated_at : latest
      }, group.sessions[0].updated_at)

      profiles.push({
        id,
        name: group.name,
        source: group.source,
        sessionCount: group.sessions.length,
        sessions: group.sessions,
        tasksAssigned: assigned,
        tasksClosed: closed,
        tasksEscalated: escalated,
        totalTokensIn: totalIn,
        totalTokensOut: totalOut,
        totalCost: totalCost,
        avgDurationMinutes: avgDuration,
        successRate,
        categoryBreakdown: cats,
        failureModes: modes,
        lastActive,
      })
    }

    return profiles
  }, [sessions, tasks])

  // Filter and sort
  const displayAgents = useMemo(() => {
    let result = agents
    if (filterSource) {
      result = result.filter(a => a.source === filterSource)
    }

    result.sort((a, b) => {
      switch (sortField) {
        case 'name': return a.name.localeCompare(b.name)
        case 'tasks': return b.tasksClosed.length - a.tasksClosed.length
        case 'success': return b.successRate - a.successRate
        case 'cost': return b.totalCost - a.totalCost
        case 'lastActive': return new Date(b.lastActive).getTime() - new Date(a.lastActive).getTime()
        default: return 0
      }
    })

    return result
  }, [agents, filterSource, sortField])

  // Aggregate stats
  const totals = useMemo(() => {
    return {
      agents: agents.length,
      sessions: sessions.length,
      tasksClosed: agents.reduce((s, a) => s + a.tasksClosed.length, 0),
      totalCost: agents.reduce((s, a) => s + a.totalCost, 0),
      avgSuccess: agents.length > 0
        ? agents.reduce((s, a) => s + a.successRate, 0) / agents.length
        : 0,
    }
  }, [agents, sessions])

  const sources = useMemo(() => {
    const s = new Set(agents.map(a => a.source))
    return Array.from(s).sort()
  }, [agents])

  return (
    <main className="agent-portfolio-page">
      {/* Toolbar */}
      <div className="agent-toolbar">
        <div className="agent-toolbar-left">
          <h2 className="agent-page-title">Agent Portfolio</h2>
          <span className="agent-page-count">{agents.length} agents</span>
        </div>
        <div className="agent-toolbar-right">
          <select
            className="agent-filter-select"
            value={filterSource ?? ''}
            onChange={e => setFilterSource(e.target.value || null)}
          >
            <option value="">All Sources</option>
            {sources.map(s => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
          <select
            className="agent-filter-select"
            value={sortField}
            onChange={e => setSortField(e.target.value as SortField)}
          >
            <option value="tasks">Sort: Tasks Closed</option>
            <option value="success">Sort: Success Rate</option>
            <option value="cost">Sort: Cost</option>
            <option value="lastActive">Sort: Last Active</option>
            <option value="name">Sort: Name</option>
          </select>
          <button className="agent-refresh-btn" onClick={fetchData} title="Refresh">
            \u21BB
          </button>
        </div>
      </div>

      {/* Summary cards */}
      <div className="agent-summary-cards">
        <div className="agent-summary-card">
          <span className="agent-summary-value">{totals.agents}</span>
          <span className="agent-summary-label">Agent Types</span>
        </div>
        <div className="agent-summary-card">
          <span className="agent-summary-value">{totals.sessions}</span>
          <span className="agent-summary-label">Total Sessions</span>
        </div>
        <div className="agent-summary-card">
          <span className="agent-summary-value">{totals.tasksClosed}</span>
          <span className="agent-summary-label">Tasks Closed</span>
        </div>
        <div className="agent-summary-card">
          <span className="agent-summary-value">{Math.round(totals.avgSuccess * 100)}%</span>
          <span className="agent-summary-label">Avg Success</span>
        </div>
        <div className="agent-summary-card">
          <span className="agent-summary-value">{formatCost(totals.totalCost)}</span>
          <span className="agent-summary-label">Total Cost</span>
        </div>
      </div>

      {/* Agent list */}
      {isLoading ? (
        <div className="agent-loading">Loading agent data...</div>
      ) : error ? (
        <div className="agent-error">{error}</div>
      ) : displayAgents.length === 0 ? (
        <div className="agent-empty">No agents found</div>
      ) : (
        <div className="agent-list">
          {displayAgents.map(agent => (
            <AgentCard
              key={agent.id}
              agent={agent}
              isExpanded={expandedAgent === agent.id}
              onToggle={() => setExpandedAgent(expandedAgent === agent.id ? null : agent.id)}
            />
          ))}
        </div>
      )}
    </main>
  )
}
