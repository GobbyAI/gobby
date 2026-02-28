import { useState, useCallback } from 'react'
import type { GobbyTaskDetail } from '../../hooks/useTasks'

function getBaseUrl(): string {
  return ''
}

// =============================================================================
// Types
// =============================================================================

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

// =============================================================================
// Parse escalation reason into structured data
// =============================================================================

/**
 * Try to extract structured options from escalation_reason.
 * Supports markdown-like format:
 *   ## Question text
 *   ### Option A
 *   Pros: x, y
 *   Cons: z
 *   ### Option B
 *   ...
 * Falls back to showing raw text if unparseable.
 */
function parseEscalation(reason: string | null): ParsedEscalation {
  if (!reason) {
    return { question: 'Agent needs your input', options: [], context: null }
  }

  // Try JSON parse first (structured escalation)
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

  // Try markdown-style parsing
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

// =============================================================================
// ConfidenceBar
// =============================================================================

function ConfidenceBar({ value }: { value: number }) {
  const pct = Math.min(100, Math.max(0, value))
  const color = pct >= 70 ? '#22c55e' : pct >= 40 ? '#eab308' : '#ef4444'

  return (
    <div className="escalation-confidence">
      <div className="escalation-confidence-bar">
        <div
          className="escalation-confidence-fill"
          style={{ width: `${pct}%`, background: color }}
        />
      </div>
      <span className="escalation-confidence-label">{pct}%</span>
    </div>
  )
}

// =============================================================================
// EscalationCard
// =============================================================================

interface EscalationCardProps {
  task: GobbyTaskDetail
  onResolve: (decision: string) => void
}

export function EscalationCard({ task, onResolve }: EscalationCardProps) {
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
          body: JSON.stringify({ decision_context: decision }),
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
  }, [selectedOption, customInput, showCustom, escalation.options, onResolve, task.id])

  const canSubmit = (showCustom ? customInput.trim().length > 0 : selectedOption !== null) && !isSubmitting

  return (
    <div className="escalation-card">
      <div className="escalation-card-header">
        <span className="escalation-card-icon">{'\u26A0'}</span>
        <span className="escalation-card-title">Agent Needs Your Decision</span>
        {task.escalated_at && (
          <span className="escalation-card-time">
            {new Date(task.escalated_at).toLocaleString(undefined, {
              month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
            })}
          </span>
        )}
      </div>

      <div className="escalation-card-question">{escalation.question}</div>

      {escalation.context && (
        <div className="escalation-card-context">{escalation.context}</div>
      )}

      {/* Options */}
      {escalation.options.length > 0 && (
        <div className="escalation-options">
          {escalation.options.map((opt, i) => (
            <button
              key={i}
              className={`escalation-option ${selectedOption === i ? 'escalation-option--selected' : ''}`}
              onClick={() => { setSelectedOption(i); setShowCustom(false) }}
            >
              <div className="escalation-option-header">
                <span className="escalation-option-radio">
                  {selectedOption === i ? '\u25C9' : '\u25CB'}
                </span>
                <span className="escalation-option-label">{opt.label}</span>
                {opt.confidence !== undefined && <ConfidenceBar value={opt.confidence} />}
              </div>
              {opt.description && (
                <div className="escalation-option-desc">{opt.description}</div>
              )}
              {(opt.pros.length > 0 || opt.cons.length > 0) && (
                <div className="escalation-option-tradeoffs">
                  {opt.pros.length > 0 && (
                    <div className="escalation-option-pros">
                      {opt.pros.map((p, j) => (
                        <span key={j} className="escalation-pro">+ {p}</span>
                      ))}
                    </div>
                  )}
                  {opt.cons.length > 0 && (
                    <div className="escalation-option-cons">
                      {opt.cons.map((c, j) => (
                        <span key={j} className="escalation-con">- {c}</span>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </button>
          ))}
        </div>
      )}

      {/* Custom input toggle */}
      <button
        className={`escalation-custom-toggle ${showCustom ? 'active' : ''}`}
        onClick={() => { setShowCustom(!showCustom); setSelectedOption(null) }}
      >
        {showCustom ? '\u25BE' : '\u25B8'} Provide custom response
      </button>

      {showCustom && (
        <textarea
          className="escalation-custom-input"
          value={customInput}
          onChange={e => setCustomInput(e.target.value)}
          placeholder="Type your decision or instructions..."
          aria-label="Decision or instructions"
          rows={3}
          autoFocus
        />
      )}

      {/* Submit */}
      <div className="escalation-card-actions">
        <button
          className="task-detail-action-btn task-detail-action-btn--primary"
          onClick={handleResolve}
          disabled={!canSubmit}
        >
          {isSubmitting ? 'Returning...' : '\u21A9 Return to Agent'}
        </button>
      </div>
    </div>
  )
}
