export type RiskLevel = 'critical' | 'high' | 'medium' | 'low' | 'none'

interface RiskDef {
  level: RiskLevel
  label: string
  color: string
  bg: string
}

export const RISK_DEFS: Record<RiskLevel, RiskDef> = {
  critical: { level: 'critical', label: 'Critical', color: '#dc2626', bg: 'rgba(220, 38, 38, 0.1)' },
  high: { level: 'high', label: 'High', color: '#ef4444', bg: 'rgba(239, 68, 68, 0.1)' },
  medium: { level: 'medium', label: 'Medium', color: '#f59e0b', bg: 'rgba(245, 158, 11, 0.1)' },
  low: { level: 'low', label: 'Low', color: '#737373', bg: 'rgba(115, 115, 115, 0.08)' },
  none: { level: 'none', label: '', color: 'transparent', bg: 'transparent' },
}

const CRITICAL_PATTERNS = [
  /deploy/i, /push.*force/i, /reset.*hard/i, /drop.*table/i,
  /destroy/i, /rm\s+-rf/i, /force.?push/i,
]

const HIGH_PATTERNS = [
  /delete/i, /remove/i, /^bash$/i, /run_command/i,
  /git.*push/i, /migrate/i, /alter.*table/i,
]

const MEDIUM_PATTERNS = [
  /write/i, /edit/i, /create/i, /update/i, /insert/i,
  /patch/i, /put/i, /post/i, /webhook/i,
  /fetch/i, /http/i, /api.*call/i, /send/i,
]

const TASK_CRITICAL_PATTERNS = [/deploy/i, /production/i, /migration/i, /drop/i]
const TASK_HIGH_PATTERNS = [/delete/i, /remove.*data/i, /payment/i, /billing/i, /auth/i, /security/i, /credential/i]
const TASK_MEDIUM_PATTERNS = [/api/i, /external/i, /webhook/i, /integration/i, /database/i]

export function classifyRisk(toolName: string, toolInput?: string | null): RiskLevel {
  const combined = toolInput ? `${toolName} ${toolInput}` : toolName

  for (const pattern of CRITICAL_PATTERNS) {
    if (pattern.test(combined)) return 'critical'
  }
  for (const pattern of HIGH_PATTERNS) {
    if (pattern.test(combined)) return 'high'
  }
  for (const pattern of MEDIUM_PATTERNS) {
    if (pattern.test(combined)) return 'medium'
  }
  return 'none'
}

export function classifyTaskRisk(title: string, taskType?: string): RiskLevel {
  const text = `${title} ${taskType || ''}`
  for (const pattern of TASK_CRITICAL_PATTERNS) {
    if (pattern.test(text)) return 'critical'
  }
  for (const pattern of TASK_HIGH_PATTERNS) {
    if (pattern.test(text)) return 'high'
  }
  for (const pattern of TASK_MEDIUM_PATTERNS) {
    if (pattern.test(text)) return 'medium'
  }
  return 'none'
}

export function highestRisk(toolNames: string[]): RiskLevel {
  const levels: RiskLevel[] = ['critical', 'high', 'medium', 'low', 'none']
  let highest = 4
  for (const name of toolNames) {
    const risk = classifyRisk(name)
    const idx = levels.indexOf(risk)
    if (idx < highest) highest = idx
  }
  return levels[highest]
}
