import { cn } from "../../../lib/utils";
import { ActivityRowStatusDot, type StatusKind } from "../ActivityRowStatusDot";
import { QuickMenu, type QuickMenuItem } from "../QuickMenu";
import { SourceIcon } from "../../shared/SourceIcon";
import { Button } from "../../ui/Button";
import { Chip } from "../../ui/Chip";
import { chipIdentityClasses } from "../../ui/chipVariants";
import { coarseHitAreaCls } from "../../ui/controlStyles";
import { type JoinedTerminalSession, sessionKey } from "./terminalSessions";

interface TerminalSessionListProps {
  sessions: JoinedTerminalSession[];
  value: string | null;
  onChange: (value: string) => void;
  onTerminate: (session: JoinedTerminalSession) => void;
}

interface LifecyclePresentation {
  kind: StatusKind;
  pulse: boolean;
  label: string;
}

function lifecyclePresentation(status: string): LifecyclePresentation {
  const kind =
    status === "active"
      ? "active"
      : status === "expired"
        ? "stopped"
        : status === "paused"
          ? "paused"
          : "warning";
  return {
    kind,
    pulse: status === "active",
    label: `Session ${status}`,
  };
}

/** VT-100-style monitor mark for terminals Gobby didn't create — it sits in
 * the provider-icon slot, so an external row reads like any provider row. */
function ExternalTerminalGlyph() {
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
      className="shrink-0 text-accent"
      role="img"
      aria-label="External terminal"
    >
      <title>A tmux session on this machine that Gobby didn't create</title>
      <rect x="2" y="3" width="20" height="15" rx="2" />
      <path d="m7 8 3 3-3 3" />
      <path d="M13 14h4" />
      <path d="M9 21h6" />
    </svg>
  );
}

function SessionRowContent({ session }: { session: JoinedTerminalSession }) {
  const lifecycle = session.gobby
    ? lifecyclePresentation(session.gobby.status)
    : null;

  return (
    <>
      {lifecycle ? (
        <ActivityRowStatusDot
          kind={lifecycle.kind}
          pulse={lifecycle.pulse}
          label={lifecycle.label}
        />
      ) : null}
      {session.provider ? (
        <SourceIcon source={session.provider} size={14} />
      ) : session.external ? (
        <ExternalTerminalGlyph />
      ) : null}
      <span className="activity-row-title">{session.label}</span>
      {/* paneRef embeds the raw tmux session name, which is unbounded; cap it
          so it can never push the kebab off the row or overflow the list. */}
      <span className="activity-row-meta max-w-[45%] truncate font-mono">
        {session.paneRef}
      </span>
      <span className="flex shrink-0 items-center gap-1">
        <Chip tone="accent" uppercase className={chipIdentityClasses}>
          {session.backendLabel}
        </Chip>
        {session.dead ? (
          <span className="rounded-full border border-destructive/40 bg-destructive/10 px-1.5 py-0.5 text-2xs font-medium text-destructive-foreground">
            Dead
          </span>
        ) : null}
        {session.agentManaged ? (
          <span className="rounded-full border border-info/40 bg-info-soft px-1.5 py-0.5 text-2xs font-medium text-info">
            Agent-managed
          </span>
        ) : null}
      </span>
    </>
  );
}

/**
 * The terminal list mirrors the sessions-list placement: rows in the tab's
 * top area, the terminal view below. Rows use the shared
 * `.activity-list-row` idiom from ActivityPanel so the two lists read as
 * one family.
 */
export function TerminalSessionList({
  sessions,
  value,
  onChange,
  onTerminate,
}: TerminalSessionListProps) {
  return (
    <div className="flex flex-col" role="list" aria-label="Terminal sessions">
      {sessions.map((session) => {
        const key = sessionKey(session.tmux);
        const selected = key === value;
        const menuItems: QuickMenuItem[] = [
          {
            label: "Terminate",
            destructive: true,
            disabled: session.agentManaged,
            title: session.agentManaged
              ? "Managed by an agent — stop the agent instead"
              : undefined,
            onSelect: () => onTerminate(session),
          },
        ];
        return (
          <div
            key={key}
            role="listitem"
            aria-label={`${session.label} terminal`}
            className={cn(
              "activity-list-row",
              selected && "activity-list-row--selected",
            )}
          >
            <Button
              type="button"
              variant="ghost"
              className={cn("activity-list-row__body", coarseHitAreaCls)}
              aria-label={`Attach ${session.label}`}
              aria-pressed={selected}
              onClick={() => onChange(key)}
            >
              <SessionRowContent session={session} />
            </Button>
            <div className="px-1">
              <QuickMenu
                items={menuItems}
                menuLabel={`Actions for ${session.label}`}
                triggerLabel={`Open actions for ${session.label}`}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}
