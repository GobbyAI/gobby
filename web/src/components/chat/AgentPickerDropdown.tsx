import { useState } from 'react'
import { Dialog, DialogContent, DialogTitle, DialogDescription } from './ui/Dialog'
import type { AgentDefInfo } from '../../hooks/useAgentDefinitions'
import '../workflows/LaunchAgentModal.css'

interface AgentPickerDropdownProps {
  definitions: AgentDefInfo[]
  globalDefs: AgentDefInfo[]
  projectDefs: AgentDefInfo[]
  showScopeToggle: boolean
  hasGlobal: boolean
  hasProject: boolean
  activeAgent?: string
  onSelect: (agentName: string) => void
  onClose: () => void
}

export function AgentPickerDropdown({
  globalDefs,
  projectDefs,
  showScopeToggle,
  hasProject,
  activeAgent,
  onSelect,
  onClose,
}: AgentPickerDropdownProps) {
  const [scope, setScope] = useState<'global' | 'project'>(hasProject ? 'project' : 'global')

  const visibleDefs = scope === 'project' && hasProject ? projectDefs : globalDefs

  return (
    <Dialog open onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent className="max-w-sm p-0 gap-0 overflow-hidden" onOpenAutoFocus={(e) => e.preventDefault()}>
        <div className="flex items-center justify-between px-4 py-3 border-b border-border">
          <DialogTitle className="text-sm font-semibold">Select Persona</DialogTitle>
          <button
            type="button"
            className="p-1 rounded text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
            onClick={onClose}
          >
            <CloseIcon />
          </button>
        </div>
        <DialogDescription className="sr-only">Choose a persona for this conversation</DialogDescription>
        {showScopeToggle && (
          <div className="agent-picker-scope-toggle">
            <button
              type="button"
              className={`agent-picker-scope-btn ${scope === 'global' ? 'active' : ''}`}
              onClick={() => setScope('global')}
            >
              Global
            </button>
            <button
              type="button"
              className={`agent-picker-scope-btn ${scope === 'project' ? 'active' : ''}`}
              onClick={() => setScope('project')}
            >
              Project
            </button>
          </div>
        )}
        <div className="agent-picker-list">
          {visibleDefs.length === 0 && (
            <div className="agent-picker-empty">No agents</div>
          )}
          {visibleDefs.map((d) => {
            const name = d.definition.name
            const isActive = name === activeAgent
            return (
              <button
                key={`${d.source}-${name}`}
                type="button"
                className={`agent-picker-item ${isActive ? 'agent-picker-item--active' : ''}`}
                onClick={() => {
                  onSelect(name)
                  onClose()
                }}
              >
                <div className="agent-picker-item-main">
                  <AgentIcon />
                  <span className="agent-picker-item-name">{name}</span>
                  {isActive && <span className="agent-picker-item-check">&#10003;</span>}
                </div>
                {d.definition.description && (
                  <div className="agent-picker-item-desc">{d.definition.description}</div>
                )}
              </button>
            )
          })}
        </div>
      </DialogContent>
    </Dialog>
  )
}

function AgentIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
      <circle cx="12" cy="7" r="4" />
    </svg>
  )
}

function CloseIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  )
}
