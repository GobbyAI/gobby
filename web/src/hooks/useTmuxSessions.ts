import { useState, useEffect, useCallback, useRef } from "react";

export {
  createTerminalWsReducer,
  TERMINAL_WS_FRAGMENT_MAX_REASSEMBLY_BYTES,
  TERMINAL_WS_FRAGMENT_MAX_SOCKET_REASSEMBLY_BYTES,
  TERMINAL_WS_SAFE_INTEGER_MAX,
} from "./terminalWsFragments";
import { createTerminalWsReducer } from "./terminalWsFragments";

export const TMUX_REQUEST_TIMEOUT_MS = 10_000;

function asSession(row: Partial<TmuxSession> & { terminal_id?: string }): TmuxSession {
  const terminalId = row.terminal_id ?? "";
  return {
    terminal_id: terminalId,
    backend: row.backend ?? "tmux",
    ownership: row.ownership ?? "gobby",
    state: row.state ?? "live",
    title: row.title ?? row.name ?? null,
    session_id: row.session_id ?? row.gobby_session_id ?? null,
    agent_run_id: row.agent_run_id ?? null,
    dims: row.dims ?? null,
    name: row.name ?? row.title ?? terminalId,
    socket: row.socket ?? row.backend ?? "tmux",
    pane_pid: row.pane_pid ?? null,
    pane_dead: row.pane_dead ?? false,
    pane_title: row.pane_title ?? row.title ?? null,
    pane_command: row.pane_command ?? null,
    pane_path: row.pane_path ?? null,
    window_name: row.window_name ?? null,
    session_title: row.session_title ?? row.title ?? null,
    gobby_session_id: row.gobby_session_id ?? row.session_id ?? null,
    agent_managed: row.agent_managed ?? row.ownership === "gobby",
    attached_bridge: row.attached_bridge ?? null,
  };
}

export interface TmuxSession {
  terminal_id: string;
  backend: string;
  ownership: string;
  state: string;
  title: string | null;
  session_id: string | null;
  agent_run_id: string | null;
  dims: { rows: number; cols: number } | null;
  name: string;
  socket: string;
  pane_pid: number | null;
  pane_dead: boolean;
  pane_title: string | null;
  pane_command: string | null;
  pane_path: string | null;
  window_name: string | null;
  session_title: string | null;
  gobby_session_id: string | null;
  agent_managed: boolean;
  attached_bridge: string | null;
}

export interface TmuxTarget {
  terminal_id: string;
}

export interface CreatedTmuxSession {
  terminal_id: string;
}

/** A bounded scrollback window delivered once, just before streaming starts. */
export interface TerminalAttachHistory {
  streamingId: string;
  text: string;
  /** Older history existed and was cut, by the line bound or the byte bound. */
  truncated: boolean;
  /** Capture failed while the stream itself stayed healthy. */
  unavailable: boolean;
  droppedBytes: number;
  totalBytes: number;
}

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
  liveCliSessionIds: string[];
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
  killSession: (sessionName: string, socket: string) => void;
  refreshSessions: () => void;
  dismissEndedSession: () => void;
  sendInput: (data: string) => void;
  resizeTerminal: (rows: number, cols: number) => void;
  onOutput: (callback: (runId: string, data: string) => void) => void;
  onAttachHistory: (callback: (history: TerminalAttachHistory) => void) => void;
}

