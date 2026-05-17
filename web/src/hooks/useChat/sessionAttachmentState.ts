import { useCallback, useRef, useState } from "react";
import type {
  SessionInteractionMode,
  SessionObservationMeta,
} from "../../types/chat";
import {
  loadViewingSessionId,
  loadViewingSessionMode,
  type ContinuationRollbackSnapshot,
  type PendingProxyMessage,
} from "./core";

export function useSessionAttachmentState() {
  const [viewingSessionId, setViewingSessionId] = useState<string | null>(() =>
    loadViewingSessionId(),
  );
  const viewingSessionIdRef = useRef<string | null>(null);
  const [viewingSessionMeta, setViewingSessionMeta] =
    useState<SessionObservationMeta | null>(null);
  const viewingSessionMetaRef = useRef<SessionObservationMeta | null>(null);
  const initialViewingSessionIdRef = useRef<string | null>(
    loadViewingSessionId(),
  );
  // Keep this widened to the full interaction mode: persisted proxy-attached
  // terminal sessions must restore as proxy, not collapse to read-only.
  const initialViewingModeRef = useRef<SessionInteractionMode>(
    loadViewingSessionMode(),
  );
  const initialViewingRestoreRef = useRef(false);
  const initialViewingReconnectRetryRef = useRef(false);

  const [observedSessionId, setObservedSessionId] = useState<string | null>(
    null,
  );
  const observedSessionIdRef = useRef<string | null>(null);
  const observedSessionMetaRef = useRef<SessionObservationMeta | null>(null);
  const [sessionInteractionMode, setSessionInteractionMode] =
    useState<SessionInteractionMode>("none");
  const sessionInteractionModeRef = useRef<SessionInteractionMode>("none");
  const pendingSessionInteractionModeRef = useRef<"observe" | "proxy">(
    "proxy",
  );
  const [proxyDeliveryNotice, setProxyDeliveryNotice] = useState<string | null>(
    null,
  );
  const agentNameCacheRef = useRef<Map<string, string | null>>(new Map());

  const [attachedSessionId, setAttachedSessionId] = useState<string | null>(
    null,
  );
  const attachedSessionIdRef = useRef<string | null>(null);
  const [attachedSessionMeta, setAttachedSessionMeta] =
    useState<SessionObservationMeta | null>(null);
  const attachedSessionMetaRef = useRef<SessionObservationMeta | null>(null);
  const pendingProxyMessagesRef = useRef<Map<string, PendingProxyMessage>>(
    new Map(),
  );
  const pendingProxySessionQueuesRef = useRef<Map<string, string[]>>(new Map());
  const [isContinuingSession, setIsContinuingSession] = useState(false);
  const continuingSessionIdRef = useRef<string | null>(null);
  const continuationRollbackRef = useRef<ContinuationRollbackSnapshot | null>(
    null,
  );

  const clearContinuingSession = useCallback(() => {
    continuingSessionIdRef.current = null;
    setIsContinuingSession(false);
  }, []);

  const clearContinuationRollback = useCallback(() => {
    continuationRollbackRef.current = null;
  }, []);

  const resolveAgentName = useCallback(async (agentRunId: string) => {
    const cached = agentNameCacheRef.current.get(agentRunId);
    if (cached !== undefined) {
      return cached;
    }

    const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
    try {
      const res = await fetch(`${baseUrl}/api/agents/runs/${agentRunId}`);
      if (!res.ok) {
        agentNameCacheRef.current.set(agentRunId, null);
        return null;
      }
      const data = await res.json();
      const resolved =
        data?.run?.agent_name || data?.run?.workflow_name || null;
      agentNameCacheRef.current.set(agentRunId, resolved);
      return resolved;
    } catch {
      agentNameCacheRef.current.set(agentRunId, null);
      return null;
    }
  }, []);

  return {
    attachedSessionId,
    attachedSessionIdRef,
    attachedSessionMeta,
    attachedSessionMetaRef,
    clearContinuationRollback,
    clearContinuingSession,
    continuingSessionIdRef,
    continuationRollbackRef,
    initialViewingModeRef,
    initialViewingReconnectRetryRef,
    initialViewingRestoreRef,
    initialViewingSessionIdRef,
    isContinuingSession,
    observedSessionId,
    observedSessionIdRef,
    observedSessionMetaRef,
    pendingProxyMessagesRef,
    pendingProxySessionQueuesRef,
    pendingSessionInteractionModeRef,
    proxyDeliveryNotice,
    resolveAgentName,
    sessionInteractionMode,
    sessionInteractionModeRef,
    setAttachedSessionId,
    setAttachedSessionMeta,
    setIsContinuingSession,
    setObservedSessionId,
    setProxyDeliveryNotice,
    setSessionInteractionMode,
    setViewingSessionId,
    setViewingSessionMeta,
    viewingSessionId,
    viewingSessionIdRef,
    viewingSessionMeta,
    viewingSessionMetaRef,
  };
}
