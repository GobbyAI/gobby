import { useCallback, useEffect, useRef, useState } from "react";
import {
  useWebSocketConnected,
  useWebSocketEvent,
} from "./useWebSocketEvent";

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

const ATTENTION_ROSTER_REFRESH_MS = 5_000;

function parseAttentionEvent(
  data: Record<string, unknown>,
): AttentionEvent | null {
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
  entries: Map<string, BlockedAttentionEntry>,
): Map<string, SessionAttentionSummary> {
  const grouped = new Map<
    string,
    { count: number; reasons: Set<string> }
  >();
  for (const entry of entries.values()) {
    const summary = grouped.get(entry.sessionId) ?? {
      count: 0,
      reasons: new Set<string>(),
    };
    summary.count += 1;
    summary.reasons.add(entry.reason);
    grouped.set(entry.sessionId, summary);
  }
  return new Map(
    Array.from(grouped, ([sessionId, summary]) => [
      sessionId,
      {
        count: summary.count,
        reasons: Array.from(summary.reasons).sort(),
      },
    ]),
  );
}

export function useSessionAttention() {
  const [attentionBySession, setAttentionBySession] = useState<
    Map<string, SessionAttentionSummary>
  >(new Map());
  const abortRef = useRef<AbortController | null>(null);
  const cursorRef = useRef<AttentionCursor | null>(null);
  const entriesRef = useRef<Map<string, BlockedAttentionEntry>>(new Map());
  const bufferedEventsRef = useRef<AttentionEvent[]>([]);
  const fetchInFlightRef = useRef(false);
  const resyncAttemptedRef = useRef(false);
  const resyncTimerRef = useRef<number | null>(null);
  const wsConnected = useWebSocketConnected();
  const wasConnectedRef = useRef(wsConnected);

  const fetchAttentionRoster = useCallback(
    async function fetchRosterRequest() {
      if (fetchInFlightRef.current) return;
      fetchInFlightRef.current = true;
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      try {
        const response = await fetch("/api/attention/roster", {
          signal: controller.signal,
        });
        if (!response.ok) {
          throw new Error(
            `Attention roster request failed (${response.status})`,
          );
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
        const pending = bufferedEventsRef.current;
        bufferedEventsRef.current = [];
        const matchingEvents = pending.filter(
          (event) => event.epoch === data.epoch,
        );
        const nonMatchingEvents = pending.filter(
          (event) => event.epoch !== data.epoch,
        );

        if (
          nonMatchingEvents.length > 0 &&
          !resyncAttemptedRef.current
        ) {
          bufferedEventsRef.current = nonMatchingEvents;
          resyncAttemptedRef.current = true;
          resyncTimerRef.current = window.setTimeout(() => {
            resyncTimerRef.current = null;
            void fetchRosterRequest();
          }, 50);
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
            reason:
              typeof state.reason === "string"
                ? state.reason
                : "Attention required",
          });
        }

        let seq = snapshotSeq;
        for (const event of matchingEvents
          .filter((candidate) => candidate.seq > snapshotSeq)
          .sort((left, right) => left.seq - right.seq)) {
          applyAttentionEvent(entries, event);
          seq = Math.max(seq, event.seq);
        }
        cursorRef.current = { epoch: data.epoch, seq };
        entriesRef.current = entries;
        resyncAttemptedRef.current = false;
        setAttentionBySession(summarizeAttention(entries));
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") {
          return;
        }
        console.error("Failed to fetch attention roster:", error);
      } finally {
        fetchInFlightRef.current = false;
      }
    },
    [],
  );

  const handleAttentionEvent = useCallback(
    (data: Record<string, unknown>) => {
      const attentionEvent = parseAttentionEvent(data);
      if (attentionEvent === null) return;

      const cursor = cursorRef.current;
      if (cursor === null) {
        bufferedEventsRef.current.push(attentionEvent);
        if (!fetchInFlightRef.current) void fetchAttentionRoster();
      } else if (fetchInFlightRef.current) {
        bufferedEventsRef.current.push(attentionEvent);
      } else if (attentionEvent.epoch !== cursor.epoch) {
        bufferedEventsRef.current = [attentionEvent];
        cursorRef.current = null;
        resyncAttemptedRef.current = false;
        void fetchAttentionRoster();
      } else if (attentionEvent.seq > cursor.seq) {
        const entries = new Map(entriesRef.current);
        applyAttentionEvent(entries, attentionEvent);
        entriesRef.current = entries;
        cursorRef.current = {
          epoch: cursor.epoch,
          seq: attentionEvent.seq,
        };
        setAttentionBySession(summarizeAttention(entries));
      }
    },
    [fetchAttentionRoster],
  );

  // Register before the initial fetch so attention transitions can be buffered.
  useWebSocketEvent("agent_event", handleAttentionEvent);

  useEffect(() => {
    void fetchAttentionRoster();
    const refreshTimer = window.setInterval(() => {
      void fetchAttentionRoster();
    }, ATTENTION_ROSTER_REFRESH_MS);
    return () => {
      clearInterval(refreshTimer);
    };
  }, [fetchAttentionRoster]);

  useEffect(() => {
    const reconnected = wsConnected && !wasConnectedRef.current;
    wasConnectedRef.current = wsConnected;
    if (!reconnected) return;

    cursorRef.current = null;
    resyncAttemptedRef.current = false;
    void fetchAttentionRoster();
  }, [fetchAttentionRoster, wsConnected]);

  useEffect(() => {
    return () => {
      abortRef.current?.abort();
      if (resyncTimerRef.current !== null) {
        clearTimeout(resyncTimerRef.current);
      }
    };
  }, []);

  return { attentionBySession, fetchAttentionRoster };
}
