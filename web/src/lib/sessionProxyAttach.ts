import type { SessionObservationMeta } from "../types/chat";

type TerminalContext = Record<string, unknown> | null | undefined;

export function hasTerminalLiveness(terminalContext: TerminalContext): boolean {
  if (!terminalContext) {
    return false;
  }

  const tmuxPane = terminalContext.tmux_pane;
  if (typeof tmuxPane === "string" && tmuxPane.length > 0) {
    return true;
  }

  const parentPid = terminalContext.parent_pid;
  if (typeof parentPid === "number") {
    return Number.isFinite(parentPid) && parentPid > 0;
  }
  if (typeof parentPid === "string") {
    const parsed = Number.parseInt(parentPid, 10);
    return Number.isFinite(parsed) && parsed > 0;
  }

  return false;
}

export function canProxyAttachSessionRecord(
  session:
    | {
        session_type?: unknown;
        status?: unknown;
        terminal_context?: TerminalContext;
        can_proxy_attach?: unknown;
      }
    | null
    | undefined,
): boolean {
  if (!session || session.session_type !== "terminal") {
    return false;
  }
  if (typeof session.can_proxy_attach === "boolean") {
    return session.can_proxy_attach;
  }
  if (session.status === "active") {
    return true;
  }
  return hasTerminalLiveness(session.terminal_context);
}

export function canProxyAttachObservationMeta(
  meta: SessionObservationMeta | null | undefined,
): boolean {
  if (!meta || meta.sessionType !== "terminal") {
    return false;
  }
  return meta.canProxyAttach ?? meta.status === "active";
}
