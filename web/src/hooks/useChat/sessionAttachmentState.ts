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

const AGENT_NAME_RESOLVE_TIMEOUT_MS = 5_000;
const AGENT_NAME_RESOLVE_SUCCESS_TTL_MS = 10 * 60_000;
const AGENT_NAME_RESOLVE_FAILURE_TTL_MS = 30_000;

interface AgentNameCacheEntry {
  value: string | null;
  expiresAt: number | null;
}

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
  const agentNameCacheRef = useRef<Map<string, AgentNameCacheEntry>>(new Map());
  const agentNameInflightRef = useRef<Map<string, Promise<string | null>>>(
    new Map(),
  );

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
      if (cached.expiresAt === null || cached.expiresAt > Date.now()) {
        return cached.value;
      }
      agentNameCacheRef.current.delete(agentRunId);
    }

    const inflight = agentNameInflightRef.current.get(agentRunId);
    if (inflight) {
      return inflight;
    }

    const promise = (async () => {
      const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
      const controller = new AbortController();
      const timeout = globalThis.setTimeout(
        () => controller.abort(),
        AGENT_NAME_RESOLVE_TIMEOUT_MS,
      );
      try {
        const res = await fetch(`${baseUrl}/api/agents/runs/${agentRunId}`, {
          credentials: "include",
          signal: controller.signal,
        });
        if (!res.ok) {
          console.warn("Failed to resolve agent name", {
            agentRunId,
            status: res.status,
          });
          agentNameCacheRef.current.set(agentRunId, {
            value: null,
            expiresAt: Date.now() + AGENT_NAME_RESOLVE_FAILURE_TTL_MS,
          });
          return null;
        }
        const body = await res.text();
        let data: unknown;
        try {
          data = JSON.parse(body);
        } catch (error) {
          console.warn("Failed to parse agent name response", { agentRunId, error });
          agentNameCacheRef.current.set(agentRunId, {
            value: null,
            expiresAt: Date.now() + AGENT_NAME_RESOLVE_FAILURE_TTL_MS,
          });
          return null;
        }
        const run = data && typeof data === "object" && "run" in data
          ? (data as { run?: unknown }).run
          : null;
        const resolved = run && typeof run === "object"
          ? (run as { agent_name?: unknown; workflow_name?: unknown }).agent_name
            || (run as { agent_name?: unknown; workflow_name?: unknown }).workflow_name
            || null
          : null;
        if (resolved) {
          agentNameCacheRef.current.set(agentRunId, {
            value: String(resolved),
            expiresAt: Date.now() + AGENT_NAME_RESOLVE_SUCCESS_TTL_MS,
          });
        } else {
          agentNameCacheRef.current.set(agentRunId, {
            value: null,
            expiresAt: Date.now() + AGENT_NAME_RESOLVE_FAILURE_TTL_MS,
          });
        }
        return resolved ? String(resolved) : null;
      } catch (error) {
        console.warn("Failed to resolve agent name", { agentRunId, error });
        agentNameCacheRef.current.set(agentRunId, {
          value: null,
          expiresAt: Date.now() + AGENT_NAME_RESOLVE_FAILURE_TTL_MS,
        });
        return null;
      } finally {
        globalThis.clearTimeout(timeout);
      }
    })();
    agentNameInflightRef.current.set(agentRunId, promise);
    try {
      return await promise;
    } finally {
      agentNameInflightRef.current.delete(agentRunId);
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
