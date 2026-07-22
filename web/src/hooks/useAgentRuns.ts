import { useState, useEffect, useCallback, useRef } from "react";
import { useWebSocketEvent } from "./useWebSocketEvent";

export interface AgentRunRecord {
  id: string;
  parent_session_id: string;
  child_session_id: string | null;
  workflow_name: string | null;
  provider: string;
  model: string | null;
  status: "pending" | "running" | "success" | "error" | "timeout" | "cancelled";
  prompt: string;
  result: string | null;
  error: string | null;
  tool_calls_count: number;
  turns_used: number;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
  task_id: string | null;
  mode: string;
  worktree_id: string | null;
  clone_id: string | null;
  // Session enrichment (from API)
  usage_input_tokens?: number;
  usage_output_tokens?: number;
  usage_cache_creation_tokens?: number;
  usage_cache_read_tokens?: number;
  summary_markdown?: string | null;
  git_branch?: string | null;
}

export type AgentRunDetail = AgentRunRecord;

export interface SessionAttentionSummary {
  count: number;
  reasons: string[];
}

interface AttentionCursor {
  epoch: string;
  seq: number;
}

interface AttentionEvent extends AttentionCursor {
  entryId: string;
  sessionId: string | null;
  state: "blocked" | null;
  reason: string | null;
}

interface BlockedAttentionEntry {
  sessionId: string;
  reason: string;
}

interface Filters {
  status?: string;
}

function parseAttentionEvent(data: Record<string, unknown>): AttentionEvent | null {
  if (
    data.event !== "attention_changed" ||
    typeof data.epoch !== "string" ||
    typeof data.seq !== "number" ||
    typeof data.entry_id !== "string" ||
    (data.state !== "blocked" && data.state !== null)
  ) {
    return null;
  }
  return {
    epoch: data.epoch,
    seq: data.seq,
    entryId: data.entry_id,
    sessionId: typeof data.session_id === "string" ? data.session_id : null,
    state: data.state,
    reason: typeof data.reason === "string" ? data.reason : null,
  };
}

function applyAttentionEvent(
  entries: Map<string, BlockedAttentionEntry>,
  event: AttentionEvent,
): void {
  if (event.state === "blocked" && event.sessionId !== null) {
    entries.set(event.entryId, {
      sessionId: event.sessionId,
      reason: event.reason || "Attention required",
    });
  } else {
    entries.delete(event.entryId);
  }
}

function summarizeAttention(
  entries: ReadonlyMap<string, BlockedAttentionEntry>,
): Map<string, SessionAttentionSummary> {
  const grouped = new Map<string, { count: number; reasons: Set<string> }>();
  for (const entry of entries.values()) {
    const summary = grouped.get(entry.sessionId) ?? { count: 0, reasons: new Set<string>() };
    summary.count += 1;
    summary.reasons.add(entry.reason);
    grouped.set(entry.sessionId, summary);
  }
  return new Map(
    Array.from(grouped, ([sessionId, summary]) => [
      sessionId,
      { count: summary.count, reasons: Array.from(summary.reasons).sort() },
    ]),
  );
}

