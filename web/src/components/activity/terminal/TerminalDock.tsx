import { Suspense, lazy } from "react";

import type { GobbySession } from "../../../types/sessions";
import { Button } from "../../ui/Button";

const TerminalTab = lazy(() =>
  import("./TerminalTab").then((module) => ({ default: module.TerminalTab })),
);

export interface TerminalDockProps {
  sessions: GobbySession[];
  focusSessionId: string | null;
  onFocusHandled: () => void;
  expanded: boolean;
  onToggleExpanded: () => void;
  onClose: () => void;
}

function TerminalGlyph() {
  return (
    <svg
      className="size-3.5"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <polyline points="4 17 10 11 4 5" />
      <line x1="12" y1="19" x2="20" y2="19" />
    </svg>
  );
}

function ExpandIcon({ expanded }: { expanded: boolean }) {
  return (
    <svg
      className="size-3.5"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {expanded ? (
        <>
          <polyline points="4 14 10 14 10 20" />
          <polyline points="20 10 14 10 14 4" />
        </>
      ) : (
        <>
          <polyline points="15 3 21 3 21 9" />
          <polyline points="9 21 3 21 3 15" />
        </>
      )}
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg
      className="size-3.5"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  );
}

// Bottom terminal dock. Collapsed it holds a fixed height under the chat and
// activity panes; expanded it takes the full viewport (the row above hides).
// The embedded ghostty terminal auto-resizes on container changes, which
// plumbs a PTY resize through TerminalTab's onSizeChange.
export function TerminalDock({
  sessions,
  focusSessionId,
  onFocusHandled,
  expanded,
  onToggleExpanded,
  onClose,
}: TerminalDockProps) {
  return (
    <section
      aria-label="Terminal"
      className={`flex min-h-0 flex-col border-t border-border bg-background ${
        expanded ? "flex-1" : "h-80 shrink-0"
      }`}
    >
      <div className="flex h-9 shrink-0 items-center gap-2 border-b border-border px-3">
        <span className="flex items-center gap-1.5 text-xs font-semibold text-foreground">
          <TerminalGlyph />
          Terminal
        </span>
        <div className="ms-auto flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon"
            dense
            aria-label={expanded ? "Collapse terminal panel" : "Expand terminal panel"}
            title={expanded ? "Collapse terminal panel" : "Expand terminal panel"}
            onClick={onToggleExpanded}
          >
            <ExpandIcon expanded={expanded} />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            dense
            aria-label="Close terminal panel"
            title="Close terminal panel"
            onClick={onClose}
          >
            <CloseIcon />
          </Button>
        </div>
      </div>
      <div className="min-h-0 flex-1 p-3">
        <Suspense fallback={null}>
          <TerminalTab
            sessions={sessions}
            focusSessionId={focusSessionId}
            onFocusHandled={onFocusHandled}
          />
        </Suspense>
      </div>
    </section>
  );
}
