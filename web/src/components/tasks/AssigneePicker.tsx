import { useState, useEffect, useCallback, useRef } from 'react'
import {
  agentIcon,
  formatAssigneeDisplay,
  getBaseUrl,
  inferAssigneeType,
  shortId,
  type KnownAgent,
} from './assigneeUtils'
import { cn } from '../../lib/utils'

const ROOT_CLS = 'relative'
const TRIGGER_CLS = 'inline-flex cursor-pointer items-center gap-[5px] rounded border border-[var(--border)] bg-[var(--bg-secondary)] px-2 py-0.5 font-[inherit] text-[length:var(--text-sm)] text-[var(--text-primary)] transition-colors duration-150 hover:border-[var(--accent)] pointer-coarse:min-h-11'
const ICON_CLS = 'text-[length:var(--text-md)]'
const VALUE_CLS = 'max-w-[140px] overflow-hidden text-ellipsis whitespace-nowrap'
const CHEVRON_CLS = 'text-[length:var(--text-2xs)] text-[var(--text-muted)]'

const DROPDOWN_CLS = 'absolute left-0 top-full z-[100] mt-1 max-h-[300px] min-w-[220px] overflow-y-auto rounded-md border border-[var(--border)] bg-[var(--bg-primary)] p-1 shadow-[var(--shadow-md)]'
const MODE_CLS = 'mb-1 flex gap-px border-b border-[var(--border)] p-1'
const MODE_BTN_CLS = 'flex-1 cursor-pointer rounded-sm border border-[var(--border)] bg-[var(--bg-secondary)] px-2 py-[3px] font-[inherit] text-[length:var(--text-xs)] text-[var(--text-secondary)] pointer-coarse:min-h-11'
const MODE_BTN_ACTIVE_CLS = 'border-[color-mix(in_srgb,var(--color-info)_30%,transparent)] bg-[color-mix(in_srgb,var(--color-info)_15%,transparent)] text-[var(--color-info)]'

const SECONDARY_INPUT_CLS = 'mx-2 my-1 block w-[calc(100%-1rem)] rounded-sm border border-[var(--border)] bg-[var(--bg-secondary)] px-2 py-1 font-[inherit] text-[length:var(--text-xs)] text-[var(--text-primary)] pointer-coarse:min-h-11'

const OPTION_CLS = 'flex w-full cursor-pointer items-center gap-1.5 rounded border-0 bg-transparent px-2 py-1 text-left font-[inherit] text-[length:var(--text-sm)] text-[var(--text-secondary)] hover:bg-[var(--bg-secondary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11'
const OPTION_ACTIVE_CLS = 'bg-[color-mix(in_srgb,var(--color-info)_10%,transparent)] text-[var(--color-info)]'
const OPTION_ICON_CLS = 'shrink-0 text-[length:var(--text-md)]'
const OPTION_LABEL_CLS = 'flex-1 overflow-hidden text-ellipsis whitespace-nowrap'
const OPTION_ID_CLS = 'font-[inherit] text-[length:var(--text-2xs)] text-[var(--text-muted)]'

const CUSTOM_ROW_CLS = 'flex gap-1 px-2 py-1'
const CUSTOM_INPUT_CLS = 'flex-1 rounded-sm border border-[var(--border)] bg-[var(--bg-secondary)] px-1.5 py-[3px] font-[inherit] text-[length:var(--text-xs)] text-[var(--text-primary)] pointer-coarse:min-h-11'
const CUSTOM_BTN_CLS = 'cursor-pointer rounded-sm border border-[color-mix(in_srgb,var(--color-info)_30%,transparent)] bg-[var(--color-info-soft)] px-2 py-[3px] font-[inherit] text-[length:var(--text-xs)] text-[var(--color-info)] disabled:cursor-default disabled:opacity-40 pointer-coarse:min-h-11'

type OwnershipMode = 'single' | 'joint'

interface AssigneePickerProps {
  currentAssignee: string | null
  currentAgentName: string | null
  onAssign: (assignee: string | null) => void
}

