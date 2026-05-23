import type { AdminStatus } from '../../hooks/useDashboard'
import { Badge } from '../chat/ui/Badge'
import { DashboardCard } from './DashboardCard'
import {
  dashboardHealthBadgeVariant,
  dashboardHealthDotClass,
  dashboardServicesClass,
  dashboardServiceRowClass,
  dashboardStatClass,
  dashboardStatGridClass,
  dashboardStatLabelClass,
  dashboardStatValueClass,
} from './dashboardStyles'

function formatUptime(seconds: number | null): string {
  if (seconds == null) return '—'
  const d = Math.floor(seconds / 86400)
  const h = Math.floor((seconds % 86400) / 3600)
  const m = Math.round((seconds % 3600) / 60)
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${m}m`
  if (m > 0) return `${m}m`
  return `${Math.round(seconds)}s`
}

interface Props {
  data: AdminStatus
}

export function SystemHealthCard({ data }: Props) {
  const { server, process, background_tasks, status, memory, mcp_servers } = data
  const falkordb = memory?.falkordb
  const qdrant = memory?.qdrant

  // External MCP servers summary
  const externalMcps = Object.entries(mcp_servers ?? {}).filter(([, info]) => !info.internal)
  const externalHealthy = externalMcps.filter(([, info]) => info.health === 'healthy' || info.connected).length
  const externalTotal = externalMcps.length

  return (
    <DashboardCard
      title="System Health"
      action={
        <Badge variant={dashboardHealthBadgeVariant(status)} className="capitalize">
          {status}
        </Badge>
      }
    >
      <div className={dashboardStatGridClass}>
        <div className={dashboardStatClass}>
          <span className={dashboardStatValueClass}>{formatUptime(server.uptime_seconds)}</span>
          <span className={dashboardStatLabelClass}>Uptime</span>
        </div>
        <div className={dashboardStatClass}>
          <span className={dashboardStatValueClass}>
            {process ? `${process.memory_rss_mb}` : '—'}
          </span>
          <span className={dashboardStatLabelClass}>Memory (MB)</span>
        </div>
        <div className={dashboardStatClass}>
          <span className={dashboardStatValueClass}>
            {process ? `${process.cpu_percent}%` : '—'}
          </span>
          <span className={dashboardStatLabelClass}>CPU</span>
        </div>
        {background_tasks.active > 0 && (
          <div className={dashboardStatClass}>
            <span className={dashboardStatValueClass}>{background_tasks.active}</span>
            <span className={dashboardStatLabelClass}>Background Tasks</span>
          </div>
        )}
      </div>

      <div className={dashboardServicesClass}>
        {[
          qdrant && {
            id: 'qdrant',
            label: `Qdrant ${qdrant.healthy ? 'connected' : qdrant.configured ? 'disconnected' : 'not configured'}`,
            status: qdrant.healthy ? 'healthy' : qdrant.configured ? 'unhealthy' : 'unknown',
          },
          falkordb && {
            id: 'falkordb',
            label: `FalkorDB ${falkordb.healthy ? 'connected' : falkordb.configured ? 'disconnected' : 'not configured'}`,
            status: falkordb.healthy ? 'healthy' : falkordb.configured ? 'unhealthy' : 'unknown',
          },
          externalTotal > 0 && {
            id: 'external-mcps',
            label: `External MCPs ${externalHealthy}/${externalTotal} connected`,
            status: externalHealthy === externalTotal ? 'healthy' : externalHealthy > 0 ? 'degraded' : 'unhealthy',
          },
        ]
          .filter((s): s is { id: string; label: string; status: string } => !!s)
          .sort((a, b) => a.label.localeCompare(b.label))
          .map(s => (
            <div key={s.id} className={dashboardServiceRowClass}>
              <span className={`size-2 shrink-0 rounded-full ${dashboardHealthDotClass(s.status)}`} />
              <span>{s.label}</span>
            </div>
          ))}
      </div>
    </DashboardCard>
  )
}
