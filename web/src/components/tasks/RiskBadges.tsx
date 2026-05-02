import { RISK_DEFS, type RiskLevel } from './riskUtils'

const BADGE_CLS =
  'inline-flex items-center gap-[0.2rem] rounded-[0.2rem] border px-[0.35rem] py-[0.1rem] font-[inherit] text-[length:calc(var(--font-size-base)*0.65)] font-semibold leading-none'
const BADGE_COMPACT_CLS = 'px-[0.2rem] py-[0.05rem]'
const ICON_CLS = 'shrink-0'
const LABEL_CLS = 'whitespace-nowrap'
const DOT_CLS = 'h-1.5 w-1.5 shrink-0 rounded-full'

interface RiskBadgeProps {
  level: RiskLevel
  compact?: boolean
}

export function RiskBadge({ level, compact }: RiskBadgeProps) {
  if (level === 'none' || level === 'low') return null

  const def = RISK_DEFS[level]

  return (
    <span
      className={compact ? `${BADGE_CLS} ${BADGE_COMPACT_CLS}` : BADGE_CLS}
      style={{ color: def.color, background: def.bg, borderColor: def.color }}
      title={`${def.label} risk`}
    >
      <svg className={ICON_CLS} width="10" height="10" viewBox="0 0 24 24" fill="currentColor">
        <path d="M12 2L3 20h18L12 2zm0 4l6.9 12H5.1L12 6zm-1 5v4h2v-4h-2zm0 5v2h2v-2h-2z" />
      </svg>
      {!compact && <span className={LABEL_CLS}>{def.label}</span>}
    </span>
  )
}

export function RiskDot({ level }: { level: RiskLevel }) {
  if (level === 'none' || level === 'low') return null
  const def = RISK_DEFS[level]
  return (
    <span
      className={DOT_CLS}
      style={{ background: def.color }}
      title={`${def.label} risk`}
    />
  )
}
