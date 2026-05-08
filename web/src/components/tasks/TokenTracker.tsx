import { useState, useEffect } from 'react'
import { cn } from '../../lib/utils'

interface SessionUsage {
  sessionId: string
  inputTokens: number
  outputTokens: number
}

const ROOT_CLS = 'flex flex-col gap-1.5'
const STATE_TEXT_CLS = 'text-xs text-muted-foreground'

const TOTAL_CLS = 'flex items-baseline gap-1.5'
const TOTAL_VALUE_CLS = 'text-lg font-bold text-foreground'
const TOTAL_LABEL_CLS = 'text-2xs text-muted-foreground'

const BAR_CONTAINER_CLS = 'py-0.5'
const BAR_CLS = 'flex h-1.5 overflow-hidden rounded bg-muted'
const BAR_INPUT_CLS = 'rounded-l bg-accent'
const BAR_OUTPUT_CLS = 'rounded-r bg-agent'

const STATS_CLS = 'flex gap-3'
const STAT_CLS = 'flex items-center gap-1'
const STAT_DOT_CLS = 'h-1.5 w-1.5 shrink-0 rounded-full'
const STAT_DOT_INPUT_CLS = 'bg-accent'
const STAT_DOT_OUTPUT_CLS = 'bg-agent'
const STAT_LABEL_CLS = 'text-2xs text-muted-foreground'
const STAT_VALUE_CLS = 'text-xs font-semibold text-muted-foreground'

function getBaseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL || ''
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(n)
}

interface TokenTrackerProps {
  sessionId: string | null
}

export function TokenTracker({ sessionId }: TokenTrackerProps) {
  const [usage, setUsage] = useState<SessionUsage | null>(null)
  const [isLoading, setIsLoading] = useState(false)

  useEffect(() => {
    if (!sessionId) return
    const sid = sessionId
    const controller = new AbortController()
    let cancelled = false

    async function fetchUsage() {
      setIsLoading(true)
      try {
        const baseUrl = getBaseUrl()
        const response = await fetch(
          `${baseUrl}/api/sessions/${encodeURIComponent(sid)}`,
          { signal: controller.signal }
        )
        if (!response.ok) {
          console.warn(`Session usage fetch returned ${response.status}`)
          if (!cancelled) setUsage(null)
        } else {
          const data = await response.json()
          const session = data.session
          if (session && !cancelled) {
            setUsage({
              sessionId: session.id || sid,
              inputTokens: session.usage_input_tokens || 0,
              outputTokens: session.usage_output_tokens || 0,
            })
          }
        }
      } catch (e) {
        if (!cancelled) {
          console.error('Failed to fetch session usage:', e)
          setUsage(null)
        }
      }
      if (!cancelled) setIsLoading(false)
    }

    fetchUsage()
    return () => { cancelled = true; controller.abort() }
  }, [sessionId])

  if (!sessionId) return null
  if (isLoading) return <div className={STATE_TEXT_CLS}>Loading usage...</div>
  if (!usage) return <div className={STATE_TEXT_CLS}>No usage data</div>

  const totalTokens = usage.inputTokens + usage.outputTokens
  const inputPct = totalTokens > 0 ? (usage.inputTokens / totalTokens) * 100 : 50

  return (
    <div className={ROOT_CLS}>
      <div className={TOTAL_CLS}>
        <span className={TOTAL_VALUE_CLS}>{formatTokens(totalTokens)}</span>
        <span className={TOTAL_LABEL_CLS}>tokens used</span>
      </div>

      <div className={BAR_CONTAINER_CLS}>
        <div className={BAR_CLS}>
          <div
            className={BAR_INPUT_CLS}
            style={{ width: `${inputPct}%` }}
            title={`Input: ${formatTokens(usage.inputTokens)}`}
          />
          <div
            className={BAR_OUTPUT_CLS}
            style={{ width: `${100 - inputPct}%` }}
            title={`Output: ${formatTokens(usage.outputTokens)}`}
          />
        </div>
      </div>

      <div className={STATS_CLS}>
        <div className={STAT_CLS}>
          <span className={cn(STAT_DOT_CLS, STAT_DOT_INPUT_CLS)} />
          <span className={STAT_LABEL_CLS}>Input</span>
          <span className={STAT_VALUE_CLS}>{formatTokens(usage.inputTokens)}</span>
        </div>
        <div className={STAT_CLS}>
          <span className={cn(STAT_DOT_CLS, STAT_DOT_OUTPUT_CLS)} />
          <span className={STAT_LABEL_CLS}>Output</span>
          <span className={STAT_VALUE_CLS}>{formatTokens(usage.outputTokens)}</span>
        </div>
        <div className={STAT_CLS}>
          <span className={STAT_LABEL_CLS}>Total</span>
          <span className={STAT_VALUE_CLS}>{formatTokens(totalTokens)}</span>
        </div>
      </div>
    </div>
  )
}
