import type { GobbySession } from "../../types/sessions";
import type { SwappedSessionTarget } from "../../types/chat";
import {
  DEFAULT_LIVE_STATUSES,
  matchesSessionsFilters,
  type SessionsFilters,
} from "./sessionsFilters";

export const HIDDEN_ACTIVITY_SESSION_SOURCES = new Set([
  "pipeline",
  "cron",
  "system",
]);

interface ActivitySessionVisibilityOptions {
  chatSessionId?: string | null;
  expiringIds?: ReadonlySet<string>;
  search?: string;
  filters?: SessionsFilters;
  now?: Date;
  liveOnly?: boolean;
}

export function getVisibleActivitySessions(
  sessions: GobbySession[],
  {
    chatSessionId = null,
    expiringIds,
    search = "",
    filters,
    now = new Date(),
    liveOnly = false,
  }: ActivitySessionVisibilityOptions = {},
): GobbySession[] {
  const query = search.trim().toLowerCase();
  return sessions
    .filter(
      (session) =>
        session.id !== chatSessionId &&
        !expiringIds?.has(session.id) &&
        !HIDDEN_ACTIVITY_SESSION_SOURCES.has(session.source) &&
        (!liveOnly || DEFAULT_LIVE_STATUSES.some((status) => status === session.status)) &&
        matchesActivitySessionSearch(session, query) &&
        (!filters || matchesSessionsFilters(session, filters, now)),
    )
    .sort(compareActivitySessions);
}

export function toSwappedSessionTarget(
  session: GobbySession,
): SwappedSessionTarget {
  return {
    sessionId: session.id,
    sessionType: session.session_type,
    agentRunId: session.agent_run_id,
  };
}

function matchesActivitySessionSearch(
  session: GobbySession,
  query: string,
): boolean {
  if (!query) {
    return true;
  }
  return (
    (session.title && session.title.toLowerCase().includes(query)) ||
    session.ref.toLowerCase().includes(query) ||
    (session.external_id?.toLowerCase().includes(query) ?? false)
  );
}

function compareActivitySessions(a: GobbySession, b: GobbySession): number {
  const aSeq = a.seq_num ?? -Infinity;
  const bSeq = b.seq_num ?? -Infinity;
  if (aSeq !== bSeq) return bSeq - aSeq;
  return parseTimestamp(b.created_at) - parseTimestamp(a.created_at);
}

function parseTimestamp(value: string | null | undefined): number {
  if (!value) return 0;
  const parsed = new Date(value).getTime();
  return Number.isFinite(parsed) ? parsed : 0;
}
