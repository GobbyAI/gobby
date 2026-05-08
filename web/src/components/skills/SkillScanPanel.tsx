import type { ScanResult } from '../../hooks/useSkills'
import { cn } from '../../lib/utils'

const PANEL_CLS = 'mx-5 my-2 overflow-hidden rounded-md border border-[var(--border)]'
const HEADER_CLS = 'flex items-center gap-2 border-b border-[var(--border)] bg-[var(--bg-secondary)] px-3 py-2'

const BADGE_CLS = 'rounded px-2 py-0.5 text-[length:var(--text-sm)] font-semibold uppercase'
const BADGE_SAFE_CLS =
  'bg-[color-mix(in_srgb,var(--color-success-foreground)_15%,transparent)] text-[var(--color-success-foreground)]'
const BADGE_UNSAFE_CLS =
  'bg-[color-mix(in_srgb,var(--color-error)_15%,transparent)] text-[var(--color-error)]'
const META_CLS = 'text-[length:var(--text-sm)] text-[var(--text-muted)]'

const FINDINGS_CLS = 'flex flex-col'
const FINDING_CLS = 'border-b border-[var(--border)] px-3 py-2 last:border-b-0'
const FINDING_HEADER_CLS = 'mb-1 flex items-center gap-1.5'
const SEVERITY_CLS = 'rounded-sm px-1.5 py-px text-[length:var(--text-2xs)] font-semibold uppercase'

const SEVERITY_BG: Record<string, string> = {
  CRITICAL: 'bg-[color-mix(in_srgb,var(--color-error)_15%,transparent)] text-[var(--color-error)]',
  HIGH: 'bg-[color-mix(in_srgb,var(--color-warning-foreground)_15%,transparent)] text-[var(--color-warning-foreground)]',
  MEDIUM: 'bg-[color-mix(in_srgb,var(--color-warning-foreground)_10%,transparent)] text-[var(--color-warning-foreground)]',
  LOW: 'bg-[color-mix(in_srgb,var(--color-success-foreground)_15%,transparent)] text-[var(--color-success-foreground)]',
  INFO: 'bg-[color-mix(in_srgb,var(--text-muted)_10%,transparent)] text-[var(--text-muted)]',
}

const FINDING_TITLE_CLS = 'text-[length:var(--text-base)] font-medium text-[var(--text-primary)]'
const FINDING_DESC_CLS = 'my-0.5 text-[length:var(--text-sm)] text-[var(--text-muted)]'
const FINDING_REMEDIATION_CLS = 'my-0.5 text-[length:var(--text-sm)] text-[var(--text-secondary)]'

interface SkillScanPanelProps {
  result: ScanResult
}

function severityKey(severity: string): string {
  const upper = severity.toUpperCase()
  return SEVERITY_BG[upper] ? upper : 'INFO'
}

export function SkillScanPanel({ result }: SkillScanPanelProps) {
  return (
    <div className={PANEL_CLS}>
      <div className={HEADER_CLS}>
        <span className={cn(BADGE_CLS, result.is_safe ? BADGE_SAFE_CLS : BADGE_UNSAFE_CLS)}>
          {result.is_safe ? 'SAFE' : 'UNSAFE'}
        </span>
        <span className={META_CLS}>
          {result.findings_count} finding{result.findings_count !== 1 ? 's' : ''} &middot; {result.scan_duration_seconds}s
        </span>
      </div>

      {result.findings.length > 0 && (
        <div className={FINDINGS_CLS}>
          {result.findings.map((f, i) => (
            <div key={i} className={FINDING_CLS}>
              <div className={FINDING_HEADER_CLS}>
                <span className={cn(SEVERITY_CLS, SEVERITY_BG[severityKey(f.severity)])}>
                  {f.severity}
                </span>
                <span className={FINDING_TITLE_CLS}>{f.title}</span>
              </div>
              {f.description && <p className={FINDING_DESC_CLS}>{f.description}</p>}
              {f.remediation && (
                <p className={FINDING_REMEDIATION_CLS}>
                  <strong>Fix:</strong> {f.remediation}
                </p>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
