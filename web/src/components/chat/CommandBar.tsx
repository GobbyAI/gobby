import type { AgentDefInfo } from '../../hooks/useAgentDefinitions'
import { getSessionTitleText } from '../../lib/sessionTitle'
import { SourceIcon } from '../shared/SourceIcon'
import { Button } from '../ui/Button'
import { DropdownCaret } from '../ui/DropdownCaret'
import { PanelIcon } from './icons/PanelIcon'

interface CommandBarProps {
  sessionRef: string | null
  title: string | null
  sessionSource?: string | null
  onOpenPalette: () => void
  onTogglePanel?: () => void
  panelVisible?: boolean
  agentDefinitions?: AgentDefInfo[]
  agentGlobalDefs?: AgentDefInfo[]
  agentProjectDefs?: AgentDefInfo[]
  agentShowScopeToggle?: boolean
  agentHasGlobal?: boolean
  agentHasProject?: boolean
}

export function CommandBar({
  sessionRef,
  title,
  sessionSource,
  onOpenPalette,
  onTogglePanel,
  panelVisible = false,
  agentDefinitions: _agentDefinitions = [],
  agentGlobalDefs: _agentGlobalDefs = [],
  agentProjectDefs: _agentProjectDefs = [],
  agentShowScopeToggle: _agentShowScopeToggle = false,
  agentHasGlobal: _agentHasGlobal = false,
  agentHasProject: _agentHasProject = false,
}: CommandBarProps) {
  return (
    <div className="command-bar">
      {/* Left cluster — Session context */}
      <div className="command-bar-left">
        <button
          type="button"
          className="command-bar-session"
          data-testid="chat-session-selector"
          onClick={onOpenPalette}
          title="Switch session (Cmd+K)"
        >
          {sessionSource && (
            <span className="command-bar-source" aria-hidden="true">
              <SourceIcon source={sessionSource} size={14} />
            </span>
          )}
          {sessionRef && (
            <span className="command-bar-ref">{sessionRef}</span>
          )}
          <span className="command-bar-title">
            {getSessionTitleText(title)}
          </span>
          <DropdownCaret />
        </button>
      </div>

      {/* Right cluster — Actions */}
      <div className="command-bar-right">
        {onTogglePanel && (
          <Button
            type="button"
            variant="accent"
            size="sm"
            dense
            className="command-bar-btn"
            onClick={onTogglePanel}
            aria-label={panelVisible ? 'Hide activity panel' : 'Show activity panel'}
            title={panelVisible ? 'Hide activity panel' : 'Show activity panel'}
          >
            <PanelIcon visible={panelVisible} />
            <span className="command-bar-btn__label">
              {panelVisible ? 'Hide Panel' : 'Show Panel'}
            </span>
          </Button>
        )}
      </div>
    </div>
  )
}
