import { RISK_DEFS, type RiskLevel } from './riskUtils'

// =============================================================================
// RiskBadge component
// =============================================================================

interface RiskBadgeProps {
  level: RiskLevel
  compact?: boolean
}

export function RiskBadge({ level, compact }: RiskBadgeProps) {
  if (level === 'none' || level === 'low') return null

  const def = RISK_DEFS[level]

  return (
    <span
      className={`risk-badge ${compact ? 'risk-badge--compact' : ''}`}
      style={{ color: def.color, background: def.bg, borderColor: def.color }}
      title={`${def.label} risk`}
    >
      <svg className="risk-badge-icon" width="10" height="10" viewBox="0 0 24 24" fill="currentColor">
        <path d="M12 2L3 20h18L12 2zm0 4l6.9 12H5.1L12 6zm-1 5v4h2v-4h-2zm0 5v2h2v-2h-2z" />
      </svg>
      {!compact && <span className="risk-badge-label">{def.label}</span>}
    </span>
  )
}

/** Inline risk dot for action feed items. */
export function RiskDot({ level }: { level: RiskLevel }) {
  if (level === 'none' || level === 'low') return null
  const def = RISK_DEFS[level]
  return (
    <span
      className="risk-dot"
      style={{ background: def.color }}
      title={`${def.label} risk`}
    />
  )
}
