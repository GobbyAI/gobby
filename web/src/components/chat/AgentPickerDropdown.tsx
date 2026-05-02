import { useState } from 'react'
import { Dialog, DialogContent, DialogTitle, DialogDescription } from './ui/Dialog'
import type { AgentDefInfo } from '../../hooks/useAgentDefinitions'
import { cn } from '../../lib/utils'

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

const SCOPE_TOGGLE_CLS = 'flex gap-0.5 border-b border-[var(--border)] px-2 py-1.5'

const SCOPE_BTN_BASE_CLS =
  'flex-1 cursor-pointer rounded border-0 bg-transparent px-2 py-1 text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-muted)] transition-colors duration-150 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-secondary)] pointer-coarse:min-h-11'

const SCOPE_BTN_ACTIVE_CLS =
  'bg-[var(--accent)] text-[var(--accent-foreground)] hover:bg-[var(--accent)] hover:text-[var(--accent-foreground)]'

const LIST_CLS = 'max-h-60 overflow-y-auto py-1'

const EMPTY_CLS = 'px-4 py-3 text-center text-[length:var(--text-sm)] text-[var(--text-muted)]'

const ITEM_BASE_CLS =
  'flex w-full cursor-pointer flex-col border-0 bg-transparent px-3 py-2 text-left text-[var(--text-primary)] transition-colors duration-150 hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11'

const ITEM_ACTIVE_CLS =
  'bg-[color-mix(in_srgb,var(--accent)_15%,transparent)] hover:bg-[color-mix(in_srgb,var(--accent)_15%,transparent)]'

const ITEM_MAIN_CLS = 'flex items-center gap-2'

const ITEM_NAME_CLS = 'text-[length:var(--text-md)] font-medium'

const ITEM_CHECK_CLS = 'ml-auto text-[length:calc(var(--font-size-base)*0.7)] text-[var(--accent)]'

const ITEM_DESC_CLS =
  'mt-0.5 ml-[1.375rem] overflow-hidden text-ellipsis whitespace-nowrap text-[length:calc(var(--font-size-base)*0.7)] leading-[1.3] text-[var(--text-muted)]'

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
          <div className={SCOPE_TOGGLE_CLS}>
            <button
              type="button"
              className={cn(SCOPE_BTN_BASE_CLS, scope === 'global' && SCOPE_BTN_ACTIVE_CLS)}
              onClick={() => setScope('global')}
            >
              Global
            </button>
            <button
              type="button"
              className={cn(SCOPE_BTN_BASE_CLS, scope === 'project' && SCOPE_BTN_ACTIVE_CLS)}
              onClick={() => setScope('project')}
            >
              Project
            </button>
          </div>
        )}
        <div className={LIST_CLS}>
          {visibleDefs.length === 0 && (
            <div className={EMPTY_CLS}>No agents</div>
          )}
          {visibleDefs.map((d) => {
            const name = d.definition.name
            const isActive = name === activeAgent
            return (
              <button
                key={`${d.source}-${name}`}
                type="button"
                className={cn(ITEM_BASE_CLS, isActive && ITEM_ACTIVE_CLS)}
                onClick={() => {
                  onSelect(name)
                  onClose()
                }}
              >
                <div className={ITEM_MAIN_CLS}>
                  <AgentIcon />
                  <span className={ITEM_NAME_CLS}>{name}</span>
                  {isActive && <span className={ITEM_CHECK_CLS}>&#10003;</span>}
                </div>
                {d.definition.description && (
                  <div className={ITEM_DESC_CLS}>{d.definition.description}</div>
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
