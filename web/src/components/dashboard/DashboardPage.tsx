import { useDashboard } from '../../hooks/useDashboard'
import { SystemHealthCard } from './SystemHealthCard'
import { TasksCard } from './TasksCard'
import { SessionsCard } from './SessionsCard'
import { PipelinesCard } from './PipelinesCard'
import { McpHealthCard } from './McpHealthCard'
import { MemorySkillsCard } from './MemorySkillsCard'
import './DashboardPage.css'

function formatTime(date: Date | null): string {
  if (!date) return ''
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export function DashboardPage() {
  const { data, isLoading, error, lastUpdated } = useDashboard()

  return (
    <main className="dash-page">
      <div className="dash-toolbar">
        <div className="dash-toolbar-left">
          <h2 className="dash-toolbar-title">Dashboard</h2>
        </div>
        <div className="dash-toolbar-right">
          {lastUpdated && (
            <span className="dash-toolbar-updated">
              Updated {formatTime(lastUpdated)}
            </span>
          )}
        </div>
      </div>

      <div className="dash-content">
        {isLoading && !data ? (
          <div className="dash-loading">Loading dashboard...</div>
        ) : error && !data ? (
          <div className="dash-error">Failed to load: {error.slice(0, 200)}</div>
        ) : data ? (
          <div className="dash-grid">
            <SystemHealthCard data={data} />
            <TasksCard tasks={data.tasks} />
            <PipelinesCard pipelines={data.pipelines} />
            <SessionsCard sessions={data.sessions} />
            <McpHealthCard mcpServers={data.mcp_servers} />
            <MemorySkillsCard memory={data.memory} skills={data.skills} />
          </div>
        ) : null}
      </div>
    </main>
  )
}
