import { useState, useEffect } from 'react'

// =============================================================================
// Types
// =============================================================================

interface SessionUsage {
  sessionId: string
  inputTokens: number
  outputTokens: number
  totalCostUsd: number
}

// =============================================================================
// Helpers
// =============================================================================

function getBaseUrl(): string {
  return ''
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`
  return String(n)
}

function formatCost(usd: number): string {
  if (usd < 0.01) return `$${usd.toFixed(4)}`
  if (usd < 1) return `$${usd.toFixed(3)}`
  return `$${usd.toFixed(2)}`
}

// =============================================================================
// CostTracker
// =============================================================================

interface CostTrackerProps {
  sessionId: string | null
}

export function CostTracker({ sessionId }: CostTrackerProps) {
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
        } else {
          const data = await response.json()
          const session = data.session
          if (session && !cancelled) {
            setUsage({
              sessionId: session.id || sid,
              inputTokens: session.usage_input_tokens || 0,
              outputTokens: session.usage_output_tokens || 0,
              totalCostUsd: session.usage_total_cost_usd || 0,
            })
          }
        }
      } catch (e) {
        if (!cancelled) console.error('Failed to fetch session usage:', e)
      }
      if (!cancelled) setIsLoading(false)
    }

    fetchUsage()
    return () => { cancelled = true; controller.abort() }
  }, [sessionId])

  if (!sessionId) return null
  if (isLoading) return <div className="cost-tracker-loading">Loading usage...</div>
  if (!usage) return <div className="cost-tracker-empty">No usage data</div>

  const totalTokens = usage.inputTokens + usage.outputTokens
  const inputPct = totalTokens > 0 ? (usage.inputTokens / totalTokens) * 100 : 50

  return (
    <div className="cost-tracker">
      {/* Total cost */}
      <div className="cost-tracker-total">
        <span className="cost-tracker-cost">{formatCost(usage.totalCostUsd)}</span>
        <span className="cost-tracker-total-label">estimated cost</span>
      </div>

      {/* Token breakdown bar */}
      <div className="cost-tracker-bar-container">
        <div className="cost-tracker-bar">
          <div
            className="cost-tracker-bar-input"
            style={{ width: `${inputPct}%` }}
            title={`Input: ${formatTokens(usage.inputTokens)}`}
          />
          <div
            className="cost-tracker-bar-output"
            style={{ width: `${100 - inputPct}%` }}
            title={`Output: ${formatTokens(usage.outputTokens)}`}
          />
        </div>
      </div>

      {/* Token stats */}
      <div className="cost-tracker-stats">
        <div className="cost-tracker-stat">
          <span className="cost-tracker-stat-dot cost-tracker-stat-dot--input" />
          <span className="cost-tracker-stat-label">Input</span>
          <span className="cost-tracker-stat-value">{formatTokens(usage.inputTokens)}</span>
        </div>
        <div className="cost-tracker-stat">
          <span className="cost-tracker-stat-dot cost-tracker-stat-dot--output" />
          <span className="cost-tracker-stat-label">Output</span>
          <span className="cost-tracker-stat-value">{formatTokens(usage.outputTokens)}</span>
        </div>
        <div className="cost-tracker-stat">
          <span className="cost-tracker-stat-label">Total</span>
          <span className="cost-tracker-stat-value">{formatTokens(totalTokens)}</span>
        </div>
      </div>
    </div>
  )
}
