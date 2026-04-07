import { useMemo } from 'react'
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
} from './ui/Dialog'
import type { GobbySession } from '../../hooks/useSessions'

interface RunningAgent {
  run_id: string
  provider: string
  pid?: number
  mode?: string
  started_at?: string
  session_id?: string
}

const PROVIDER_COLORS: Record<string, string> = {
  claude: '#c084fc',
  gemini: '#4ade80',
  codex: '#3b82f6',
  unknown: '#737373',
}

const SOURCE_COLORS: Record<string, string> = {
  claude: '#c084fc',
  gemini: '#4ade80',
  codex: '#3b82f6',
  unknown: '#737373',
}

interface ActiveSessionsModalProps {
  isOpen: boolean
  onClose: () => void
  agents: RunningAgent[]
  cliSessions?: GobbySession[]
  onViewAgent: (agent: RunningAgent) => void
  onKillAgent?: (runId: string) => void
  onViewCliSession?: (session: GobbySession) => void
}

export function ActiveSessionsModal({
  isOpen,
  onClose,
  agents,
  cliSessions = [],
  onViewAgent,
  onKillAgent,
  onViewCliSession,
}: ActiveSessionsModalProps) {
  const totalCount = agents.length + cliSessions.length

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-md">
        <DialogTitle>Active Sessions ({totalCount})</DialogTitle>
        <DialogDescription className="sr-only">
          View and manage running agent and CLI sessions
        </DialogDescription>

        {totalCount === 0 && (
          <p className="text-sm text-muted-foreground py-4 text-center">
            No active sessions
          </p>
        )}

        {agents.length > 0 && (
          <div className="mt-3">
            <div className="text-xs font-medium text-muted-foreground mb-2 uppercase tracking-wider">
              Agents
            </div>
            <div className="flex flex-col gap-2">
              {agents.map((agent) => (
                <AgentCard
                  key={agent.run_id}
                  agent={agent}
                  onView={() => {
                    onViewAgent(agent)
                    onClose()
                  }}
                  onKill={onKillAgent ? () => onKillAgent(agent.run_id) : undefined}
                />
              ))}
            </div>
          </div>
        )}

        {cliSessions.length > 0 && (
          <div className="mt-4">
            <div className="text-xs font-medium text-muted-foreground mb-2 uppercase tracking-wider">
              CLI Sessions
            </div>
            <div className="flex flex-col gap-2">
              {cliSessions.map((session) => (
                <CliSessionCard
                  key={session.id}
                  session={session}
                  onView={
                    onViewCliSession
                      ? () => {
                          onViewCliSession(session)
                          onClose()
                        }
                      : undefined
                  }
                />
              ))}
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

function AgentCard({
  agent,
  onView,
  onKill,
}: {
  agent: RunningAgent
  onView: () => void
  onKill?: () => void
}) {
  const uptime = useAgentUptime(agent.started_at)
  const color = PROVIDER_COLORS[agent.provider] ?? PROVIDER_COLORS.unknown

  return (
    <div className="flex items-center gap-3 p-3 rounded-md border border-border bg-muted/30">
      <span
        className="w-2 h-2 rounded-full shrink-0"
        style={{ background: color }}
      />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-foreground">{agent.provider}</span>
        </div>
        <div className="text-xs text-muted-foreground mt-0.5">
          Started {uptime}
        </div>
      </div>
      <div className="flex items-center gap-1.5 shrink-0">
        <button
          className="px-2.5 py-1 rounded text-xs border border-border bg-background text-foreground hover:bg-muted transition-colors"
          onClick={onView}
        >
          View
        </button>
        {onKill && (
          <button
            className="px-2.5 py-1 rounded text-xs border border-red-500/30 bg-red-500/10 text-red-400 hover:bg-red-500/20 transition-colors"
            onClick={onKill}
          >
            Kill
          </button>
        )}
      </div>
    </div>
  )
}

function CliSessionCard({
  session,
  onView,
}: {
  session: GobbySession
  onView?: () => void
}) {
  const seqLabel = session.seq_num != null ? `#${session.seq_num}` : null
  const titleText = session.title || session.ref || 'CLI Session'
  const displayTitle = seqLabel ? `${seqLabel}: ${titleText}` : titleText
  const color = SOURCE_COLORS[session.source] ?? SOURCE_COLORS.unknown

  return (
    <div className="flex items-center gap-3 p-3 rounded-md border border-border bg-muted/30">
      <span
        className="w-2 h-2 rounded-full shrink-0"
        style={{ background: color }}
      />
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium text-foreground truncate">{displayTitle}</div>
        <div className="text-xs text-muted-foreground mt-0.5">{session.status}</div>
      </div>
      {onView && (
        <button
          className="px-2.5 py-1 rounded text-xs border border-border bg-background text-foreground hover:bg-muted transition-colors shrink-0"
          onClick={onView}
        >
          View
        </button>
      )}
    </div>
  )
}

function useAgentUptime(startedAt?: string) {
  const startTime = useMemo(() => {
    if (startedAt) {
      const t = new Date(startedAt).getTime()
      if (!Number.isNaN(t)) return t
    }
    return null
  }, [startedAt])

  if (startTime === null) return '\u2014'
  const elapsed = Math.floor((Date.now() - startTime) / 1000)
  if (elapsed < 60) return `${elapsed}s ago`
  if (elapsed < 3600) return `${Math.floor(elapsed / 60)}m ago`
  return `${Math.floor(elapsed / 3600)}h${Math.floor((elapsed % 3600) / 60)}m ago`
}
