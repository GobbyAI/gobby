import { useState, useEffect, useCallback, useRef } from 'react'

// =============================================================================
// Types
// =============================================================================

interface KnownAgent {
  id: string
  label: string
  type: 'agent' | 'human' | 'session'
}

type OwnershipMode = 'single' | 'joint'

// =============================================================================
// Helpers
// =============================================================================

function getBaseUrl(): string {
  return ''
}

function agentIcon(type: KnownAgent['type']): string {
  if (type === 'agent') return '\u2699'    // ⚙
  if (type === 'human') return '\u{1F464}' // 👤
  return '\u{1F4BB}'                        // 💻
}

function shortId(id: string): string {
  if (id.startsWith('#')) return id
  return id.length > 12 ? id.slice(0, 8) + '...' : id
}

// =============================================================================
// AssigneePicker
// =============================================================================

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

  // Fetch known agents from recent sessions
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

  // Close on outside click
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

  // Parse joint assignee display
  const displayAssignee = currentAssignee
    ? formatAssigneeDisplay(currentAssignee, currentAgentName)
    : 'Unassigned'

  return (
    <div className="assignee-picker" ref={dropdownRef}>
      <button
        className="assignee-picker-trigger"
        onClick={() => setIsOpen(!isOpen)}
        aria-haspopup="listbox"
        aria-expanded={isOpen}
        aria-controls={isOpen ? 'assignee-picker-dropdown' : undefined}
      >
        <span className="assignee-picker-icon">
          {currentAssignee ? agentIcon(currentAgentName ? 'agent' : 'session') : '\u25CB'}
        </span>
        <span className="assignee-picker-value">{displayAssignee}</span>
        <span className="assignee-picker-chevron">{isOpen ? '\u25BE' : '\u25B8'}</span>
      </button>

      {isOpen && (
        <div className="assignee-picker-dropdown" id="assignee-picker-dropdown" role="listbox">
          {/* Mode toggle */}
          <div className="assignee-picker-mode">
            <button
              className={`assignee-picker-mode-btn ${mode === 'single' ? 'active' : ''}`}
              onClick={() => setMode('single')}
            >
              Single
            </button>
            <button
              className={`assignee-picker-mode-btn ${mode === 'joint' ? 'active' : ''}`}
              onClick={() => setMode('joint')}
            >
              Joint
            </button>
          </div>

          {mode === 'joint' && (
            <input
              className="assignee-picker-secondary"
              placeholder="Secondary assignee..."
              value={secondaryAssignee}
              onChange={e => setSecondaryAssignee(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter') e.preventDefault() }}
            />
          )}

          {/* Unassigned option */}
          <button
            className={`assignee-picker-option ${!currentAssignee ? 'active' : ''}`}
            onClick={() => handleSelect(null)}
          >
            <span className="assignee-picker-option-icon">{'\u25CB'}</span>
            <span>Unassigned</span>
          </button>

          {/* Known agents */}
          {agents.map(agent => (
            <button
              key={agent.id}
              className={`assignee-picker-option ${currentAssignee?.split('+')[0] === agent.id ? 'active' : ''}`}
              onClick={() => handleSelect(agent)}
            >
              <span className="assignee-picker-option-icon">{agentIcon(agent.type)}</span>
              <span className="assignee-picker-option-label">{agent.label}</span>
              <span className="assignee-picker-option-id">{shortId(agent.id)}</span>
            </button>
          ))}

          {/* Custom entry */}
          <button
            className={`assignee-picker-option assignee-picker-custom-toggle ${showCustom ? 'active' : ''}`}
            onClick={() => setShowCustom(!showCustom)}
          >
            <span className="assignee-picker-option-icon">{'\u270E'}</span>
            <span>Custom...</span>
          </button>

          {showCustom && (
            <div className="assignee-picker-custom">
              <input
                className="assignee-picker-custom-input"
                placeholder="Session ID or name..."
                value={customValue}
                onChange={e => setCustomValue(e.target.value)}
                onKeyDown={e => { if (e.key === 'Enter') { e.preventDefault(); handleCustomSubmit() } }}
                autoFocus
              />
              <button
                className="assignee-picker-custom-btn"
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

// =============================================================================
// Display helper (also used by KanbanCard)
// =============================================================================

export function formatAssigneeDisplay(assignee: string | null, agentName: string | null): string {
  if (!assignee) return 'Unassigned'

  // Joint format: "id1+id2"
  if (assignee.includes('+')) {
    const parts = assignee.split('+')
    return parts.map(p => shortId(p)).join(' + ')
  }

  return agentName || shortId(assignee)
}

export function AssigneeBadge({ assignee, agentName }: { assignee: string | null; agentName: string | null }) {
  if (!assignee) return null

  const isJoint = assignee.includes('+')
  const display = formatAssigneeDisplay(assignee, agentName)
  const type = agentName ? 'agent' : 'session'

  return (
    <span className="assignee-badge" title={assignee}>
      <span className="assignee-badge-icon">
        {isJoint ? '\u{1F91D}' : agentIcon(type)}
      </span>
      <span className="assignee-badge-label">{display}</span>
    </span>
  )
}