export function useTmuxSessions(): TmuxSessionsResult {
  const [sessions, setSessions] = useState<TmuxSession[]>([]);
  const [liveCliSessionIds, setLiveCliSessionIds] = useState<string[]>([]);
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
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<number | null>(null);
  const outputCallbackRef = useRef<
    ((runId: string, data: string) => void) | null
  >(null);
  const attachHistoryCallbackRef = useRef<
    ((history: TerminalAttachHistory) => void) | null
  >(null);
  const attachedTargetRef = useRef<TmuxTarget | null>(null);
  const streamingIdRef = useRef<string | null>(null);
  const connectionGenerationRef = useRef(0);
  const requestCounterRef = useRef(0);
  const pendingRequestRef = useRef<PendingRequest | null>(null);
  const pendingRequestTimeoutRef = useRef<number | null>(null);
  const connectRef = useRef<() => void>(() => {});
  const fragmentReducerRef = useRef(createTerminalWsReducer());
  const leaseGenerationRef = useRef(new Map<string, number>());

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
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    wsRef.current.send(
      JSON.stringify({
        type: "terminal_list",
        request_id: `refresh-${Date.now()}`,
      }),
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
      ws.send(
        JSON.stringify({
          type: "terminal_attach",
          request_id: requestId,
          terminal_id: target.terminal_id,
          frame_delivery: "proxy",
        }),
      );
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
        JSON.stringify({
          type: "terminal_detach",
          request_id: requestId,
          terminal_id: attachedTargetRef.current?.terminal_id,
          attachment_id: currentStreamingId,
        }),
      );
      schedulePendingRequestTimeout(request);
      return true;
    },
    [schedulePendingRequestTimeout],
  );

  const handleMessage = useCallback(
    (data: Record<string, unknown>) => {
      const reducer = fragmentReducerRef.current;
      if (data.type === "terminal_ws_fragment") {
        reducer.push(data);
        for (const event of reducer.applied.splice(0)) {
          handleMessage(event);
        }
        return;
      }
      if (data.type === "terminal_attachment_finalized") {
        const finalizedId = data.attachment_id;
        if (typeof finalizedId === "string") reducer.finalize(finalizedId);
      }
      if (
        data.type === "terminal_control_result" ||
        data.type === "terminal_lease_lost"
      ) {
        const attachmentId = data.attachment_id;
        const generation = data.lease_generation;
        if (typeof attachmentId === "string" && typeof generation === "number") {
          const previous = leaseGenerationRef.current.get(attachmentId) ?? -1;
          if (generation < previous) return;
          leaseGenerationRef.current.set(attachmentId, generation);
        }
      }
      switch (data.type) {
        case "terminal_list": {
          const pageItems = ((data.items as TmuxSession[] | undefined) ?? []).map(asSession);
          const generation = connectionGenerationRef.current;
          if (data.request_id === "init" || data.request_id === "refresh") {
            setSessions(pageItems);
          } else {
            setSessions((current) => {
              const seen = new Set(current.map((row) => row.terminal_id));
              const merged = [...current];
              for (const row of pageItems) {
                if (!seen.has(row.terminal_id)) merged.push(row);
              }
              return merged;
            });
          }
          setLiveCliSessionIds((data.live_cli_session_ids as string[]) || []);
          setSessionsLoaded(true);
          const attached = attachedTargetRef.current;
          if (
            attached &&
            !pageItems.some((session) => session.terminal_id === attached.terminal_id) &&
            data.next_cursor == null
          ) {
            setSessionEnded(true);
          }
          if (typeof data.next_cursor === "string" && data.next_cursor) {
            wsRef.current?.send(
              JSON.stringify({
                type: "terminal_list",
                request_id: `page-${generation}`,
                cursor: data.next_cursor,
              }),
            );
          }
          if (pendingRequestRef.current === null) setIsLoading(false);
          break;
        }

        case "terminal_attach_result": {
          const pending = pendingRequestRef.current;
          if (
            !pending ||
            pending.kind !== "attach" ||
            pending.requestId !== data.request_id ||
            pending.generation !== connectionGenerationRef.current
          )
            break;

          if (data.success || typeof data.attachment_id === "string") {
            const attachedId = data.attachment_id as string;
            fragmentReducerRef.current.markLive(attachedId);
            updateAttachment(pending.target, attachedId);
          } else {
            setAttachError(
              typeof data.message === "string" ? data.message : "Attach failed",
            );
          }
          clearPendingRequest();
          break;
        }

        case "terminal_detach_result": {
          const pending = pendingRequestRef.current;
          if (
            !pending ||
            pending.kind !== "detach" ||
            pending.requestId !== data.request_id ||
            pending.generation !== connectionGenerationRef.current
          )
            break;

          const nextTarget = pending.nextTarget;
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
          const pending = pendingRequestRef.current;
          if (
            !pending ||
            pending.requestId !== data.request_id ||
            pending.generation !== connectionGenerationRef.current
          )
            break;

          setAttachError(
            typeof data.message === "string"
              ? data.message
              : "Terminal request failed",
          );
          clearPendingRequest();
          break;
        }

        case "terminal_create_result": {
          const pending = pendingRequestRef.current;
          if (
            !pending ||
            pending.kind !== "create" ||
            pending.requestId !== data.request_id ||
            pending.generation !== connectionGenerationRef.current
          )
            break;

          if (data.success && typeof data.terminal_id === "string") {
            setCreatedSession({
              terminal_id: data.terminal_id,
            });
            refreshSessions();
          } else {
            setAttachError(
              typeof data.message === "string" ? data.message : "Create failed",
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

        case "terminal_attach_history": {
          // The host proxy keys history on the attachment; that id doubles as
          // the streaming id the scrollback consumer registered against.
          const streamingId =
            (data.attachment_id as string | undefined) ||
            (data.streaming_id as string | undefined) ||
            (data.terminal_id as string | undefined);
          if (typeof streamingId !== "string") break;
          const text = typeof data.text === "string" ? data.text : "";
          const callback = attachHistoryCallbackRef.current;
          if (callback) {
            callback({
              streamingId,
              text,
              truncated: data.truncated === true,
              unavailable: data.unavailable === true,
              droppedBytes:
                typeof data.dropped_bytes === "number" ? data.dropped_bytes : 0,
              totalBytes:
                typeof data.total_bytes === "number" ? data.total_bytes : 0,
            });
          } else if (outputCallbackRef.current && text) {
            outputCallbackRef.current(streamingId, text);
          }
          break;
        }

        case "terminal_output":
          if (outputCallbackRef.current) {
            outputCallbackRef.current(
              (data.attachment_id as string) || (data.terminal_id as string),
              data.data as string,
            );
          }
          break;
      }
    },
    [
      beginAttachRequest,
      clearPendingRequest,
      refreshSessions,
      updateAttachment,
    ],
  );

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
      setConnected(true);
      ws.send(
        JSON.stringify({
          type: "subscribe",
          events: ["terminal_output", "terminal_event", "session_event"],
        }),
      );
      // Fetch session list on connect
      ws.send(
        JSON.stringify({ type: "terminal_list", request_id: "init" }),
      );
    };

    ws.onclose = () => {
      if (!isCurrentConnection()) return;
      setConnected(false);
      setSessionsLoaded(false);
      fragmentReducerRef.current.disconnect();
      fragmentReducerRef.current = createTerminalWsReducer();
      updateAttachment(null, null);
      if (pendingRequestTimeoutRef.current !== null) {
        clearTimeout(pendingRequestTimeoutRef.current);
        pendingRequestTimeoutRef.current = null;
      }
      pendingRequestRef.current = null;
      setRequestPending(false);
      setIsLoading(false);
      setAttachError(null);
      setCreatedSession(null);
      reconnectTimeoutRef.current = window.setTimeout(() => {
        if (!isCurrentConnection()) return;
        reconnectTimeoutRef.current = null;
        connectRef.current();
      }, 2000);
    };

    ws.onerror = (error) => {
      if (!isCurrentConnection()) return;
      console.error("Tmux WebSocket error:", error);
    };

    ws.onmessage = (event) => {
      if (!isCurrentConnection()) return;
      try {
        const data = JSON.parse(event.data);
        handleMessage(data);
      } catch (e) {
        console.error("Failed to parse tmux message:", e);
      }
    };
  }, [handleMessage, updateAttachment]);

  useEffect(() => {
    connectRef.current = connect;
  }, [connect]);

  const attachSession = useCallback(
    (sessionName: string, socket: string) => {
      if (pendingRequestRef.current) return;
      const target = { terminal_id: sessionName || socket };
      const currentTarget = attachedTargetRef.current;
      if (currentTarget?.terminal_id === target.terminal_id)
        return;
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
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    wsRef.current.send(
      JSON.stringify({
        type: "terminal_set_viewport",
        request_id: `refresh-${connectionGenerationRef.current}-${++requestCounterRef.current}`,
        terminal_id: sessionName || socket,
      }),
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
        JSON.stringify({
          type: "terminal_create",
          request_id: requestId,
          rows: 24,
          cols: 80,
          cwd: name,
          command: socket ? [socket] : ["zsh"],
        }),
      );
      schedulePendingRequestTimeout(request);
    },
    [schedulePendingRequestTimeout],
  );

  const killSession = useCallback(
    (sessionName: string, socket: string) => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
      setIsLoading(true);
      const currentTarget = attachedTargetRef.current;
      if (currentTarget?.terminal_id === sessionName) {
        updateAttachment(null, null);
      }
      wsRef.current.send(
        JSON.stringify({
          type: "terminal_kill",
          request_id: `kill-${Date.now()}`,
          terminal_id: sessionName,
        }),
      );
    },
    [updateAttachment],
  );

  const sendInput = useCallback((data: string) => {
    const currentStreamingId = streamingIdRef.current;
    if (
      !wsRef.current ||
      wsRef.current.readyState !== WebSocket.OPEN ||
      !currentStreamingId
    )
      return;
    wsRef.current.send(
      JSON.stringify({
        type: "terminal_input",
        terminal_id: attachedTargetRef.current?.terminal_id,
        attachment_id: currentStreamingId,
        client_write_seq: ++requestCounterRef.current,
        data,
      }),
    );
  }, []);

  const resizeTerminal = useCallback((rows: number, cols: number) => {
    const currentStreamingId = streamingIdRef.current;
    if (
      !wsRef.current ||
      wsRef.current.readyState !== WebSocket.OPEN ||
      !currentStreamingId
    )
      return;
    wsRef.current.send(
      JSON.stringify({
        type: "terminal_resize",
        terminal_id: attachedTargetRef.current?.terminal_id,
        attachment_id: currentStreamingId,
        rows,
        cols,
      }),
    );
  }, []);

  // Single consumer by design: TerminalTab owns the terminal output stream.
  const onOutput = useCallback(
    (callback: (runId: string, data: string) => void) => {
      outputCallbackRef.current = callback;
    },
    [],
  );

  const onAttachHistory = useCallback(
    (callback: (history: TerminalAttachHistory) => void) => {
      attachHistoryCallbackRef.current = callback;
    },
    [],
  );

  useEffect(() => {
    connect();
    return () => {
      connectionGenerationRef.current += 1;
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
        reconnectTimeoutRef.current = null;
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
  }, [connect]);

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
    liveCliSessionIds,
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
    sendInput,
    resizeTerminal,
    onOutput,
    onAttachHistory,
  };
}