export function useAgentRuns(projectId?: string | null) {
  const [runs, setRuns] = useState<AgentRunRecord[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [filters, setFilters] = useState<Filters>({});
  const [attentionBySession, setAttentionBySession] = useState<
    Map<string, SessionAttentionSummary>
  >(new Map());
  const refetchTimerRef = useRef<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const attentionAbortRef = useRef<AbortController | null>(null);
  const attentionCursorRef = useRef<AttentionCursor | null>(null);
  const attentionEntriesRef = useRef<Map<string, BlockedAttentionEntry>>(new Map());
  const bufferedAttentionEventsRef = useRef<AttentionEvent[]>([]);
  const attentionFetchInFlightRef = useRef(false);

  const fetchRuns = useCallback(async () => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    const params = new URLSearchParams();
    if (filters.status) params.set("status", filters.status);
    if (projectId) params.set("project_id", projectId);
    params.set("limit", "100");

    try {
      const res = await fetch(`/api/agents/runs?${params}`, {
        signal: controller.signal,
      });
      if (res.ok) {
        const data = await res.json();
        setRuns(data.runs || []);
      } else {
        console.error(
          "Failed to fetch agent runs:",
          res.status,
          res.statusText,
        );
        setRuns([]);
      }
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      console.error("Failed to fetch agent runs:", e);
    } finally {
      setIsLoading(false);
    }
  }, [filters, projectId]);

  const fetchAttentionRoster = useCallback(async function fetchRosterRequest() {
    if (attentionFetchInFlightRef.current) return;
    attentionFetchInFlightRef.current = true;
    attentionAbortRef.current?.abort();
    const controller = new AbortController();
    attentionAbortRef.current = controller;

    try {
      const response = await fetch("/api/attention/roster", { signal: controller.signal });
      if (!response.ok) {
        throw new Error(`Attention roster request failed (${response.status})`);
      }
      const data = (await response.json()) as Record<string, unknown>;
      if (
        typeof data.epoch !== "string" ||
        typeof data.seq !== "number" ||
        !Array.isArray(data.entries)
      ) {
        throw new Error("Attention roster response is invalid");
      }
      const snapshotSeq = data.seq;

      const pending = bufferedAttentionEventsRef.current;
      bufferedAttentionEventsRef.current = [];
      if (pending.some((event) => event.epoch !== data.epoch)) {
        bufferedAttentionEventsRef.current = pending;
        window.setTimeout(() => void fetchRosterRequest(), 0);
        return;
      }

      const entries = new Map<string, BlockedAttentionEntry>();
      for (const rawEntry of data.entries) {
        if (typeof rawEntry !== "object" || rawEntry === null) continue;
        const entry = rawEntry as Record<string, unknown>;
        const attention = entry.attention;
        if (
          typeof entry.entry_id !== "string" ||
          typeof entry.session_id !== "string" ||
          typeof attention !== "object" ||
          attention === null
        ) {
          continue;
        }
        const state = attention as Record<string, unknown>;
        if (state.state !== "blocked") continue;
        entries.set(entry.entry_id, {
          sessionId: entry.session_id,
          reason: typeof state.reason === "string" ? state.reason : "Attention required",
        });
      }

      let seq = snapshotSeq;
      for (const event of pending
        .filter((candidate) => candidate.seq > snapshotSeq)
        .sort((left, right) => left.seq - right.seq)) {
        applyAttentionEvent(entries, event);
        seq = Math.max(seq, event.seq);
      }
      attentionCursorRef.current = { epoch: data.epoch, seq };
      attentionEntriesRef.current = entries;
      setAttentionBySession(summarizeAttention(entries));
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      console.error("Failed to fetch attention roster:", error);
    } finally {
      attentionFetchInFlightRef.current = false;
    }
  }, []);

  const handleAgentEvent = useCallback(
    (data: Record<string, unknown>) => {
      const attentionEvent = parseAttentionEvent(data);
      if (attentionEvent !== null) {
        const cursor = attentionCursorRef.current;
        if (cursor === null) {
          bufferedAttentionEventsRef.current.push(attentionEvent);
          if (!attentionFetchInFlightRef.current) void fetchAttentionRoster();
        } else if (attentionFetchInFlightRef.current) {
          bufferedAttentionEventsRef.current.push(attentionEvent);
        } else if (attentionEvent.epoch !== cursor.epoch) {
          bufferedAttentionEventsRef.current = [attentionEvent];
          attentionCursorRef.current = null;
          void fetchAttentionRoster();
        } else if (attentionEvent.seq > cursor.seq) {
          const entries = new Map(attentionEntriesRef.current);
          applyAttentionEvent(entries, attentionEvent);
          attentionEntriesRef.current = entries;
          attentionCursorRef.current = { epoch: cursor.epoch, seq: attentionEvent.seq };
          setAttentionBySession(summarizeAttention(entries));
        }
      }

      if (refetchTimerRef.current) clearTimeout(refetchTimerRef.current);
      refetchTimerRef.current = window.setTimeout(() => {
        void fetchRuns();
      }, 500);
    },
    [fetchAttentionRoster, fetchRuns],
  );

  // Register before either initial fetch so attention transitions can be buffered.
  useWebSocketEvent("agent_event", handleAgentEvent);

  // Initial load + refetch on filter change
  useEffect(() => {
    setIsLoading(true);
    void fetchRuns();
  }, [fetchRuns]);

  useEffect(() => {
    void fetchAttentionRoster();
  }, [fetchAttentionRoster]);

  // Clean up on unmount
  useEffect(() => {
    return () => {
      if (refetchTimerRef.current) clearTimeout(refetchTimerRef.current);
      abortRef.current?.abort();
      attentionAbortRef.current?.abort();
    };
  }, []);

  const cancelRun = useCallback(
    async (runId: string) => {
      const res = await fetch(
        `/api/agents/runs/${encodeURIComponent(runId)}/cancel`,
        {
          method: "POST",
        },
      );
      if (!res.ok) throw new Error(`Failed to cancel: ${res.statusText}`);
      const data = await res.json();
      await fetchRuns();
      return data;
    },
    [fetchRuns],
  );

  const fetchRunDetail = useCallback(
    async (runId: string): Promise<AgentRunDetail | null> => {
      try {
        const res = await fetch(
          `/api/agents/runs/${encodeURIComponent(runId)}`,
        );
        if (!res.ok) return null;
        const data = await res.json();
        return data.run || null;
      } catch {
        return null;
      }
    },
    [],
  );

  return {
    runs,
    isLoading,
    filters,
    setFilters,
    fetchRuns,
    cancelRun,
    fetchRunDetail,
    attentionBySession,
  };
}
