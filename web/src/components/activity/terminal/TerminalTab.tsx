import {
  type ReactNode,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  type TmuxTarget,
  useTmuxSessions,
} from "../../../hooks/useTmuxSessions";
import type { GobbySession } from "../../../types/sessions";
import { Button } from "../../ui/Button";
import { ResizeHandle } from "../../shared/ResizeHandle";
import { useRegisterActivityActions } from "../activityActions";
import { TerminalKeysBar } from "./TerminalKeysBar";
import { TerminalSessionList } from "./TerminalSessionList";
import {
  findByGobbySessionId,
  type JoinedTerminalSession,
  joinTmuxSessions,
  sessionKey,
} from "./terminalSessions";
import { TerminalView, type TerminalViewHandle } from "./TerminalView";

export interface TerminalTabProps {
  sessions?: GobbySession[];
  focusSessionId?: string | null;
  onFocusHandled?: () => void;
}

interface StatePanelProps {
  title: string;
  body: string;
  action?: ReactNode;
  busy?: boolean;
}

interface TerminalContext {
  connected: boolean;
  streamingId: string | null;
}

function StatePanel({ title, body, action, busy = false }: StatePanelProps) {
  return (
    <div className="grid h-full min-h-44 place-items-center p-4">
      <div className="flex max-w-sm flex-col items-center gap-3 text-center">
        {busy ? (
          <span
            className="size-4 animate-spin rounded-full border-2 border-muted-foreground/30 border-t-accent"
            aria-hidden="true"
          />
        ) : null}
        <div className="space-y-1">
          <p className="text-sm font-semibold text-foreground">{title}</p>
          <p className="text-xs leading-relaxed text-muted-foreground">
            {body}
          </p>
        </div>
        {action}
      </div>
    </div>
  );
}

function targetKey(target: TmuxTarget | null): string | null {
  return target ? target.terminal_id : null;
}

const TERMINAL_TARGET_STORAGE_KEY = "gobby:terminal:selected-target";

function loadStoredTerminalTargetKey(): string | null {
  try {
    const raw = window.sessionStorage.getItem(TERMINAL_TARGET_STORAGE_KEY);
    if (raw === null) return null;
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return null;
    const target = parsed as Record<string, unknown>;
    if (
      typeof target.socket !== "string" ||
      target.socket.length === 0 ||
      typeof target.sessionName !== "string" ||
      target.sessionName.length === 0
    ) {
      return null;
    }
    return `${target.socket}:${target.sessionName}`;
  } catch {
    return null;
  }
}

function storeTerminalTarget(
  target: { name: string; socket: string } | null,
): void {
  try {
    if (target === null) {
      window.sessionStorage.removeItem(TERMINAL_TARGET_STORAGE_KEY);
      return;
    }
    window.sessionStorage.setItem(
      TERMINAL_TARGET_STORAGE_KEY,
      JSON.stringify({ socket: target.socket, sessionName: target.name }),
    );
  } catch {
    // Terminal access remains usable when browser storage is unavailable.
  }
}

function PlusIcon() {
  return (
    <svg
      className="size-3"
      viewBox="0 0 16 16"
      fill="currentColor"
      aria-hidden="true"
    >
      <path d="M8 1.5a.75.75 0 0 1 .75.75v5h5a.75.75 0 0 1 0 1.5h-5v5a.75.75 0 0 1-1.5 0v-5h-5a.75.75 0 0 1 0-1.5h5v-5A.75.75 0 0 1 8 1.5Z" />
    </svg>
  );
}

