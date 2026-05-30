import { useCallback, useEffect, useRef, useState } from "react";
import type { ContextUsage } from "../../types/chat";

export const LIVE_CONTEXT_USAGE_TTL_MS = 10 * 60 * 1000;
export const LIVE_CONTEXT_USAGE_MAX_ENTRIES = 200;

export interface LiveContextUsageEntry {
  usageTimestamp: number;
  lastSeenAt: number;
}

export function createEmptyContextUsage(): ContextUsage {
  return {
    totalInputTokens: 0,
    outputTokens: 0,
    contextWindow: null,
    uncachedInputTokens: 0,
    cacheReadTokens: 0,
    cacheCreationTokens: 0,
  };
}

export function pruneLiveContextUsageEntries(
  entries: Map<string, LiveContextUsageEntry>,
  now = Date.now(),
): void {
  for (const [sessionId, entry] of entries) {
    if (now - entry.lastSeenAt > LIVE_CONTEXT_USAGE_TTL_MS) {
      entries.delete(sessionId);
    }
  }
  if (entries.size <= LIVE_CONTEXT_USAGE_MAX_ENTRIES) return;
  const staleFirst = [...entries.entries()].sort(
    (left, right) => left[1].lastSeenAt - right[1].lastSeenAt,
  );
  for (const [sessionId] of staleFirst.slice(
    0,
    entries.size - LIVE_CONTEXT_USAGE_MAX_ENTRIES,
  )) {
    entries.delete(sessionId);
  }
}

export function useContextUsageState() {
  const [contextUsage, setContextUsage] = useState<ContextUsage>(() =>
    createEmptyContextUsage(),
  );
  const [contextUsageUpdatedAt, setContextUsageUpdatedAt] = useState<
    number | null
  >(null);
  const preAttachContextUsageRef = useRef<ContextUsage | null>(null);
  const didTrackContextUsageRef = useRef(false);
  const lastLiveUsageBySessionRef = useRef<Map<string, LiveContextUsageEntry>>(
    new Map(),
  );

  const clearPreAttachContextUsage = useCallback(() => {
    preAttachContextUsageRef.current = null;
  }, []);

  const markSessionUsageFresh = useCallback(
    (sessionId: string, rawTimestamp?: string) => {
      const parsed = rawTimestamp ? new Date(rawTimestamp).getTime() : NaN;
      const now = Date.now();
      // Live websocket usage beats older hydrated session snapshots.
      lastLiveUsageBySessionRef.current.set(
        sessionId,
        {
          usageTimestamp:
            Number.isFinite(parsed) && parsed >= 0 && parsed <= now
              ? parsed
              : now,
          lastSeenAt: now,
        },
      );
      pruneLiveContextUsageEntries(lastLiveUsageBySessionRef.current, now);
    },
    [],
  );

  const shouldApplyHydratedUsage = useCallback(
    (sessionId: string, fetchStartedAt: number) => {
      pruneLiveContextUsageEntries(lastLiveUsageBySessionRef.current);
      const lastLive = lastLiveUsageBySessionRef.current.get(sessionId);
      return lastLive == null || lastLive.usageTimestamp <= fetchStartedAt;
    },
    [],
  );

  useEffect(() => {
    if (!didTrackContextUsageRef.current) {
      didTrackContextUsageRef.current = true;
      return;
    }
    setContextUsageUpdatedAt(Date.now());
  }, [contextUsage]);

  return {
    clearPreAttachContextUsage,
    contextUsage,
    contextUsageUpdatedAt,
    markSessionUsageFresh,
    preAttachContextUsageRef,
    setContextUsage,
    shouldApplyHydratedUsage,
  };
}
