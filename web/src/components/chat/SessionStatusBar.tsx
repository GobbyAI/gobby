import type { SessionObservationMeta } from '../../types/chat'

const SOURCE_LABELS: Record<string, string> = {
  claude_code: 'Claude Code',
  gemini_cli: 'Gemini CLI',
  codex: 'Codex',
  windsurf: 'Windsurf',
  cursor: 'Cursor',
  copilot: 'Copilot',
}

const SOURCE_COLORS: Record<string, string> = {
  claude_code: 'bg-purple-400',
  gemini_cli: 'bg-green-400',
  codex: 'bg-blue-400',
  windsurf: 'bg-sky-400',
  cursor: 'bg-pink-400',
  copilot: 'bg-indigo-400',
}

interface SessionStatusBarProps {
  sessionRef: string | null
  title: string | null
  viewingMeta?: SessionObservationMeta | null
  isAttached?: boolean
  onAttach?: () => void
  onDetach?: () => void
}

export function SessionStatusBar({
  sessionRef,
  title,
  viewingMeta,
  isAttached,
  onAttach,
  onDetach,
}: SessionStatusBarProps) {
  const isObserving = !!viewingMeta

  // When observing a terminal session, show observation-specific right side
  if (isObserving && viewingMeta) {
    const sourceLabel = SOURCE_LABELS[viewingMeta.source] ?? viewingMeta.source
    const sourceDot = SOURCE_COLORS[viewingMeta.source] ?? 'bg-neutral-400'
    const isLive = viewingMeta.status === 'active'

    return (
      <div className="flex items-center justify-between px-4 py-1.5 bg-muted border-b border-border text-xs">
        <div className="flex items-center gap-1.5 min-w-0">
          {sessionRef && (
            <span className="font-mono text-accent shrink-0">{sessionRef}</span>
          )}
          <span className="text-muted-foreground truncate">
            {sessionRef && (viewingMeta.title ?? title) ? ': ' : ''}
            {viewingMeta.title ?? title ?? 'Terminal session'}
          </span>
        </div>
        <div className="flex items-center gap-1.5 shrink-0 whitespace-nowrap ml-4">
          <span className={`inline-block w-1.5 h-1.5 rounded-full ${sourceDot}`} />
          <span className="text-muted-foreground">{sourceLabel}</span>
          <span className="text-muted-foreground/60">&middot;</span>
          <span className="text-muted-foreground">
            {isAttached ? 'Attached' : 'Observing'}
            {isLive && !isAttached && ' (live)'}
          </span>
          {isAttached && onDetach ? (
            <button
              className="ml-1.5 px-2 py-0.5 rounded border border-border bg-background text-muted-foreground hover:text-foreground hover:bg-muted transition-colors text-[11px]"
              onClick={onDetach}
            >
              Detach
            </button>
          ) : !isAttached && onAttach ? (
            <button
              className="ml-1.5 px-2 py-0.5 rounded border border-accent/40 bg-accent/10 text-accent hover:bg-accent/20 transition-colors text-[11px]"
              onClick={onAttach}
            >
              Attach
            </button>
          ) : null}
        </div>
      </div>
    )
  }

  return (
    <div className="flex items-center justify-between px-4 py-1.5 bg-muted border-b border-border text-xs">
      <div className="flex items-center gap-1.5 min-w-0">
        {sessionRef && (
          <span className="font-mono text-accent shrink-0">{sessionRef}</span>
        )}
        <span className="text-muted-foreground truncate">
          {sessionRef && title ? ': ' : ''}{title ?? 'New conversation'}
        </span>
      </div>
    </div>
  )
}
