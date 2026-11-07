import {
  getSessionLabelLines,
  type SessionRosterLabel,
} from "../../lib/sessionLabel";
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
  /** Roster labels by session id; search matches the fields a row displays. */
  rosterBySession?: ReadonlyMap<string, SessionRosterLabel>;
}

const NO_ROSTER: ReadonlyMap<string, SessionRosterLabel> = new Map();

export function getVisibleActivitySessions(
  sessions: GobbySession[],
  {
    chatSessionId = null,
    expiringIds,
    search = "",
    filters,
    now = new Date(),
    liveOnly = false,
    rosterBySession = NO_ROSTER,
  }: ActivitySessionVisibilityOptions = {},
): GobbySession[] {
  const query = search.trim().toLowerCase();
  return [...sessions]
    .filter(
      (session) =>
        session.id !== chatSessionId &&
        !expiringIds?.has(session.id) &&
        !HIDDEN_ACTIVITY_SESSION_SOURCES.has(session.source) &&
        (!liveOnly ||
          DEFAULT_LIVE_STATUSES.includes(
            session.status as (typeof DEFAULT_LIVE_STATUSES)[number],
          )) &&
        matchesActivitySessionSearch(
          session,
          query,
          rosterBySession.get(session.id),
        ) &&
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
  roster: SessionRosterLabel | undefined,
): boolean {
  if (!query) {
    return true;
  }
  const fields = [
    getSessionLabelLines(session, roster).head,
    roster?.task?.ref,
    roster?.task?.title,
    roster?.statusLine,
    session.external_id,
  ];
  return fields.some((field) => field?.toLowerCase().includes(query) ?? false);
}

function compareActivitySessions(a: GobbySession, b: GobbySession): number {
  const aSeq = a.seq_num ?? -Infinity;
  const bSeq = b.seq_num ?? -Infinity;
  if (aSeq !== bSeq) return bSeq - aSeq;
  return parseTimestamp(b.created_at) - parseTimestamp(a.created_at);
}

function parseTimestamp(value: string | null | undefined): number {
  if (!value) return -Infinity;
  const parsed = new Date(value).getTime();
  return Number.isFinite(parsed) ? parsed : -Infinity;
}
