import { useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
} from "../ui/Dialog";
import type { AgentDefInfo } from "../../hooks/useAgentDefinitions";
import { cn } from "../../lib/utils";
import { Button } from "../ui/Button";
import { SegmentedControl } from "../ui/SegmentedControl";

interface AgentPickerDropdownProps {
  definitions: AgentDefInfo[];
  globalDefs: AgentDefInfo[];
  projectDefs: AgentDefInfo[];
  showScopeToggle: boolean;
  hasGlobal: boolean;
  hasProject: boolean;
  activeAgent?: string;
  onSelect: (agentName: string) => void;
  onClose: () => void;
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
  const [scope, setScope] = useState<"global" | "project">(
    hasProject ? "project" : "global",
  );

  const visibleDefs =
    scope === "project" && hasProject ? projectDefs : globalDefs;

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent
        className="max-w-sm gap-0 overflow-hidden p-0"
        onOpenAutoFocus={(e) => e.preventDefault()}
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <DialogTitle className="text-sm font-semibold">
            Select Persona
          </DialogTitle>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            dense
            className="min-h-0 w-auto rounded p-1 text-muted-foreground transition-colors hover:bg-muted hover:text-foreground"
            aria-label="Close persona picker"
            onClick={onClose}
          >
            <CloseIcon />
          </Button>
        </div>
        <DialogDescription className="sr-only">
          Choose a persona for this conversation
        </DialogDescription>
        {showScopeToggle && (
          <div className="border-b border-[var(--border)] px-2 py-1.5">
            <SegmentedControl
              value={scope}
              onChange={setScope}
              options={[
                { value: "global", label: "Global" },
                { value: "project", label: "Project" },
              ]}
              ariaLabel="Persona scope"
              controlHeight="sm"
              className="w-full"
            />
          </div>
        )}
        <div className="max-h-60 overflow-y-auto py-1">
          {visibleDefs.length === 0 && (
            <div className="px-4 py-3 text-center text-[length:var(--text-sm)] text-[var(--text-muted)]">
              No agents
            </div>
          )}
          {visibleDefs.map((d) => {
            const name = d.definition.name;
            const isActive = name === activeAgent;
            return (
              <Button
                key={`${d.source}-${name}`}
                type="button"
                variant="ghost"
                dense
                className={cn(
                  "flex min-h-0 w-full cursor-pointer flex-col items-stretch justify-start rounded-none border-0 bg-transparent px-3 py-2 text-left font-normal whitespace-normal text-[var(--text-primary)] transition-colors duration-150 hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11",
                  isActive &&
                    "bg-[color-mix(in_srgb,var(--accent)_15%,transparent)] hover:bg-[color-mix(in_srgb,var(--accent)_15%,transparent)]",
                )}
                onClick={() => {
                  onSelect(name);
                  onClose();
                }}
              >
                <div className="flex items-center gap-2">
                  <AgentIcon />
                  <span className="text-[length:var(--text-md)] font-medium">
                    {name}
                  </span>
                  {isActive && (
                    <span className="ml-auto text-xs text-[var(--accent)]">
                      &#10003;
                    </span>
                  )}
                </div>
                {d.definition.description && (
                  <div className="mt-0.5 ml-[1.375rem] overflow-hidden text-xs leading-[1.3] text-ellipsis whitespace-nowrap text-[var(--text-muted)]">
                    {d.definition.description}
                  </div>
                )}
              </Button>
            );
          })}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function AgentIcon() {
  return (
    <svg
      width="14"
      height="14"
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
  );
}

function CloseIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  );
}
