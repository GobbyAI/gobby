import { useState, useCallback } from 'react'
import { cn } from '../../lib/utils'

type OversightMode = 'hands_off' | 'ask_risky' | 'ask_each'

interface OversightOption {
  value: OversightMode
  label: string
  description: string
  icon: string
}

const OPTIONS: OversightOption[] = [
  { value: 'hands_off', label: 'Hands-off', description: 'Full autonomy - agent works independently', icon: '⚡' },
  { value: 'ask_risky', label: 'Ask before risky', description: 'Pause on destructive or high-risk actions', icon: '⚠' },
  { value: 'ask_each', label: 'Ask each step', description: 'Require approval before every action', icon: '✅' },
]

const ROOT_CLS = 'flex flex-col gap-[0.35rem]'
const HEADER_CLS = 'flex items-center gap-[0.4rem]'
const LABEL_CLS =
  'font-[inherit] text-[length:calc(var(--font-size-base)*0.7)] font-semibold uppercase tracking-[0.04em] text-[var(--text-secondary)]'
const INFO_BTN_CLS =
  'flex h-4 w-4 cursor-pointer items-center justify-center rounded-full border border-[var(--border)] bg-transparent text-[length:calc(var(--font-size-base)*0.6)] text-[var(--text-muted)] transition-colors duration-150 hover:border-[var(--text-muted)] hover:text-[var(--text-secondary)]'
const HELP_CLS = 'py-1 text-[length:calc(var(--font-size-base)*0.7)] leading-[1.4] text-[var(--text-muted)]'
const OPTIONS_CLS = 'flex gap-1'
const OPTION_CLS =
  'flex flex-1 cursor-pointer items-center justify-center gap-[0.3rem] rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] px-[0.4rem] py-[0.35rem] text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-muted)] transition-colors duration-150 hover:border-[var(--text-muted)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-secondary)] pointer-coarse:min-h-11'
const OPTION_ACTIVE_CLS =
  'border-[var(--accent)] bg-[color-mix(in_srgb,var(--color-info)_10%,transparent)] text-[var(--accent)] hover:border-[var(--accent-hover)] hover:bg-[color-mix(in_srgb,var(--color-info)_15%,transparent)] hover:text-[var(--accent-hover)]'
const OPTION_ICON_CLS = 'text-[length:calc(var(--font-size-base)*0.8)]'
const OPTION_LABEL_CLS = 'whitespace-nowrap font-[inherit] text-[length:calc(var(--font-size-base)*0.65)] font-medium'
const DESC_CLS = 'italic text-[length:calc(var(--font-size-base)*0.65)] text-[var(--text-muted)]'

const STORAGE_PREFIX = 'gobby-oversight-'

function getStoredMode(taskId: string): OversightMode {
  try {
    const stored = localStorage.getItem(`${STORAGE_PREFIX}${taskId}`)
    if (stored && OPTIONS.some(o => o.value === stored)) return stored as OversightMode
  } catch {
    // localStorage unavailable
  }
  return 'ask_risky'
}

function storeMode(taskId: string, mode: OversightMode) {
  try {
    localStorage.setItem(`${STORAGE_PREFIX}${taskId}`, mode)
  } catch {
    // localStorage unavailable
  }
}

interface OversightSelectorProps {
  taskId: string
}

export function OversightSelector({ taskId }: OversightSelectorProps) {
  const [mode, setMode] = useState<OversightMode>(() => getStoredMode(taskId))
  const [showDetails, setShowDetails] = useState(false)

  const handleChange = useCallback((newMode: OversightMode) => {
    setMode(newMode)
    storeMode(taskId, newMode)
  }, [taskId])

  const current = OPTIONS.find(o => o.value === mode)!

  return (
    <div className={ROOT_CLS}>
      <div className={HEADER_CLS}>
        <span className={LABEL_CLS}>Oversight</span>
        <button
          className={INFO_BTN_CLS}
          onClick={() => setShowDetails(!showDetails)}
          title="What is oversight mode?"
        >
          ?
        </button>
      </div>

      {showDetails && (
        <div className={HELP_CLS}>
          Controls how much autonomy the agent has when working on this task.
        </div>
      )}

      <div className={OPTIONS_CLS}>
        {OPTIONS.map(opt => (
          <button
            key={opt.value}
            className={cn(OPTION_CLS, mode === opt.value && OPTION_ACTIVE_CLS)}
            onClick={() => handleChange(opt.value)}
            aria-pressed={mode === opt.value}
            title={opt.description}
          >
            <span className={OPTION_ICON_CLS}>{opt.icon}</span>
            <span className={OPTION_LABEL_CLS}>{opt.label}</span>
          </button>
        ))}
      </div>

      <div className={DESC_CLS}>{current.description}</div>
    </div>
  )
}