export function TerminalTab({
  sessions,
  focusSessionId = null,
  onFocusHandled,
}: TerminalTabProps) {
  const {
    sessions: tmuxSessions,
    connected,
    sessionsLoaded,
    attachedTarget,
    streamingId,
    sessionEnded: hookSessionEnded,
    requestPending,
    attachError,
    createdSession,
    attachSession,
    detachSession,
    clearAttachError,
    createSession,
    dismissEndedSession,
    sendInput,
    resizeTerminal,
    killSession,
    onOutput,
    onAttachHistory,
  } = useTmuxSessions();
  const [selectedKey, setSelectedKey] = useState<string | null>(
    loadStoredTerminalTargetKey,
  );
  const [listHeight, setListHeight] = useState(30);
  const [endedKey, setEndedKey] = useState<string | null>(null);
  const [readyContext, setReadyContext] = useState<TerminalContext | null>(
    null,
  );
  const [focusNotice, setFocusNotice] = useState<string | null>(null);
  const viewRef = useRef<TerminalViewHandle>(null);
  const streamingIdRef = useRef<string | null>(streamingId);
  const lastAttachedKeyRef = useRef<string | null>(null);
  const consumedFocusIdRef = useRef<string | null>(null);
  const consumedCreatedKeyRef = useRef<string | null>(null);
  const allowInitialSelectionRef = useRef(selectedKey === null);

  const joinedSessions = useMemo(
    () => joinTmuxSessions(tmuxSessions, sessions),
    [sessions, tmuxSessions],
  );
  const selected =
    joinedSessions.find(
      (session) => sessionKey(session.tmux) === selectedKey,
    ) ?? null;
  const attachedKey = targetKey(attachedTarget);
  const terminalContext = useMemo<TerminalContext>(
    () => ({ connected, streamingId }),
    [connected, streamingId],
  );

  useLayoutEffect(() => {
    streamingIdRef.current = streamingId;
  }, [streamingId]);

  useEffect(() => {
    onOutput((runId, data) => {
      if (runId === streamingIdRef.current) viewRef.current?.write(data);
    });
    return () => onOutput(() => undefined);
  }, [onOutput]);

  useEffect(() => {
    onAttachHistory((history) => {
      // Same stale-attachment filter the output stream uses: a superseded
      // streaming id must not paint into the replacement's terminal.
      if (history.streamingId !== streamingIdRef.current) return;
      viewRef.current?.applyAttachHistory(
        history.text,
        history.truncated,
        history.unavailable,
      );
    });
    return () => onAttachHistory(() => undefined);
  }, [onAttachHistory]);

  useEffect(() => () => detachSession(), [detachSession]);

  useEffect(() => {
    if (streamingId !== null && attachedKey !== null) {
      lastAttachedKeyRef.current = attachedKey;
    }
  }, [attachedKey, streamingId]);

  const chooseSession = useCallback(
    (nextKey: string) => {
      if (nextKey === selectedKey) return;
      allowInitialSelectionRef.current = false;
      lastAttachedKeyRef.current = null;
      setEndedKey(null);
      setReadyContext(null);
      setSelectedKey(nextKey);
      clearAttachError();
      if (hookSessionEnded) dismissEndedSession();
    },
    [clearAttachError, dismissEndedSession, hookSessionEnded, selectedKey],
  );

  useEffect(() => {
    if (selected !== null) storeTerminalTarget(selected.tmux);
  }, [selected]);

  const terminateSession = useCallback(
    (session: JoinedTerminalSession) => {
      killSession(session.tmux.terminal_id);
    },
    [killSession],
  );

  useEffect(() => {
    if (createdSession === null) return;
    const createdKey = createdSession.terminal_id;
    if (
      consumedCreatedKeyRef.current === createdKey ||
      !joinedSessions.some((session) => sessionKey(session.tmux) === createdKey)
    ) {
      return;
    }
    consumedCreatedKeyRef.current = createdKey;
    chooseSession(createdKey);
  }, [chooseSession, createdSession, joinedSessions]);

  useEffect(() => {
    if (focusSessionId === null) {
      consumedFocusIdRef.current = null;
      return;
    }
    if (!sessionsLoaded || consumedFocusIdRef.current === focusSessionId)
      return;

    const timer = window.setTimeout(() => {
      if (consumedFocusIdRef.current === focusSessionId) return;
      consumedFocusIdRef.current = focusSessionId;
      const focused = findByGobbySessionId(joinedSessions, focusSessionId);
      if (focused) {
        chooseSession(sessionKey(focused.tmux));
        setFocusNotice(null);
      } else {
        setFocusNotice("No live terminal for this session");
        const fallback =
          joinedSessions.find((session) => !session.dead) ?? joinedSessions[0];
        if (fallback) chooseSession(sessionKey(fallback.tmux));
      }
      onFocusHandled?.();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [
    chooseSession,
    focusSessionId,
    joinedSessions,
    onFocusHandled,
    sessionsLoaded,
  ]);

  useEffect(() => {
    if (focusNotice === null) return;
    const timer = window.setTimeout(() => setFocusNotice(null), 3000);
    return () => window.clearTimeout(timer);
  }, [focusNotice]);

  useEffect(() => {
    if (!sessionsLoaded || endedKey !== null) return;
    const availableKeys = new Set(
      joinedSessions.map((session) => sessionKey(session.tmux)),
    );
    const lastAttachedKey = lastAttachedKeyRef.current;
    const vanishedKey =
      lastAttachedKey !== null && !availableKeys.has(lastAttachedKey)
        ? lastAttachedKey
        : selectedKey !== null && !availableKeys.has(selectedKey)
          ? selectedKey
          : null;
    if (hookSessionEnded || vanishedKey !== null) {
      const timer = window.setTimeout(() => {
        setEndedKey(vanishedKey ?? lastAttachedKey ?? selectedKey ?? "ended");
      }, 0);
      return () => window.clearTimeout(timer);
    }
  }, [endedKey, hookSessionEnded, joinedSessions, selectedKey, sessionsLoaded]);

  useEffect(() => {
    if (
      !sessionsLoaded ||
      endedKey !== null ||
      selectedKey !== null ||
      joinedSessions.length === 0 ||
      focusSessionId !== null ||
      !allowInitialSelectionRef.current
    ) {
      return;
    }
    const fallback =
      joinedSessions.find((session) => !session.dead) ?? joinedSessions[0];
    const timer = window.setTimeout(() => {
      allowInitialSelectionRef.current = false;
      setSelectedKey(sessionKey(fallback.tmux));
    }, 0);
    return () => window.clearTimeout(timer);
  }, [endedKey, focusSessionId, joinedSessions, selectedKey, sessionsLoaded]);

  useEffect(() => {
    if (
      !connected ||
      !sessionsLoaded ||
      requestPending ||
      attachError !== null ||
      endedKey !== null ||
      selected === null ||
      sessionKey(selected.tmux) !== selectedKey ||
      attachedKey === selectedKey
    ) {
      return;
    }
    const availableKeys = new Set(
      joinedSessions.map((session) => sessionKey(session.tmux)),
    );
    const lastAttachedKey = lastAttachedKeyRef.current;
    if (
      (lastAttachedKey !== null && !availableKeys.has(lastAttachedKey)) ||
      !availableKeys.has(selectedKey)
    ) {
      return;
    }
    if (streamingId !== null) {
      detachSession();
    } else {
      attachSession(selected.tmux.terminal_id, selected.tmux.socket);
    }
  }, [
    attachError,
    attachSession,
    attachedKey,
    connected,
    detachSession,
    endedKey,
    joinedSessions,
    requestPending,
    selected,
    selectedKey,
    sessionsLoaded,
    streamingId,
  ]);

  const handleViewReady = useCallback(
    (rows: number, cols: number) => {
      const activeStreamingId = streamingIdRef.current;
      if (
        activeStreamingId === null ||
        selected === null ||
        attachedKey !== selectedKey
      ) {
        return;
      }
      const reportedSize =
        rows > 0 && cols > 0 ? { rows, cols } : viewRef.current?.getSize();
      // This resize is the activation signal: it carries the first real
      // geometry, and the daemon builds the tmux client, captures history, and
      // repaints off the back of it. A client-driven refresh here would only
      // race the repaint the server already owns.
      if (reportedSize) {
        resizeTerminal(reportedSize.rows, reportedSize.cols);
      }
      setReadyContext(terminalContext);
    },
    [attachedKey, resizeTerminal, selected, selectedKey, terminalContext],
  );

  const dismissVanishedSession = useCallback(() => {
    allowInitialSelectionRef.current = false;
    lastAttachedKeyRef.current = null;
    setSelectedKey(null);
    setEndedKey(null);
    setReadyContext(null);
    storeTerminalTarget(null);
    clearAttachError();
    dismissEndedSession();
  }, [clearAttachError, dismissEndedSession]);

  // "+ New Terminal" lives in the shared activity toolbar like every other
  // tab's add action — the tab body contributes no chrome bar of its own.
  useRegisterActivityActions(
    {
      onAdd: () => createSession(),
      addLabel: "New Terminal",
      addAriaLabel: "Create terminal session",
      addDisabled: !connected || requestPending,
    },
    [connected, createSession, requestPending],
  );

  const isAttaching =
    selected !== null &&
    attachError === null &&
    endedKey === null &&
    (streamingId === null ||
      attachedKey !== selectedKey ||
      readyContext !== terminalContext);
  const newTerminalButton = (
    <Button
      variant="accent"
      size="sm"
      disabled={!connected || requestPending}
      onClick={() => createSession()}
    >
      <PlusIcon />
      New Terminal
    </Button>
  );

  if (!sessionsLoaded && selectedKey === null) {
    return (
      <StatePanel
        title="Loading terminal sessions…"
        body="Waiting for the daemon’s first tmux session list."
        busy
      />
    );
  }

  if (endedKey !== null) {
    return (
      <StatePanel
        title="Terminal session ended"
        body="The selected socket and tmux session disappeared. Dismiss this notice to choose another live session."
        action={
          <Button
            variant="secondary"
            size="sm"
            onClick={dismissVanishedSession}
          >
            Dismiss ended session
          </Button>
        }
      />
    );
  }

  if (sessionsLoaded && joinedSessions.length === 0) {
    return (
      <StatePanel
        title="No terminal sessions"
        body="Create one to start a live, read-only terminal view. Input stays behind the explicit composer."
        action={newTerminalButton}
      />
    );
  }

  return (
    <div className="relative flex h-full min-h-0 flex-col">
      {focusNotice ? (
        <div
          className="absolute end-3 top-12 z-30 rounded-md border border-warning/40 bg-[var(--bg-secondary)] px-3 py-2 text-xs text-warning shadow-sm"
          role="status"
        >
          {focusNotice}
        </div>
      ) : null}

      {/* Terminal list mirrors the sessions-list placement: rows on top,
          the selected terminal's view below its Watching status bar. */}
      <div
        className="min-h-0 overflow-y-auto"
        style={{ height: `${listHeight}%` }}
      >
        <TerminalSessionList
          sessions={joinedSessions}
          value={selectedKey}
          onChange={chooseSession}
          onTerminate={terminateSession}
        />
      </div>

      <ResizeHandle
        direction="vertical"
        onResize={setListHeight}
        panelHeight={listHeight}
        minHeight={15}
        maxHeight={70}
      />

      <div className="activity-panel-status-bar border-t">
        <span className="activity-panel-status-bar__title">
          <span className="activity-panel-status-bar__watching-prefix">
            Watching{" "}
          </span>
          {selected ? selected.label : "terminal"}
        </span>
      </div>

      <div className="relative min-h-0 flex-1 overflow-hidden bg-[var(--bg-primary)]">
        <TerminalView
          key={streamingId ?? "pending"}
          ref={viewRef}
          onReady={handleViewReady}
          onSizeChange={resizeTerminal}
          onProtocolResponse={sendInput}
        />

        {isAttaching ? (
          <div className="absolute inset-0 z-10 grid place-items-center bg-[var(--bg-primary)]/88 backdrop-blur-[1px]">
            <div className="flex items-center gap-2 text-xs font-medium text-muted-foreground">
              <span
                className="size-3.5 animate-spin rounded-full border-2 border-muted-foreground/30 border-t-accent"
                aria-hidden="true"
              />
              Attaching terminal…
            </div>
          </div>
        ) : null}

        {!connected ? (
          <div className="absolute inset-0 z-20 grid place-items-center bg-[var(--bg-primary)]/90">
            <div className="max-w-xs space-y-1 text-center">
              <p className="text-sm font-semibold text-foreground">
                Reconnecting
              </p>
              <p className="text-xs text-muted-foreground">
                Waiting for the daemon before confirming this terminal still
                exists.
              </p>
            </div>
          </div>
        ) : null}

        {attachError ? (
          <div className="absolute inset-0 z-30 grid place-items-center bg-[var(--bg-primary)]/92 p-4">
            <div
              className="flex max-w-sm flex-col items-center gap-3 rounded-lg border border-destructive/40 bg-[var(--bg-secondary)] p-4 text-center"
              role="alert"
            >
              <p className="text-sm font-semibold text-foreground">
                Couldn’t attach to this terminal. {attachError}
              </p>
              <Button variant="secondary" size="sm" onClick={clearAttachError}>
                Retry terminal attach
              </Button>
            </div>
          </div>
        ) : null}
      </div>

      {selected && !selected.dead ? (
        <div className="shrink-0 border-t border-border px-2.5 py-1.5">
          <TerminalKeysBar sendInput={sendInput} />
        </div>
      ) : null}
    </div>
  );
}
