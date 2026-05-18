import { useCallback, useEffect, useRef, useState } from "react";
import type { ContextUsage } from "./core";

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

export function useContextUsageState() {
  const [contextUsage, setContextUsage] = useState<ContextUsage>(() =>
    createEmptyContextUsage(),
  );
  const [contextUsageUpdatedAt, setContextUsageUpdatedAt] = useState<
    number | null
  >(null);
  const preAttachContextUsageRef = useRef<ContextUsage | null>(null);
  const didTrackContextUsageRef = useRef(false);
  const lastLiveUsageBySessionRef = useRef<Map<string, number>>(new Map());

  const clearPreAttachContextUsage = useCallback(() => {
    preAttachContextUsageRef.current = null;
  }, []);

  const markSessionUsageFresh = useCallback(
    (sessionId: string, rawTimestamp?: string) => {
      const parsed = rawTimestamp ? new Date(rawTimestamp).getTime() : NaN;
      const now = Date.now();
      lastLiveUsageBySessionRef.current.set(
        sessionId,
        Number.isFinite(parsed) && parsed >= 0 && parsed <= now ? parsed : now,
      );
    },
    [],
  );

  const shouldApplyHydratedUsage = useCallback(
    (sessionId: string, fetchStartedAt: number) => {
      const lastLive = lastLiveUsageBySessionRef.current.get(sessionId);
      return lastLive == null || lastLive <= fetchStartedAt;
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
