import { useState } from 'react'
import type { AdminStatus } from '../../hooks/useDashboard'
import { Badge } from '../chat/ui/Badge'
import { DashboardCard } from './DashboardCard'
import {
  dashboardHealthDotClass,
  dashboardHealthGridClass,
  dashboardHealthHeaderClass,
  dashboardHealthNameClass,
  dashboardHealthRowClass,
  dashboardToggleButtonClass,
  dashboardTransportBadgeVariant,
} from './dashboardStyles'

interface Props {
  mcpServers: AdminStatus['mcp_servers']
}

const VALID_HEALTH = new Set(['healthy', 'degraded', 'unhealthy'])

function healthClass(health: string | null): string {
  return (health && VALID_HEALTH.has(health)) ? health : 'unknown'
}

export function McpHealthCard({ mcpServers }: Props) {
  const entries = Object.entries(mcpServers ?? {})
  const connected = entries.filter(([, v]) => v.connected).length
  const unhealthy = entries.filter(([, v]) => v.health !== 'healthy' && v.health !== null)
  const healthy = entries.filter(([, v]) => v.health === 'healthy' || v.health === null)
  const allHealthy = unhealthy.length === 0
  const [expanded, setExpanded] = useState(false)

  return (
    <DashboardCard
      title="MCP Servers"
      action={
        allHealthy ? (
          <Badge variant="success">all connected</Badge>
        ) : undefined
      }
    >
      <div className={dashboardHealthHeaderClass}>
          <span className={`size-2 shrink-0 rounded-full ${dashboardHealthDotClass(allHealthy ? 'healthy' : 'degraded')}`} />
          {' '}{connected}/{entries.length} connected
      </div>

      {/* Always show unhealthy servers */}
      {unhealthy.length > 0 && (
        <div className={dashboardHealthGridClass}>
          {unhealthy.map(([name, server]) => (
            <div key={name} className={dashboardHealthRowClass}>
              <span className={`size-2 shrink-0 rounded-full ${dashboardHealthDotClass(healthClass(server.health))}`} />
              <span className={dashboardHealthNameClass}>{name}</span>
              <Badge variant={dashboardTransportBadgeVariant(server.transport)} className="px-2 py-0 text-[10px] uppercase tracking-[0.03em]">
                  {server.transport}
              </Badge>
            </div>
          ))}
        </div>
      )}

      {/* Collapsible healthy servers */}
      {healthy.length > 0 && (
        <>
          <button
            className={dashboardToggleButtonClass}
            onClick={() => setExpanded(!expanded)}
          >
            {expanded ? 'Hide' : 'Show'} {healthy.length} healthy server{healthy.length !== 1 ? 's' : ''}
          </button>
          {expanded && (
            <div className={dashboardHealthGridClass}>
              {healthy.map(([name, server]) => (
                <div key={name} className={dashboardHealthRowClass}>
                  <span className={`size-2 shrink-0 rounded-full ${dashboardHealthDotClass(healthClass(server.health))}`} />
                  <span className={dashboardHealthNameClass}>{name}</span>
                  <Badge variant={dashboardTransportBadgeVariant(server.transport)} className="px-2 py-0 text-[10px] uppercase tracking-[0.03em]">
                      {server.transport}
                  </Badge>
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </DashboardCard>
  )
}
