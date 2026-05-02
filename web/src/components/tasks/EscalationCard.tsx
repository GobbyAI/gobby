import { useState, useCallback } from 'react'
import type { GobbyTaskDetail } from '../../hooks/useTasks'
import { cn } from '../../lib/utils'

function getBaseUrl(): string {
  return ''
}

interface ParsedOption {
  label: string
  description: string
  pros: string[]
  cons: string[]
  confidence?: number
}

interface ParsedEscalation {
  question: string
  options: ParsedOption[]
  context: string | null
}

const CARD_CLS =
  'my-2 flex flex-col gap-2 rounded-lg border border-[color-mix(in_srgb,var(--color-warning-foreground)_30%,transparent)] bg-[color-mix(in_srgb,var(--color-warning-foreground)_6%,transparent)] p-[0.8rem]'
const HEADER_CLS = 'flex items-center gap-[0.4rem]'
const ICON_CLS = 'text-[length:calc(var(--font-size-base)*1.1)]'
const TITLE_CLS = 'flex-1 text-[length:calc(var(--font-size-base)*0.8)] font-semibold text-[var(--color-warning-foreground)]'
const TIME_CLS = 'font-[inherit] text-[length:calc(var(--font-size-base)*0.6)] text-[var(--text-muted)]'
const QUESTION_CLS = 'text-[length:calc(var(--font-size-base)*0.8)] font-medium leading-[1.5] text-[var(--text-primary)]'
const CONTEXT_CLS =
  'rounded bg-[var(--bg-secondary)] px-2 py-[0.3rem] text-[length:calc(var(--font-size-base)*0.7)] leading-[1.4] text-[var(--text-secondary)]'

const OPTIONS_CLS = 'flex flex-col gap-1'
const OPTION_CLS =
  'flex cursor-pointer flex-col gap-[0.2rem] rounded-md border border-[var(--border)] bg-[var(--bg-secondary)] p-2 text-left text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-primary)] transition-colors duration-150 hover:border-[var(--text-muted)] hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11'
const OPTION_SELECTED_CLS = 'border-[var(--accent)] bg-[color-mix(in_srgb,var(--color-info)_8%,transparent)]'
const OPTION_HEADER_CLS = 'flex items-center gap-[0.4rem]'
const OPTION_RADIO_CLS = 'text-[length:calc(var(--font-size-base)*0.8)] text-[var(--text-muted)]'
const OPTION_RADIO_SELECTED_CLS = 'text-[var(--accent)]'
const OPTION_LABEL_CLS = 'flex-1 font-semibold'
const OPTION_DESC_CLS = 'pl-[1.4rem] text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-secondary)]'
const OPTION_TRADEOFFS_CLS = 'flex flex-col gap-0.5 pl-[1.4rem] text-[length:calc(var(--font-size-base)*0.65)]'
const PRO_CLS = 'block text-[var(--color-success-foreground)]'
const CON_CLS = 'block text-[var(--color-error)]'

const CONFIDENCE_CLS = 'ml-auto flex items-center gap-[0.3rem]'
const CONFIDENCE_BAR_CLS = 'h-1 w-12 overflow-hidden rounded-[2px] bg-[var(--border)]'
const CONFIDENCE_FILL_CLS = 'h-full rounded-[2px]'
const CONFIDENCE_LABEL_CLS = 'font-[inherit] text-[length:calc(var(--font-size-base)*0.55)] text-[var(--text-muted)]'

const CUSTOM_TOGGLE_CLS =
  'cursor-pointer border-0 bg-transparent px-0 py-[0.2rem] text-left font-[inherit] text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-muted)] transition-colors duration-150 hover:text-[var(--text-secondary)]'
const CUSTOM_TOGGLE_ACTIVE_CLS = 'text-[var(--text-secondary)]'
const CUSTOM_INPUT_CLS =
  'resize-y rounded border border-[var(--border)] bg-[var(--bg-secondary)] px-2 py-[0.4rem] font-[inherit] text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-primary)] outline-none focus:border-[var(--accent)]'
