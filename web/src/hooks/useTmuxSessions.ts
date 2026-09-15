import { useCallback, useEffect, useRef, useState } from "react";

import { createAttachmentReadiness } from "./terminalAttachmentReadiness";
import {
  createPendingRequestSlot,
  type PendingRequest,
} from "./terminalPendingRequest";
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
/**
 * How often accumulated fragment buffers are swept. Well under the reassembly
 * timeout so a stalled message is dropped near its deadline rather than a
 * sweep-interval later.
 */
export const TERMINAL_FRAGMENT_TICK_MS = 1_000;

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
  /**
   * The renderer measured its grid. Half of the viewport rendezvous: the
   * daemon is told the size only once an attachment exists to address it to.
   */
  reportViewport: (rows: number, cols: number) => void;
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
  const connectRef = useRef<() => void>(() => {});
  const fragmentReducerRef = useRef(createTerminalWsReducer());
  const readinessRef = useRef(createAttachmentReadiness());
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
  // One request at a time. The slot mirrors itself into requestPending and
  // isLoading so the view can render the wait and the timeout.
  const [pendingRequest] = useState(() =>
    createPendingRequestSlot({
      generation: () => connectionGenerationRef.current,
      timeoutMs: TMUX_REQUEST_TIMEOUT_MS,
      onChange: (pending) => {
        setRequestPending(pending);
        setIsLoading(pending);
      },
      onTimeout: setAttachError,
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
      // Detach, disconnect and a vanished session all land here, and a
      // rendezvous left armed would fire its viewport at whatever attaches
      // next. Guarding the one choke point covers all three.
      if (streamId === null) readinessRef.current.reset();
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

  /**
   * Send the viewport once the attachment and the measured grid have both
   * arrived. Called from each side of the rendezvous, because either can be
   * the one that completes it.
   */
  const flushViewport = useCallback(
    (kind: "viewport" | "refresh" = "viewport") => {
      const ws = wsRef.current;
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      const viewport = readinessRef.current.take();
      if (viewport === null) return;
      // The daemon opens an attachment's output bridge on its first
      // `terminal_resize`; `terminal_set_viewport` only positions a bridge that
      // already exists. The renderer sends a resize when its grid changes, which
      // covers the first attachment and nothing after it: a replacement
      // attachment arrives at unchanged geometry, so nothing remeasures and that
      // attachment never streams. This rendezvous is the one place that knows a
      // new attachment has both an id and a measured grid, so it opens the
      // bridge here before positioning it. A "refresh" redraws an attachment
      // that is already bridged and must not reopen one.
      if (kind === "viewport") {
        ws.send(
          terminalResizeMessage(
            viewport.terminalId,
            viewport.attachmentId,
            viewport.rows,
            viewport.cols,
          ),
        );
      }
      ws.send(
        terminalSetViewportMessage(
          `${kind}-${connectionGenerationRef.current}-${++requestCounterRef.current}`,
          viewport.terminalId,
          viewport.attachmentId,
          viewport.rows,
          viewport.cols,
          kind,
        ),
      );
    },
    [],
  );

  /** The renderer measured its grid. */
  const reportViewport = useCallback(
    (rows: number, cols: number) => {
      readinessRef.current.size(rows, cols);
      flushViewport();
    },
    [flushViewport],
  );

  const beginAttachRequest = useCallback(
    (target: TmuxTarget): boolean => {
      const ws = wsRef.current;
      if (pendingRequest.current() || !ws || ws.readyState !== WebSocket.OPEN)
        return false;

      const generation = connectionGenerationRef.current;
      const requestId = `attach-${generation}-${++requestCounterRef.current}`;
      const request: PendingRequest = {
        kind: "attach",
        requestId,
        generation,
        target,
      };
      pendingRequest.begin(request);
      setSessionEnded(false);
      setAttachError(null);
      ws.send(terminalAttachMessage(requestId, target.terminal_id));
      return true;
    },
    [pendingRequest],
  );

  const beginDetachRequest = useCallback(
    (nextTarget: TmuxTarget | null): boolean => {
      const ws = wsRef.current;
      const currentStreamingId = streamingIdRef.current;
      if (
        pendingRequest.current() ||
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
      pendingRequest.begin(request);
      setAttachError(null);
      ws.send(
        terminalDetachMessage(
          requestId,
          attachedTargetRef.current?.terminal_id,
          currentStreamingId,
        ),
      );
      return true;
    },
    [pendingRequest],
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
          if (pendingRequest.current() === null) setIsLoading(false);
          break;
        }

        case "terminal_attach_result": {
          const pending = pendingRequest.match(data.request_id);
          if (pending?.kind !== "attach") break;

          // Failure frames also carry attachment_id (the finalized lease), so
          // only an explicit success verdict may mark the attach live.
          if (data.success === true && typeof data.attachment_id === "string") {
            const attachedId = data.attachment_id;
            fragmentReducerRef.current.markLive(attachedId);
            // Every attach is observe-only; take-control is the sole grant.
            control.observe(attachedId);
            updateAttachment(pending.target, attachedId);
            readinessRef.current.attach(attachedId, pending.target.terminal_id);
            flushViewport();
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
          pendingRequest.clear();
          break;
        }

        case "terminal_detach_result": {
          const pending = pendingRequest.match(data.request_id);
          if (pending?.kind !== "detach") break;

          const { nextTarget } = pending;
          if (data.success) {
            updateAttachment(null, null);
            pendingRequest.clear();
            if (nextTarget) beginAttachRequest(nextTarget);
          } else {
            setAttachError(
              typeof data.message === "string" ? data.message : "Detach failed",
            );
            pendingRequest.clear();
          }
          break;
        }

        case "error": {
          if (pendingRequest.match(data.request_id) === null) break;

          setAttachError(
            typeof data.message === "string"
              ? data.message
              : "Terminal request failed",
          );
          pendingRequest.clear();
          break;
        }

        case "terminal_create_result": {
          if (pendingRequest.match(data.request_id)?.kind !== "create") break;

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
          pendingRequest.clear();
          break;
        }

        case "terminal_kill_result":
          refreshSessions();
          if (pendingRequest.current() === null) setIsLoading(false);
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
      control,
      pendingRequest,
      refreshSessions,
      flushViewport,
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
      pendingRequest.clear();
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
  }, [control, pendingRequest]);

  useEffect(() => {
    connectRef.current = connect;
  }, [connect]);

  // The reassembly timeout only exists if something drives it. Without this
  // sweep a message whose tail never arrives holds its bytes against the
  // socket budget for the life of the connection, and an attachment that went
  // over budget would sit pinned with no snapshot ever requested.
  useEffect(() => {
    if (!connected) return;
    const timer = window.setInterval(() => {
      const reducer = fragmentReducerRef.current;
      reducer.tick(Date.now());
      const target = attachedTargetRef.current;
      // Draining is destructive, so read the target first: taking the requests
      // with nowhere to send them would discard the only record that the
      // attachment is pinned, leaving that pane silent for good.
      if (target === null) return;
      for (const attachmentId of reducer.takeRefreshRequests()) {
        // Re-arm the rendezvous: an attachment whose stream was abandoned has
        // to be told its viewport again, and that frame is the redraw.
        readinessRef.current.attach(attachmentId, target.terminal_id);
        flushViewport("refresh");
      }
    }, TERMINAL_FRAGMENT_TICK_MS);
    return () => window.clearInterval(timer);
  }, [connected, flushViewport]);

  const attachSession = useCallback(
    (sessionName: string, socket: string) => {
      if (pendingRequest.current()) return;
      const target = { terminal_id: sessionName || socket };
      const currentTarget = attachedTargetRef.current;
      if (currentTarget?.terminal_id === target.terminal_id) return;
      if (currentTarget) {
        beginDetachRequest(target);
        return;
      }
      beginAttachRequest(target);
    },
    [beginAttachRequest, beginDetachRequest, pendingRequest],
  );

  const detachSession = useCallback(() => {
    beginDetachRequest(null);
  }, [beginDetachRequest]);

  const clearAttachError = useCallback(() => setAttachError(null), []);

  const refreshTerminal = useCallback(
    (sessionName: string, socket: string) => {
      const streaming = streamingIdRef.current;
      const attached = attachedTargetRef.current;
      // A redraw is addressed to an attachment, not to a terminal: the daemon
      // drops a viewport frame that names no attachment, so refreshing a
      // terminal this client is not attached to could only ever be a no-op.
      // Refusing the mismatch matters as much as refusing the absence — the
      // live attachment belongs to a different terminal, and pairing this
      // terminal's id with it would redraw the wrong pane.
      if (streaming === null || attached === null) return;
      if (attached.terminal_id !== (sessionName || socket)) return;
      readinessRef.current.attach(streaming, attached.terminal_id);
      flushViewport("refresh");
    },
    [flushViewport],
  );

  const createSession = useCallback(
    (name?: string, socket?: string) => {
      const ws = wsRef.current;
      if (pendingRequest.current() || !ws || ws.readyState !== WebSocket.OPEN)
        return;

      const generation = connectionGenerationRef.current;
      const requestId = `create-${generation}-${++requestCounterRef.current}`;
      const request: PendingRequest = { kind: "create", requestId, generation };
      pendingRequest.begin(request);
      setAttachError(null);
      setCreatedSession(null);
      ws.send(
        terminalCreateMessage(requestId, projectIdRef.current, name, socket),
      );
    },
    [pendingRequest],
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
    (options?: { takeover?: boolean }) =>
      control.take(options?.takeover === true),
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
      pendingRequest.dispose();
      const ws = wsRef.current;
      wsRef.current = null;
      ws?.close();
    };
  }, [pendingRequest]);

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
    reportViewport,
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
