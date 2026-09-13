import { useCallback, useEffect, useRef, useState } from "react";

import { createTerminalWsReducer } from "./terminalWsFragments";
import {
  applyRosterPage,
  mergeRosterRows,
  rosterPageRequestId,
  type CreatedTmuxSession,
  type RosterWalk,
  type TmuxSession,
  type TmuxTarget,
} from "./terminalRosterSnapshot";
import type { WriteSettlementState } from "./terminalWriteSettlement";
import {
  EMPTY_CONTROL_LEASE,
  createControlLease,
  type ControlLease,
  type ControlLeaseSnapshot,
  type PendingWrite,
} from "./terminalControlLease";
import {
  createTerminalOutputSink,
  type TerminalAttachHistory,
} from "./terminalOutputSink";
import {
  terminalAttachMessage,
  terminalCreateMessage,
  terminalDetachMessage,
  terminalKillMessage,
  terminalListMessage,
  terminalResizeMessage,
  terminalSetViewportMessage,
} from "./tmuxSessionMessages";

export const TMUX_REQUEST_TIMEOUT_MS = 10_000;
export const TMUX_RECONNECT_BASE_MS = 2_000;
export const TMUX_RECONNECT_MAX_MS = 30_000;
export const TMUX_STABLE_OPEN_MS = 1_000;

type PendingRequest =
  | {
      kind: "attach";
      requestId: string;
      generation: number;
      target: TmuxTarget;
    }
  | {
      kind: "detach";
      requestId: string;
      generation: number;
      nextTarget: TmuxTarget | null;
    }
  | {
      kind: "create";
      requestId: string;
      generation: number;
    };

interface TmuxSessionsResult {
  sessions: TmuxSession[];
  connected: boolean;
  sessionsLoaded: boolean;
  attachedTarget: TmuxTarget | null;
  streamingId: string | null;
  isLoading: boolean;
  sessionEnded: boolean;
  requestPending: boolean;
  attachError: string | null;
  createdSession: CreatedTmuxSession | null;
  attachSession: (sessionName: string, socket: string) => void;
  detachSession: () => void;
  clearAttachError: () => void;
  refreshTerminal: (sessionName: string, socket: string) => void;
  createSession: (name?: string, socket?: string) => void;
  killSession: (terminalId: string) => void;
  refreshSessions: () => void;
  dismissEndedSession: () => void;
  /** True only while this attachment holds the daemon's writer lease. */
  hasControl: boolean;
  controlPending: boolean;
  /** Another session displaced this attachment, so the pane says so. */
  leaseLost: boolean;
  /** The one keystroke or paste held behind an in-flight take-control. */
  pendingWrite: PendingWrite | null;
  /** Why the last write was refused, shown as a non-colour cue. */
  writeRefusal: string | null;
  writeSettlement: WriteSettlementState;
  takeControl: (options?: { takeover?: boolean }) => void;
  releaseControl: () => void;
  sendInput: (data: string) => void;
  sendPaste: (text: string) => void;
  retryWrite: (attachmentId: string, seq: number) => void;
  discardWrite: (attachmentId: string, seq: number) => void;
  dismissWriteRefusal: () => void;
  resizeTerminal: (rows: number, cols: number) => void;
  onOutput: (callback: (runId: string, data: string) => void) => void;
  onAttachHistory: (callback: (history: TerminalAttachHistory) => void) => void;
}