const ACTIONS_CLS = 'flex justify-end pt-[0.2rem]'
const SUBMIT_CLS =
  'flex-1 cursor-pointer rounded-md border border-[var(--accent)] bg-[var(--accent)] px-3 py-1.5 font-[inherit] text-[length:calc(var(--font-size-base)*0.8)] font-medium text-[var(--accent-foreground)] transition-colors duration-150 hover:bg-[var(--accent-hover)] disabled:cursor-not-allowed disabled:opacity-50 pointer-coarse:min-h-11'

function parseEscalation(reason: string | null): ParsedEscalation {
  if (!reason) {
    return { question: 'Agent needs your input', options: [], context: null }
  }

  try {
    const data = JSON.parse(reason)
    if (data.question && Array.isArray(data.options)) {
      return {
        question: data.question,
        options: data.options.map((o: Record<string, unknown>) => ({
          label: String(o.label || o.name || 'Option'),
          description: String(o.description || ''),
          pros: Array.isArray(o.pros) ? o.pros.map(String) : [],
          cons: Array.isArray(o.cons) ? o.cons.map(String) : [],
          confidence: typeof o.confidence === 'number' ? o.confidence : undefined,
        })),
        context: data.context || null,
      }
    }
  } catch {
    // Not JSON
  }

  const lines = reason.split('\n')
  const question = lines[0]?.replace(/^#+\s*/, '') || 'Agent needs your input'
  const options: ParsedOption[] = []
  let currentOption: ParsedOption | null = null
  const contextLines: string[] = []
  let inOptions = false

  for (let i = 1; i < lines.length; i++) {
    const line = lines[i].trim()

    if (/^#{2,3}\s/.test(line)) {
      if (currentOption) options.push(currentOption)
      currentOption = {
        label: line.replace(/^#{2,3}\s*/, ''),
        description: '',
        pros: [],
        cons: [],
      }
      inOptions = true
    } else if (currentOption) {
      const prosMatch = line.match(/^pros?:\s*(.+)/i)
      const consMatch = line.match(/^cons?:\s*(.+)/i)
      const confMatch = line.match(/^confidence:\s*(\d+)/i)

      if (prosMatch) {
        currentOption.pros.push(...prosMatch[1].split(',').map(s => s.trim()).filter(Boolean))
      } else if (consMatch) {
        currentOption.cons.push(...consMatch[1].split(',').map(s => s.trim()).filter(Boolean))
      } else if (confMatch) {
        currentOption.confidence = parseInt(confMatch[1], 10)
      } else if (line) {
        currentOption.description += (currentOption.description ? ' ' : '') + line
      }
    } else if (!inOptions && line) {
      contextLines.push(line)
    }
  }

  if (currentOption) options.push(currentOption)

  return {
    question,
    options,
    context: contextLines.length > 0 ? contextLines.join('\n') : null,
  }
}

function ConfidenceBar({ value }: { value: number }) {
  const pct = Math.min(100, Math.max(0, value))
  const color = pct >= 70 ? 'var(--color-success-foreground)' : pct >= 40 ? 'var(--color-warning-foreground)' : 'var(--color-error)'

  return (
    <div className={CONFIDENCE_CLS}>
      <div className={CONFIDENCE_BAR_CLS}>
        <div
          className={CONFIDENCE_FILL_CLS}
          style={{ width: `${pct}%`, background: color }}
        />
      </div>
      <span className={CONFIDENCE_LABEL_CLS}>{pct}%</span>
    </div>
  )
}

interface EscalationCardProps {
  task: GobbyTaskDetail
  targetStatus?: string | null
  onResolve: (decision: string) => void
}

export function EscalationCard({ task, targetStatus, onResolve }: EscalationCardProps) {
  const [selectedOption, setSelectedOption] = useState<number | null>(null)
  const [customInput, setCustomInput] = useState('')
  const [showCustom, setShowCustom] = useState(false)
  const [isSubmitting, setIsSubmitting] = useState(false)

  const escalation = parseEscalation(task.escalation_reason)

  const handleResolve = useCallback(async () => {
    let decision: string
    if (showCustom && customInput.trim()) {
      decision = customInput.trim()
    } else if (selectedOption !== null && escalation.options[selectedOption]) {
      decision = `Selected: ${escalation.options[selectedOption].label}`
      if (escalation.options[selectedOption].description) {
        decision += ` — ${escalation.options[selectedOption].description}`
      }
    } else {
      return
    }

    setIsSubmitting(true)
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(
        `${baseUrl}/api/tasks/${encodeURIComponent(task.id)}/de-escalate`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            decision_context: decision,
            ...(targetStatus ? { target_status: targetStatus } : {}),
          }),
        }
      )
      if (response.ok) {
        onResolve(decision)
      } else {
        console.error('De-escalation failed:', await response.text())
      }
    } catch (e) {
      console.error('De-escalation request failed:', e)
    } finally {
      setIsSubmitting(false)
    }
  }, [selectedOption, customInput, showCustom, escalation.options, onResolve, targetStatus, task.id])

  const canSubmit = (showCustom ? customInput.trim().length > 0 : selectedOption !== null) && !isSubmitting

  return (
    <div className={CARD_CLS}>
      <div className={HEADER_CLS}>
        <span className={ICON_CLS}>{'⚠'}</span>
        <span className={TITLE_CLS}>Agent Needs Your Decision</span>
        {task.escalated_at && (
          <span className={TIME_CLS}>
            {new Date(task.escalated_at).toLocaleString(undefined, {
              month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
            })}
          </span>
        )}
      </div>

      <div className={QUESTION_CLS}>{escalation.question}</div>

      {escalation.context && (
        <div className={CONTEXT_CLS}>{escalation.context}</div>
      )}

      {escalation.options.length > 0 && (
        <div className={OPTIONS_CLS}>
          {escalation.options.map((opt, i) => (
            <button
              key={i}
              className={cn(OPTION_CLS, selectedOption === i && OPTION_SELECTED_CLS)}
              onClick={() => { setSelectedOption(i); setShowCustom(false) }}
            >
              <div className={OPTION_HEADER_CLS}>
                <span className={cn(OPTION_RADIO_CLS, selectedOption === i && OPTION_RADIO_SELECTED_CLS)}>
                  {selectedOption === i ? '◉' : '○'}
                </span>
                <span className={OPTION_LABEL_CLS}>{opt.label}</span>
                {opt.confidence !== undefined && <ConfidenceBar value={opt.confidence} />}
              </div>
              {opt.description && (
                <div className={OPTION_DESC_CLS}>{opt.description}</div>
              )}
              {(opt.pros.length > 0 || opt.cons.length > 0) && (
                <div className={OPTION_TRADEOFFS_CLS}>
                  {opt.pros.length > 0 && (
                    <div>
                      {opt.pros.map((p, j) => (
                        <span key={j} className={PRO_CLS}>+ {p}</span>
                      ))}
                    </div>
                  )}
                  {opt.cons.length > 0 && (
                    <div>
                      {opt.cons.map((c, j) => (
                        <span key={j} className={CON_CLS}>- {c}</span>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </button>
          ))}
        </div>
      )}

      <button
        className={cn(CUSTOM_TOGGLE_CLS, showCustom && CUSTOM_TOGGLE_ACTIVE_CLS)}
        onClick={() => { setShowCustom(!showCustom); setSelectedOption(null) }}
      >
        {showCustom ? '▾' : '▸'} Provide custom response
      </button>

      {showCustom && (
        <textarea
          className={CUSTOM_INPUT_CLS}
          value={customInput}
          onChange={e => setCustomInput(e.target.value)}
          placeholder="Type your decision or instructions..."
          aria-label="Decision or instructions"
          rows={3}
          autoFocus
        />
      )}

      <div className={ACTIONS_CLS}>
        <button
          className={SUBMIT_CLS}
          onClick={handleResolve}
          disabled={!canSubmit}
        >
          {isSubmitting ? 'Returning...' : '↩ Return to Agent'}
        </button>
      </div>
    </div>
  )
}
