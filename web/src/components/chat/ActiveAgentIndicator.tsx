import { useState } from "react";
import { AgentPickerDropdown } from "./AgentPickerDropdown";
import type { AgentDefInfo } from "../../hooks/useAgentDefinitions";
import { cn } from "../../lib/utils";
import { Button } from "../ui/Button";

interface ActiveAgentIndicatorProps {
  agentName: string;
  onAgentChange: (agentName: string) => void;
  definitions: AgentDefInfo[];
  globalDefs: AgentDefInfo[];
  projectDefs: AgentDefInfo[];
  showScopeToggle: boolean;
  hasGlobal: boolean;
  hasProject: boolean;
  disabled?: boolean;
}

export function ActiveAgentIndicator({
  agentName,
  onAgentChange,
  definitions,
  globalDefs,
  projectDefs,
  showScopeToggle,
  hasGlobal,
  hasProject,
  disabled = false,
}: ActiveAgentIndicatorProps) {
  const [isOpen, setIsOpen] = useState(false);
  const label = disabled
    ? `Attached session owns active persona: ${agentName}`
    : `Active persona: ${agentName}`;

  return (
    <>
      <Button
        type="button"
        variant="ghost"
        size="icon"
        dense
        className={cn(
          "chat-input-agent-button inline-flex size-9 min-h-9 min-w-9 items-center justify-center rounded p-1.5 transition-colors",
          disabled
            ? "cursor-not-allowed text-muted-foreground/50"
            : "text-muted-foreground hover:bg-muted hover:text-foreground",
        )}
        onClick={() => {
          if (!disabled) setIsOpen(!isOpen);
        }}
        title={label}
        aria-label={label}
        disabled={disabled}
      >
        <svg
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" />
          <circle cx="12" cy="7" r="4" />
        </svg>
      </Button>
      {isOpen && !disabled && (
        <AgentPickerDropdown
          definitions={definitions}
          globalDefs={globalDefs}
          projectDefs={projectDefs}
          showScopeToggle={showScopeToggle}
          hasGlobal={hasGlobal}
          hasProject={hasProject}
          activeAgent={agentName}
          onSelect={onAgentChange}
          onClose={() => setIsOpen(false)}
        />
      )}
    </>
  );
}
