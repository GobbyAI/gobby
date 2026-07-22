import { ActivityRowStatusDot, type StatusKind } from "../ActivityRowStatusDot";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../../chat/ui/Select";
import {
  type JoinedTerminalSession,
  sessionKey,
} from "./terminalSessions";

interface TerminalSessionPickerProps {
  sessions: JoinedTerminalSession[];
  value: string | null;
  onChange: (value: string) => void;
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

function SessionOption({ session }: { session: JoinedTerminalSession }) {
  const lifecycle = session.gobby
    ? lifecyclePresentation(session.gobby.status)
    : null;

  return (
    <div className="flex min-w-0 flex-1 items-center gap-2">
      {lifecycle ? (
        <ActivityRowStatusDot
          kind={lifecycle.kind}
          pulse={lifecycle.pulse}
          label={lifecycle.label}
        />
      ) : null}
      <span className="min-w-0 flex-1 truncate">{session.label}</span>
      <span className="flex shrink-0 items-center gap-1">
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
        {session.external ? (
          <span className="rounded-full border border-border bg-muted/50 px-1.5 py-0.5 text-2xs font-medium text-muted-foreground">
            External
          </span>
        ) : null}
      </span>
    </div>
  );
}

export function TerminalSessionPicker({
  sessions,
  value,
  onChange,
}: TerminalSessionPickerProps) {
  return (
    <Select value={value ?? ""} onValueChange={onChange} disabled={sessions.length === 0}>
      <SelectTrigger
        className="min-h-9 w-full bg-[var(--bg-secondary)] text-left pointer-coarse:min-h-11"
        aria-label="Terminal session"
      >
        <SelectValue placeholder={sessions.length === 0 ? "No terminal sessions" : "Select session"} />
      </SelectTrigger>
      <SelectContent className="max-w-[min(32rem,calc(100vw-2rem))]">
        {sessions.map((session) => (
          <SelectItem
            key={sessionKey(session.tmux)}
            value={sessionKey(session.tmux)}
            textValue={session.label}
            className="min-h-10 pointer-coarse:min-h-11"
          >
            <SessionOption session={session} />
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