export function useTmuxSessions(
  projectId: string | null = null,
): TmuxSessionsResult {
  const [sessions, setSessions] = useState<TmuxSession[]>([]);
  const [connected, setConnected] = useState(false);
  const [sessionsLoaded, setSessionsLoaded] = useState(false);
  const [attachedTarget, setAttachedTarget] = useState<TmuxTarget | null>(null);
  const [streamingId, setStreamingId] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [sessionEnded, setSessionEnded] = useState(false);
  const [requestPending, setRequestPending] = useState(false);
  const [attachError, setAttachError] = useState<string | null>(null);
  const [createdSession, setCreatedSession] =
    useState<CreatedTmuxSession | null>(null);
  const [lease, setLease] = useState<ControlLeaseSnapshot>(EMPTY_CONTROL_LEASE);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<number | null>(null);
  const reconnectAttemptsRef = useRef(0);
  const stableOpenTimeoutRef = useRef<number | null>(null);
  // Cursor state is keyed to one walk (a fresh init/refresh listing plus its
  // continuation pages); a superseded walk's pages must not merge rows or
  // steer the cursor, or a refresh started mid-pagination under-fetches.
  const listWalkRef = useRef<RosterWalk | null>(null);
  const attachedTargetRef = useRef<TmuxTarget | null>(null);
  const streamingIdRef = useRef<string | null>(null);
  const connectionGenerationRef = useRef(0);
  const requestCounterRef = useRef(0);
  const pendingRequestRef = useRef<PendingRequest | null>(null);
  const pendingRequestTimeoutRef = useRef<number | null>(null);
  const connectRef = useRef<() => void>(() => {});
  const fragmentReducerRef = useRef(createTerminalWsReducer());
  const projectIdRef = useRef<string | null>(projectId);

  // The lease owns the whole control and write plane; the hook keeps one
  // mirrored snapshot so the view re-renders when the lease moves. Both live
  // in lazy state rather than a ref: they are read during render.
  /* eslint-disable react-hooks/refs -- the lease stores these getters and
     calls them from socket callbacks and timers; the lazy initializer only
     hands them over, so no ref is read during render. */
  const [control] = useState<ControlLease>(() =>
    createControlLease({
      socket: () => wsRef.current,
      attachmentId: () => streamingIdRef.current,
      terminalId: () => attachedTargetRef.current?.terminal_id,
      connectionGeneration: () => connectionGenerationRef.current,
      requestTimeoutMs: TMUX_REQUEST_TIMEOUT_MS,
      onChange: setLease,
    }),
  );
  /* eslint-enable react-hooks/refs */

  const [sink] = useState(createTerminalOutputSink);

  const updateAttachment = useCallback(
    (target: TmuxTarget | null, streamId: string | null) => {
      attachedTargetRef.current = target;
      streamingIdRef.current = streamId;
      setAttachedTarget(target);
      setStreamingId(streamId);
    },
    [],
  );

  const clearPendingRequest = useCallback(() => {
    if (pendingRequestTimeoutRef.current !== null) {
      clearTimeout(pendingRequestTimeoutRef.current);
      pendingRequestTimeoutRef.current = null;
    }
    pendingRequestRef.current = null;
    setRequestPending(false);
    setIsLoading(false);
  }, []);

  /** The outstanding request a result answers, or null when it answers none. */
  const matchPending = useCallback(
    (requestId: unknown): PendingRequest | null => {
      const pending = pendingRequestRef.current;
      if (
        !pending ||
        pending.requestId !== requestId ||
        pending.generation !== connectionGenerationRef.current
      ) {
        return null;
      }
      return pending;
    },
    [],
  );

  const schedulePendingRequestTimeout = useCallback(
    (request: PendingRequest) => {
      if (pendingRequestTimeoutRef.current !== null) {
        clearTimeout(pendingRequestTimeoutRef.current);
      }
      pendingRequestTimeoutRef.current = window.setTimeout(() => {
        pendingRequestTimeoutRef.current = null;
        const pending = pendingRequestRef.current;
        if (
          pending?.requestId !== request.requestId ||
          pending.generation !== request.generation ||
          pending.kind !== request.kind
        ) {
          return;
        }
        pendingRequestRef.current = null;
        setRequestPending(false);
        setIsLoading(false);
        // Rendered after a full stop ("Couldn't attach to this terminal. "),
        // so it has to stand on its own as a sentence.
        setAttachError(
          `${request.kind[0].toUpperCase()}${request.kind.slice(1)} request timed out.`,
        );
      }, TMUX_REQUEST_TIMEOUT_MS);
    },
    [],
  );

  const dismissEndedSession = useCallback(() => {
    setSessionEnded(false);
    updateAttachment(null, null);
  }, [updateAttachment]);

  const refreshSessions = useCallback(() => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(
      terminalListMessage(
        `refresh-${++requestCounterRef.current}`,
        projectIdRef.current,
      ),
    );
  }, []);

  const beginAttachRequest = useCallback(
    (target: TmuxTarget): boolean => {
      const ws = wsRef.current;
      if (pendingRequestRef.current || !ws || ws.readyState !== WebSocket.OPEN)
        return false;

      const generation = connectionGenerationRef.current;
      const requestId = `attach-${generation}-${++requestCounterRef.current}`;
      const request: PendingRequest = {
        kind: "attach",
        requestId,
        generation,
        target,
      };
      pendingRequestRef.current = request;
      setRequestPending(true);
      setIsLoading(true);
      setSessionEnded(false);
      setAttachError(null);
      ws.send(terminalAttachMessage(requestId, target.terminal_id));
      schedulePendingRequestTimeout(request);
      return true;
    },
    [schedulePendingRequestTimeout],
  );

  const beginDetachRequest = useCallback(
    (nextTarget: TmuxTarget | null): boolean => {
      const ws = wsRef.current;
      const currentStreamingId = streamingIdRef.current;
      if (
        pendingRequestRef.current ||
        !ws ||
        ws.readyState !== WebSocket.OPEN ||
        !currentStreamingId
      )
        return false;

      const generation = connectionGenerationRef.current;
      const requestId = `detach-${generation}-${++requestCounterRef.current}`;
      const request: PendingRequest = {
        kind: "detach",
        requestId,
        generation,
        nextTarget,
      };
      pendingRequestRef.current = request;
      setRequestPending(true);
      setIsLoading(true);
      setAttachError(null);
      ws.send(
        terminalDetachMessage(
          requestId,
          attachedTargetRef.current?.terminal_id,
          currentStreamingId,
        ),
      );
      schedulePendingRequestTimeout(request);
      return true;
    },
    [schedulePendingRequestTimeout],
  );

  const handleMessage = useCallback(
    (data: Record<string, unknown>) => {
      if (control.handle(data) || sink.handle(data)) return;
      if (data.type === "terminal_attachment_finalized") {
        const finalizedId = data.attachment_id;
        if (typeof finalizedId === "string") {
          fragmentReducerRef.current.finalize(finalizedId);
          control.forget(finalizedId);
        }
      }
      switch (data.type) {
        case "terminal_list": {
          const page = applyRosterPage(listWalkRef.current, data);
          if (page === null) break;
          listWalkRef.current = page.walk;
          // A fresh listing replaces the table so terminals that vanished
          // drop out; only continuation pages merge.
          if (page.replace) {
            setSessions(page.rows);
          } else {
            setSessions((current) => mergeRosterRows(current, page.rows));
          }
          setSessionsLoaded(true);
          const attached = attachedTargetRef.current;
          if (
            attached &&
            !page.rows.some(
              (session) => session.terminal_id === attached.terminal_id,
            ) &&
            page.lastPage
          ) {
            setSessionEnded(true);
          }
          if (page.nextCursor !== null) {
            wsRef.current?.send(
              terminalListMessage(
                rosterPageRequestId(page.walk.id),
                projectIdRef.current,
                page.nextCursor,
              ),
            );
          }
          if (pendingRequestRef.current === null) setIsLoading(false);
          break;
        }

        case "terminal_attach_result": {
          const pending = matchPending(data.request_id);
          if (pending?.kind !== "attach") break;

          // Failure frames also carry attachment_id (the finalized lease), so
          // only an explicit success verdict may mark the attach live.
          if (data.success === true && typeof data.attachment_id === "string") {
            const attachedId = data.attachment_id;
            fragmentReducerRef.current.markLive(attachedId);
            // Every attach is observe-only; take-control is the sole grant.
            control.observe(attachedId);
            updateAttachment(pending.target, attachedId);
          } else {
            const reason =
              typeof data.reason === "string"
                ? data.reason
                : typeof data.message === "string"
                  ? data.message
                  : typeof data.code === "string"
                    ? `Attach failed: ${data.code}`
                    : "Attach failed";
            setAttachError(reason);
          }
          clearPendingRequest();
          break;
        }

        case "terminal_detach_result": {
          const pending = matchPending(data.request_id);
          if (pending?.kind !== "detach") break;

          const { nextTarget } = pending;
          if (data.success) {
            updateAttachment(null, null);
            clearPendingRequest();
            if (nextTarget) beginAttachRequest(nextTarget);
          } else {
            setAttachError(
              typeof data.message === "string" ? data.message : "Detach failed",
            );
            clearPendingRequest();
          }
          break;
        }

        case "error": {
          if (matchPending(data.request_id) === null) break;

          setAttachError(
            typeof data.message === "string"
              ? data.message
              : "Terminal request failed",
          );
          clearPendingRequest();
          break;
        }

        case "terminal_create_result": {
          if (matchPending(data.request_id)?.kind !== "create") break;

          if (data.success && typeof data.terminal_id === "string") {
            setCreatedSession({
              terminal_id: data.terminal_id,
            });
            refreshSessions();
          } else {
            setAttachError(
              typeof data.reason === "string" ? data.reason : "Create failed",
            );
          }
          clearPendingRequest();
          break;
        }

        case "terminal_kill_result":
          refreshSessions();
          if (pendingRequestRef.current === null) setIsLoading(false);
          break;

        case "terminal_event":
          refreshSessions();
          break;

        case "session_event":
          if (data.event === "session_updated") {
            refreshSessions();
          }
          break;

      }
    },
    [
      beginAttachRequest,
      clearPendingRequest,
      control,
      matchPending,
      refreshSessions,
      sink,
      updateAttachment,
    ],
  );

  const handleMessageRef = useRef(handleMessage);
  const updateAttachmentRef = useRef(updateAttachment);
  useEffect(() => {
    handleMessageRef.current = handleMessage;
    updateAttachmentRef.current = updateAttachment;
  }, [handleMessage, updateAttachment]);

  const connect = useCallback(() => {
    if (
      wsRef.current?.readyState === WebSocket.OPEN ||
      wsRef.current?.readyState === WebSocket.CONNECTING
    )
      return;

    const isSecure = window.location.protocol === "https:";
    const wsUrl = isSecure
      ? `wss://${window.location.host}/ws`
      : `ws://${window.location.host}/ws`;

    const generation = ++connectionGenerationRef.current;
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;

    const isCurrentConnection = () =>
      connectionGenerationRef.current === generation && wsRef.current === ws;

    ws.onopen = () => {
      if (!isCurrentConnection()) return;
      // Accept-then-close (e.g. 4401 after accept) must not reset backoff;
      // only a socket that stays open counts as a recovered connection.
      if (stableOpenTimeoutRef.current !== null) {
        clearTimeout(stableOpenTimeoutRef.current);
      }
      stableOpenTimeoutRef.current = window.setTimeout(() => {
        stableOpenTimeoutRef.current = null;
        if (!isCurrentConnection()) return;
        reconnectAttemptsRef.current = 0;
      }, TMUX_STABLE_OPEN_MS);
      setConnected(true);
      ws.send(
        JSON.stringify({
          type: "subscribe",
          events: ["terminal_output", "terminal_event", "session_event"],
        }),
      );
      // Fetch session list on connect
      ws.send(terminalListMessage("init", projectIdRef.current));
    };

    ws.onclose = () => {
      if (!isCurrentConnection()) return;
      if (stableOpenTimeoutRef.current !== null) {
        clearTimeout(stableOpenTimeoutRef.current);
        stableOpenTimeoutRef.current = null;
      }
      setConnected(false);
      setSessionsLoaded(false);
      listWalkRef.current = null;
      fragmentReducerRef.current.disconnect();
      fragmentReducerRef.current = createTerminalWsReducer();
      updateAttachmentRef.current(null, null);
      if (pendingRequestTimeoutRef.current !== null) {
        clearTimeout(pendingRequestTimeoutRef.current);
        pendingRequestTimeoutRef.current = null;
      }
      pendingRequestRef.current = null;
      setRequestPending(false);
      setIsLoading(false);
      setAttachError(null);
      setCreatedSession(null);
      // The old attachment ids are dead, so the lease, the pending slot and
      // every unsettled write go with them; the pane comes back observe-only.
      control.reset("Reconnected before the keystroke was sent.");
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
      const attempt = reconnectAttemptsRef.current;
      reconnectAttemptsRef.current = attempt + 1;
      const delay = Math.min(
        TMUX_RECONNECT_BASE_MS * 2 ** attempt,
        TMUX_RECONNECT_MAX_MS,
      );
      reconnectTimeoutRef.current = window.setTimeout(() => {
        if (!isCurrentConnection()) return;
        reconnectTimeoutRef.current = null;
        connectRef.current();
      }, delay);
    };

    ws.onerror = (error) => {
      if (!isCurrentConnection()) return;
      console.error("Tmux WebSocket error:", error);
    };

    ws.onmessage = (event) => {
      if (!isCurrentConnection()) return;
      try {
        const data = JSON.parse(event.data);
        if (data.type === "terminal_ws_fragment") {
          // Fragments reassemble into whole messages before dispatch, so the
          // handler itself never recurses.
          const reducer = fragmentReducerRef.current;
          reducer.push(data);
          for (const applied of reducer.applied.splice(0)) {
            handleMessageRef.current(applied);
          }
          return;
        }
        handleMessageRef.current(data);
      } catch (e) {
        console.error("Failed to parse tmux message:", e);
      }
    };
  }, [control]);

  useEffect(() => {
    connectRef.current = connect;
  }, [connect]);

  const attachSession = useCallback(
    (sessionName: string, socket: string) => {
      if (pendingRequestRef.current) return;
      const target = { terminal_id: sessionName || socket };
      const currentTarget = attachedTargetRef.current;
      if (currentTarget?.terminal_id === target.terminal_id) return;
      if (currentTarget) {
        beginDetachRequest(target);
        return;
      }
      beginAttachRequest(target);
    },
    [beginAttachRequest, beginDetachRequest],
  );

  const detachSession = useCallback(() => {
    beginDetachRequest(null);
  }, [beginDetachRequest]);

  const clearAttachError = useCallback(() => setAttachError(null), []);

  const refreshTerminal = useCallback((sessionName: string, socket: string) => {
    const ws = wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(
      terminalSetViewportMessage(
        `refresh-${connectionGenerationRef.current}-${++requestCounterRef.current}`,
        sessionName || socket,
      ),
    );
  }, []);

  const createSession = useCallback(
    (name?: string, socket?: string) => {
      const ws = wsRef.current;
      if (pendingRequestRef.current || !ws || ws.readyState !== WebSocket.OPEN)
        return;

      const generation = connectionGenerationRef.current;
      const requestId = `create-${generation}-${++requestCounterRef.current}`;
      const request: PendingRequest = { kind: "create", requestId, generation };
      pendingRequestRef.current = request;
      setRequestPending(true);
      setIsLoading(true);
      setAttachError(null);
      setCreatedSession(null);
      ws.send(
        terminalCreateMessage(requestId, projectIdRef.current, name, socket),
      );
      schedulePendingRequestTimeout(request);
    },
    [schedulePendingRequestTimeout],
  );

  const killSession = useCallback(
    (terminalId: string) => {
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      setIsLoading(true);
      const currentTarget = attachedTargetRef.current;
      if (currentTarget?.terminal_id === terminalId) {
        updateAttachment(null, null);
      }
      ws.send(terminalKillMessage(`kill-${Date.now()}`, terminalId));
    },
    [updateAttachment],
  );

  const takeControl = useCallback(
    (options?: { takeover?: boolean }) => control.take(options?.takeover === true),
    [control],
  );

  const releaseControl = useCallback(() => control.release(), [control]);

  const sendInput = useCallback(
    (data: string) => control.write("input", data),
    [control],
  );

  const sendPaste = useCallback(
    (text: string) => control.write("paste", text),
    [control],
  );

  const retryWrite = useCallback(
    (attachmentId: string, seq: number) => control.retry(attachmentId, seq),
    [control],
  );

  const discardWrite = useCallback(
    (attachmentId: string, seq: number) => control.discard(attachmentId, seq),
    [control],
  );

  const dismissWriteRefusal = useCallback(
    () => control.dismissRefusal(),
    [control],
  );

  const hasControl = lease.holder !== null && lease.holder === streamingId;

  const resizeTerminal = useCallback((rows: number, cols: number) => {
    const currentStreamingId = streamingIdRef.current;
    if (
      !wsRef.current ||
      wsRef.current.readyState !== WebSocket.OPEN ||
      !currentStreamingId
    )
      return;
    wsRef.current.send(
      terminalResizeMessage(
        attachedTargetRef.current?.terminal_id,
        currentStreamingId,
        rows,
        cols,
      ),
    );
  }, []);

  useEffect(() => {
    connectRef.current();
    return () => {
      connectionGenerationRef.current += 1;
      reconnectAttemptsRef.current = 0;
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
        reconnectTimeoutRef.current = null;
      }
      if (stableOpenTimeoutRef.current !== null) {
        clearTimeout(stableOpenTimeoutRef.current);
        stableOpenTimeoutRef.current = null;
      }
      if (pendingRequestTimeoutRef.current !== null) {
        clearTimeout(pendingRequestTimeoutRef.current);
        pendingRequestTimeoutRef.current = null;
      }
      pendingRequestRef.current = null;
      const ws = wsRef.current;
      wsRef.current = null;
      ws?.close();
    };
  }, []);

  // The project picker changed: list again under the new scope.
  useEffect(() => {
    projectIdRef.current = projectId;
    refreshSessions();
  }, [projectId, refreshSessions]);

  // Refresh session list when browser tab becomes visible (catches missed events)
  useEffect(() => {
    const handleVisibility = () => {
      if (document.visibilityState === "visible") {
        refreshSessions();
      }
    };
    document.addEventListener("visibilitychange", handleVisibility);
    return () =>
      document.removeEventListener("visibilitychange", handleVisibility);
  }, [refreshSessions]);

  return {
    sessions,
    connected,
    sessionsLoaded,
    attachedTarget,
    streamingId,
    isLoading,
    sessionEnded,
    requestPending,
    attachError,
    createdSession,
    attachSession,
    detachSession,
    clearAttachError,
    refreshTerminal,
    createSession,
    killSession,
    refreshSessions,
    dismissEndedSession,
    hasControl,
    controlPending: lease.pending,
    leaseLost: lease.lost,
    pendingWrite: lease.pendingWrite,
    writeRefusal: lease.refusal,
    writeSettlement: lease.settlement,
    takeControl,
    releaseControl,
    sendInput,
    sendPaste,
    retryWrite,
    discardWrite,
    dismissWriteRefusal,
    resizeTerminal,
    onOutput: sink.onOutput,
    onAttachHistory: sink.onAttachHistory,
  };
}
