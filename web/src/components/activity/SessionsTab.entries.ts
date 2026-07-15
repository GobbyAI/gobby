import { useCallback, useEffect, useMemo, useState } from "react";

import { getSessionTitleText } from "../../lib/sessionTitle";
import type { GobbySession } from "../../types/sessions";
import { getVisibleActivitySessions } from "./activitySessionVisibility";
import {
  type RunningAgent,
  type WatchingSessionEntry,
  entryTimestamp,
  getBaseUrl,
  resolveLocalFlag,
} from "./SessionsTab.helpers";
import {
  DEFAULT_LIVE_STATUSES,
  type SessionStatus,
  type SessionsFilters,
} from "./sessionsFilters";

export type SessionStatusMode = "live" | "expired";

export function resolveSessionStatusMode(filters: SessionsFilters): SessionStatusMode {
  return filters.statuses.size === 1 && filters.statuses.has("expired")
    ? "expired"
    : "live";
}

export function statusesForMode(mode: SessionStatusMode): Set<SessionStatus> {
  return mode === "expired"
    ? new Set<SessionStatus>(["expired"])
    : new Set<SessionStatus>(DEFAULT_LIVE_STATUSES);
}

export function useRunningAgents() {
  const [agents, setAgents] = useState<RunningAgent[]>([]);
  const [agentsLoading, setAgentsLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);

  const fetchAgents = useCallback(async () => {
    const baseUrl = getBaseUrl();
    try {
      const response = await fetch(`${baseUrl}/api/agents/running`);
      if (!response.ok) {
        throw new Error(`Running agents request failed (${response.status})`);
      }
      const data: unknown = await response.json();
      const nextAgents = Array.isArray(data)
        ? data
        : typeof data === "object" && data !== null && "agents" in data
          ? (data as { agents: unknown }).agents
          : null;
      if (!Array.isArray(nextAgents)) {
        throw new Error("Running agents response must contain an array");
      }
      setAgents(nextAgents as RunningAgent[]);
      setFetchError(null);
    } catch (error) {
      console.error("Failed to fetch running agents:", error);
      setFetchError("Failed to load running agents");
    } finally {
      setAgentsLoading(false);
    }
  }, []);

  useEffect(() => {
    const fetchNow = () => {
      void fetchAgents();
    };
    const timeout = window.setTimeout(fetchNow, 0);
    const interval = window.setInterval(() => {
      void fetchAgents();
    }, 5000);
    return () => {
      window.clearTimeout(timeout);
      window.clearInterval(interval);
    };
  }, [fetchAgents]);

  return { agents, agentsLoading, fetchError };
}

interface UseWatchingSessionEntriesOptions {
  agents: RunningAgent[];
  chatSessionId?: string | null;
  expiringIds: Set<string>;
  filters: SessionsFilters;
  search: string;
  sessions: GobbySession[];
  statusMode: SessionStatusMode;
}

export function useWatchingSessionEntries({
  agents,
  chatSessionId,
  expiringIds,
  filters,
  search,
  sessions,
  statusMode,
}: UseWatchingSessionEntriesOptions): WatchingSessionEntry[] {
  const visibleSessions = useMemo(
    () =>
      getVisibleActivitySessions(sessions, {
        chatSessionId,
        expiringIds,
        search,
        filters,
      }),
    [chatSessionId, expiringIds, search, sessions, filters],
  );

  return useMemo(() => {
    const agentEntries: WatchingSessionEntry[] =
      statusMode === "live"
        ? agents.reduce<WatchingSessionEntry[]>((nextEntries, agent) => {
            const matchedSession = agent.session_id
              ? visibleSessions.find((session) => session.id === agent.session_id)
              : undefined;
            if (!matchedSession) {
              return nextEntries;
            }
            const sessionIsLocal = resolveLocalFlag(
              matchedSession.is_local,
              matchedSession.source,
              matchedSession.model,
            );
            const agentIsLocal = resolveLocalFlag(
              agent.is_local,
              agent.provider,
              agent.model,
            );
            nextEntries.push({
              id: matchedSession.id,
              type: "agent",
              label: getSessionTitleText(matchedSession.title),
              provider: matchedSession.source ?? agent.provider,
              status: matchedSession.status,
              sessionType: matchedSession.session_type,
              externalId: matchedSession.external_id,
              agentRunId: matchedSession.agent_run_id ?? agent.run_id,
              runId: agent.run_id,
              startedAt: agent.started_at,
              updatedAt: matchedSession.updated_at,
              seqNum: matchedSession.seq_num,
              inputTokens: matchedSession.usage_input_tokens ?? 0,
              outputTokens: matchedSession.usage_output_tokens ?? 0,
              totalTokens:
                (matchedSession.usage_input_tokens ?? 0) +
                (matchedSession.usage_output_tokens ?? 0),
              hasTmux: Boolean(matchedSession.terminal_context),
              sandboxEnabled: matchedSession.sandbox_enabled ?? false,
              isLocal: sessionIsLocal || agentIsLocal,
              acp: matchedSession.acp ?? null,
            });
            return nextEntries;
          }, [])
        : [];

    const agentSessionIds = new Set(agentEntries.map((entry) => entry.id));
    const sessionEntries = visibleSessions
      .filter((session) => !agentSessionIds.has(session.id))
      .map((session) => ({
        id: session.id,
        type: "session" as const,
        label: getSessionTitleText(session.title),
        provider: session.source,
        status: session.status,
        sessionType: session.session_type,
        externalId: session.external_id,
        agentRunId: session.agent_run_id,
        updatedAt: session.updated_at,
        seqNum: session.seq_num,
        inputTokens: session.usage_input_tokens ?? 0,
        outputTokens: session.usage_output_tokens ?? 0,
        totalTokens: (session.usage_input_tokens ?? 0) + (session.usage_output_tokens ?? 0),
        hasTmux: Boolean(session.terminal_context),
        sandboxEnabled: session.sandbox_enabled ?? false,
        isLocal: resolveLocalFlag(session.is_local, session.source, session.model),
        acp: session.acp ?? null,
      }));

    return [...agentEntries, ...sessionEntries].sort((a, b) => {
      // Sort by ref (#N) descending. Entries without a seq (some agent rows)
      // fall to the bottom and tiebreak by recency among themselves.
      const aSeq = a.seqNum ?? -1;
      const bSeq = b.seqNum ?? -1;
      if (bSeq !== aSeq) return bSeq - aSeq;
      return entryTimestamp(b) - entryTimestamp(a);
    });
  }, [agents, statusMode, visibleSessions]);
}
