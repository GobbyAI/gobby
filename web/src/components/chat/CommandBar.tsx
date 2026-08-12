import type { AgentDefInfo } from "../../hooks/useAgentDefinitions";
import { getSessionTitleText } from "../../lib/sessionTitle";
import { SourceIcon } from "../shared/SourceIcon";
import { Button } from "../ui/Button";
import { DropdownCaret } from "../ui/DropdownCaret";
import { PanelIcon } from "./icons/PanelIcon";

interface CommandBarProps {
  sessionRef: string | null;
  title: string | null;
  sessionSource?: string | null;
  onOpenPalette: () => void;
  onTogglePanel?: () => void;
  panelVisible?: boolean;
  agentDefinitions?: AgentDefInfo[];
  agentGlobalDefs?: AgentDefInfo[];
  agentProjectDefs?: AgentDefInfo[];
  agentShowScopeToggle?: boolean;
  agentHasGlobal?: boolean;
  agentHasProject?: boolean;
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
    <div className="command-bar flex min-h-[var(--activity-panel-bar-height)] shrink-0 items-center justify-between gap-2 border-b border-[var(--border)] bg-[var(--bg-secondary)] px-3">
      {/* Left cluster — Session context */}
      <div className="command-bar-left flex min-w-0 flex-1 items-center gap-1">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          dense
          className="command-bar-session max-w-full min-w-0 flex-1 basis-auto justify-start rounded border-0 bg-transparent px-2 py-1 text-left text-[length:var(--text-base)] [font-weight:var(--font-weight-medium)] text-[var(--text-primary)] [transition:background_0.1s] hover:bg-[var(--bg-tertiary)]"
          data-testid="chat-session-selector"
          onClick={onOpenPalette}
          title="Switch session (Cmd+K)"
        >
          {sessionSource && (
            <span
              className="command-bar-source inline-flex shrink-0 items-center text-[var(--text-secondary)]"
              aria-hidden="true"
            >
              <SourceIcon source={sessionSource} size={14} />
            </span>
          )}
          {sessionRef && (
            <span className="command-bar-ref shrink-0 [font-weight:var(--font-weight-medium)] text-[var(--accent)]">
              {sessionRef}
            </span>
          )}
          <span className="command-bar-title min-w-0 flex-1 basis-auto overflow-hidden text-left [font-weight:var(--font-weight-medium)] text-ellipsis whitespace-nowrap text-[var(--text-primary)] max-md:max-w-none">
            {getSessionTitleText(title)}
          </span>
          <DropdownCaret />
        </Button>
      </div>

      {/* Right cluster — Actions */}
      <div className="command-bar-right flex shrink-0 items-center gap-1.5">
        {onTogglePanel && (
          <Button
            type="button"
            variant="accent"
            size="sm"
            dense
            className="command-bar-btn min-h-[var(--status-bar-control-height)] shrink-0"
            onClick={onTogglePanel}
            aria-label={
              panelVisible ? "Hide activity panel" : "Show activity panel"
            }
            title={panelVisible ? "Hide activity panel" : "Show activity panel"}
          >
            <PanelIcon visible={panelVisible} />
            <span className="command-bar-btn__label">
              {panelVisible ? "Hide Panel" : "Show Panel"}
            </span>
          </Button>
        )}
      </div>
    </div>
  );
}
