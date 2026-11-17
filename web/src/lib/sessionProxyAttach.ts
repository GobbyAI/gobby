import type { SessionObservationMeta } from "../types/chat";

type TerminalContext = Record<string, unknown> | null | undefined;

export function hasTerminalLiveness(terminalContext: TerminalContext): boolean {
  if (!terminalContext) {
    return false;
  }

  const terminalId = terminalContext.gobby_terminal_id;
  return typeof terminalId === "string" && terminalId.length > 0;
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