export function AssigneePicker({ currentAssignee, currentAgentName, onAssign }: AssigneePickerProps) {
  const [isOpen, setIsOpen] = useState(false)
  const [agents, setAgents] = useState<KnownAgent[]>([])
  const [mode, setMode] = useState<OwnershipMode>('single')
  const [secondaryAssignee, setSecondaryAssignee] = useState('')
  const [customValue, setCustomValue] = useState('')
  const [showCustom, setShowCustom] = useState(false)
  const dropdownRef = useRef<HTMLDivElement>(null)

  const fetchAgents = useCallback(async () => {
    try {
      const baseUrl = getBaseUrl()
      const response = await fetch(`${baseUrl}/api/sessions?limit=50`)
      if (!response.ok) {
        console.warn(`Agent fetch returned ${response.status}`)
        return
      }
      const data = await response.json()
      const sessions: Array<{ id: string; agent_name?: string; cli_type?: string }> = data.sessions || []

      const seen = new Set<string>()
      const results: KnownAgent[] = []

      for (const s of sessions) {
        const name = s.agent_name || s.cli_type || null
        const key = name || s.id
        if (seen.has(key)) continue
        seen.add(key)

        results.push({
          id: s.id,
          label: name || shortId(s.id),
          type: name ? 'agent' : 'session',
        })
      }

      setAgents(results)
    } catch (e) {
      console.error('Failed to fetch agents:', e)
    }
  }, [])

  useEffect(() => {
    if (isOpen) fetchAgents()
  }, [isOpen, fetchAgents])

  useEffect(() => {
    if (!isOpen) return
    const handler = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setIsOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [isOpen])

  const handleSelect = (agent: KnownAgent | null) => {
    if (!agent) {
      onAssign(null)
      setIsOpen(false)
      return
    }

    if (mode === 'joint' && secondaryAssignee.trim()) {
      onAssign(`${agent.id}+${secondaryAssignee.trim()}`)
    } else {
      onAssign(agent.id)
    }
    setIsOpen(false)
  }

  const handleCustomSubmit = () => {
    if (!customValue.trim()) return
    if (mode === 'joint' && secondaryAssignee.trim()) {
      onAssign(`${customValue.trim()}+${secondaryAssignee.trim()}`)
    } else {
      onAssign(customValue.trim())
    }
    setIsOpen(false)
    setShowCustom(false)
    setCustomValue('')
    setSecondaryAssignee('')
  }

  const displayAssignee = currentAssignee
    ? formatAssigneeDisplay(currentAssignee, currentAgentName)
    : 'Unassigned'
  const currentAssigneeType = inferAssigneeType(currentAssignee, currentAgentName)

  return (
    <div className={ROOT_CLS} ref={dropdownRef}>
      <button
        type="button"
        className={TRIGGER_CLS}
        onClick={() => setIsOpen(!isOpen)}
        aria-haspopup="listbox"
        aria-expanded={isOpen}
        aria-controls={isOpen ? 'assignee-picker-dropdown' : undefined}
      >
        <span className={ICON_CLS}>
          {currentAssignee ? agentIcon(currentAssigneeType) : '○'}
        </span>
        <span className={VALUE_CLS}>{displayAssignee}</span>
        <span className={CHEVRON_CLS}>{isOpen ? '▾' : '▸'}</span>
      </button>

      {isOpen && (
        <div className={DROPDOWN_CLS} id="assignee-picker-dropdown" role="listbox">
          <div className={MODE_CLS}>
            <button
              type="button"
              className={cn(MODE_BTN_CLS, mode === 'single' && MODE_BTN_ACTIVE_CLS)}
              onClick={() => setMode('single')}
            >
              Single
            </button>
            <button
              type="button"
              className={cn(MODE_BTN_CLS, mode === 'joint' && MODE_BTN_ACTIVE_CLS)}
              onClick={() => setMode('joint')}
            >
              Joint
            </button>
          </div>

          {mode === 'joint' && (
            <input
              className={SECONDARY_INPUT_CLS}
              placeholder="Secondary assignee..."
              value={secondaryAssignee}
              onChange={e => setSecondaryAssignee(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') e.preventDefault() }}
            />
          )}

          <button
            type="button"
            role="option"
            aria-selected={!currentAssignee}
            className={cn(OPTION_CLS, !currentAssignee && OPTION_ACTIVE_CLS)}
            onClick={() => handleSelect(null)}
          >
            <span className={OPTION_ICON_CLS}>○</span>
            <span>Unassigned</span>
          </button>

          {agents.map(agent => (
            <button
              key={agent.id}
              type="button"
              role="option"
              aria-selected={currentAssignee?.split('+')[0] === agent.id}
              className={cn(OPTION_CLS, currentAssignee?.split('+')[0] === agent.id && OPTION_ACTIVE_CLS)}
              onClick={() => handleSelect(agent)}
            >
              <span className={OPTION_ICON_CLS}>{agentIcon(agent.type)}</span>
              <span className={OPTION_LABEL_CLS}>{agent.label}</span>
              <span className={OPTION_ID_CLS}>{shortId(agent.id)}</span>
            </button>
          ))}

          <button
            type="button"
            role="option"
            aria-selected={showCustom}
            className={cn(OPTION_CLS, showCustom && OPTION_ACTIVE_CLS)}
            onClick={() => setShowCustom(!showCustom)}
          >
            <span className={OPTION_ICON_CLS}>✎</span>
            <span>Custom...</span>
          </button>

          {showCustom && (
            <div className={CUSTOM_ROW_CLS}>
              <input
                className={CUSTOM_INPUT_CLS}
                placeholder="Session ID or name..."
                value={customValue}
                onChange={e => setCustomValue(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); handleCustomSubmit() } }}
                autoFocus
              />
              <button
                type="button"
                className={CUSTOM_BTN_CLS}
                onClick={handleCustomSubmit}
                disabled={!customValue.trim()}
              >
                Assign
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
