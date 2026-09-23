import {
  type ReactNode,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import type { TmuxTarget } from "../../../hooks/terminalRosterSnapshot";
import type { SettledWrite } from "../../../hooks/terminalWriteSettlement";
import { useTmuxSessions } from "../../../hooks/useTmuxSessions";
import { useCoarsePointer, useIsMobile } from "../../../hooks/useIsMobile";
import type { GobbySession } from "../../../types/sessions";
import { cn } from "../../../lib/utils";
import { Button } from "../../ui/Button";
import { coarseHitAreaCls } from "../../ui/controlStyles";
import { ResizeHandle } from "../../shared/ResizeHandle";
import { useRegisterActivityActions } from "../activityActions";
import { TerminalKeysBar } from "./TerminalKeysBar";
import { keepTerminalFocus } from "./terminalFocus";
import { applyCtrlModifier } from "./terminalKeys";
import { useTerminalScrollOffset } from "./scrollOffset";
import { TerminalSessionList } from "./TerminalSessionList";
import {
  findByGobbySessionId,
  type JoinedTerminalSession,
  joinTmuxSessions,
  sessionKey,
} from "./terminalSessions";
import { TerminalView, type TerminalViewHandle } from "./TerminalView";
import { useTerminalKeyboard } from "./useTerminalKeyboard";

export interface TerminalTabProps {
  sessions?: GobbySession[];
  /** Project picker selection; scopes the list to it plus unscoped terminals. */
  projectId?: string | null;
  focusSessionId?: string | null;
  onFocusHandled?: () => void;
  /**
   * The terminal took the whole tab (Expand, or a raised keyboard). The
   * activity panel hides its tab bar while this holds.
   */
  onExpandedChange?: (expanded: boolean) => void;
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

/**
 * What the write-status bar says. A write that settled badly speaks for
 * itself; otherwise the bar is carrying the lease's own refusal cue.
 */
function describeWriteStatus(
  settled: SettledWrite | null,
  refusal: string | null,
): string | null {
  if (settled === null) return refusal;
  if (settled.outcome === "indeterminate") {
    return "Couldn’t confirm the last keystroke reached the terminal.";
  }
  return "The terminal refused the last keystroke.";
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
    // Selection identity is the terminals-row id (sessionKey). Entries in any
    // other shape restore nothing rather than faking a vanished session.
    return typeof target.terminal_id === "string" &&
      target.terminal_id.length > 0
      ? target.terminal_id
      : null;
  } catch {
    return null;
  }
}

function storeTerminalTarget(target: { terminal_id: string } | null): void {
  try {
    if (target === null) {
      window.sessionStorage.removeItem(TERMINAL_TARGET_STORAGE_KEY);
      return;
    }
    window.sessionStorage.setItem(
      TERMINAL_TARGET_STORAGE_KEY,
      JSON.stringify({ terminal_id: target.terminal_id }),
    );
  } catch {
    // Terminal access remains usable when browser storage is unavailable.
  }
}

// Which sessions the list shows. "agents" hides panes with no Gobby session
// (idle shells, tail processes) and is the default; the choice persists as a
// preference across visits.
type SessionScope = "agents" | "all";

const SESSION_SCOPE_STORAGE_KEY = "gobby:terminal:session-scope";

const SESSION_SCOPE_OPTIONS: readonly { value: SessionScope; label: string }[] =
  [
    { value: "agents", label: "Agents" },
    { value: "all", label: "All" },
  ];

function loadStoredSessionScope(): SessionScope {
  try {
    return window.localStorage.getItem(SESSION_SCOPE_STORAGE_KEY) === "all"
      ? "all"
      : "agents";
  } catch {
    return "agents";
  }
}

function storeSessionScope(scope: SessionScope): void {
  try {
    window.localStorage.setItem(SESSION_SCOPE_STORAGE_KEY, scope);
  } catch {
    // The toggle still works for this visit when storage is unavailable.
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

function ChevronsUpIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="m17 11-5-5-5 5" />
      <path d="m17 18-5-5-5 5" />
    </svg>
  );
}

function ChevronsDownIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="m7 6 5 5 5-5" />
      <path d="m7 13 5 5 5-5" />
    </svg>
  );
}

function SmartphoneIcon() {
  return (
    <svg
      width="28"
      height="28"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <rect width="14" height="20" x="5" y="2" rx="2" ry="2" />
      <path d="M12 18h.01" />
    </svg>
  );
}

export function TerminalTab({
  sessions,
  projectId = null,
  focusSessionId = null,
  onFocusHandled,
  onExpandedChange,
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
    sendPaste,
    leaseLost,
    takeControl,
    releaseControl,
    writeRefusal,
    dismissWriteRefusal,
    writeSettlement,
    retryWrite,
    discardWrite,
    resizeTerminal,
    reportViewport,
    killSession,
    onOutput,
    onAttachHistory,
    setScrollOffset,
    onScrollOffsetApplied,
  } = useTmuxSessions(projectId);
  const [selectedKey, setSelectedKey] = useState<string | null>(
    loadStoredTerminalTargetKey,
  );
  const [listHeight, setListHeight] = useState(30);
  const [endedKey, setEndedKey] = useState<string | null>(null);
  const [readyContext, setReadyContext] = useState<TerminalContext | null>(
    null,
  );
  const [focusNotice, setFocusNotice] = useState<string | null>(null);
  const [scope, setScope] = useState<SessionScope>(loadStoredSessionScope);
  const [ctrlArmed, setCtrlArmed] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const isMobile = useIsMobile();
  const coarsePointer = useCoarsePointer();
  const rootRef = useRef<HTMLDivElement>(null);
  const viewRef = useRef<TerminalViewHandle>(null);
  const keyboard = useTerminalKeyboard({
    rootRef,
    viewRef,
    expanded,
    onExpandedChange: setExpanded,
  });
  const streamingIdRef = useRef<string | null>(streamingId);
  const lastAttachedKeyRef = useRef<string | null>(null);
  const consumedFocusIdRef = useRef<string | null>(null);
  const consumedCreatedKeyRef = useRef<string | null>(null);
  const allowInitialSelectionRef = useRef(selectedKey === null);

  const joinedSessions = useMemo(
    () => joinTmuxSessions(tmuxSessions, sessions, projectId),
    [projectId, sessions, tmuxSessions],
  );
  const visibleSessions = useMemo(
    () =>
      scope === "all"
        ? joinedSessions
        : joinedSessions.filter((session) => session.gobby !== null),
    [joinedSessions, scope],
  );
  const changeScope = useCallback((next: SessionScope) => {
    setScope(next);
    storeSessionScope(next);
  }, []);
  const selected =
    joinedSessions.find(
      (session) => sessionKey(session.tmux) === selectedKey,
    ) ?? null;
  const attachedKey = targetKey(attachedTarget);
  // Only gterm keeps scrollback the client can ask for. A tmux pane scrolls by
  // the mouse reports the renderer already sends to its attach client, and a
  // scroll-offset message for one would be answered against the wrong plane.
  // The keys have to match as well as the backend: mid-switch the selection is
  // already the new terminal while the attachment is still the old one, and an
  // offset sent then would address a tmux attachment.
  const scroll = useTerminalScrollOffset({
    native: selected?.tmux.backend === "native" && attachedKey === selectedKey,
    streamingId,
    setScrollOffset,
    onScrollOffsetApplied,
  });
  const returnToLiveEdge = scroll.returnToLiveEdge;
  const terminalContext = useMemo<TerminalContext>(
    () => ({ connected, streamingId }),
    [connected, streamingId],
  );
  const terminalContextRef = useRef(terminalContext);

  useLayoutEffect(() => {
    streamingIdRef.current = streamingId;
    terminalContextRef.current = terminalContext;
  }, [streamingId, terminalContext]);

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
      // The pane is now showing this attachment, so the attaching scrim has
      // done its job. `onReady` cannot say so: it fires when the renderer
      // mounts, and a replacement attachment deliberately keeps the same
      // instance mounted. Exactly one history frame arrives per attachment,
      // which makes this the moment — and leaving the scrim up would be worse
      // than cosmetic, because it covers the pane and swallows scroll.
      setReadyContext(terminalContextRef.current);
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
          visibleSessions.find((session) => !session.dead) ??
          visibleSessions[0];
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
    visibleSessions,
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
      visibleSessions.length === 0 ||
      focusSessionId !== null ||
      !allowInitialSelectionRef.current
    ) {
      return;
    }
    const fallback =
      visibleSessions.find((session) => !session.dead) ?? visibleSessions[0];
    const timer = window.setTimeout(() => {
      allowInitialSelectionRef.current = false;
      setSelectedKey(sessionKey(fallback.tmux));
    }, 0);
    return () => window.clearTimeout(timer);
  }, [endedKey, focusSessionId, visibleSessions, selectedKey, sessionsLoaded]);

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
      // Reported before the attachment guard below: the grid routinely
      // measures itself before the attach resolves, and the rendezvous needs
      // both halves whichever lands first. It sends nothing on its own until
      // an attachment exists to address the frame to.
      if (rows > 0 && cols > 0) reportViewport(rows, cols);
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
    [
      attachedKey,
      reportViewport,
      resizeTerminal,
      selected,
      selectedKey,
      terminalContext,
    ],
  );

  // Taking back after another session displaced this one needs the takeover
  // flag: a plain take is refused while someone else still holds the lease.
  const takeBackControl = useCallback(() => {
    takeControl({ takeover: true });
  }, [takeControl]);

  // Focus asks for the lease, but not while displaced: a plain take can only
  // come back refused there, and control requests are single-flight per
  // attachment, so that doomed request would swallow the takeover the user is
  // reaching for when they focus the take-back button itself.
  const takeControlOnFocus = useCallback(() => {
    if (leaseLost) return;
    takeControl();
  }, [leaseLost, takeControl]);

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
  useRegisterActivityActions<SessionScope>(
    {
      selector: {
        value: scope,
        onChange: changeScope,
        options: SESSION_SCOPE_OPTIONS,
        ariaLabel: "Terminal sessions shown",
      },
      onAdd: () => createSession(),
      addLabel: "New Terminal",
      addAriaLabel: "Create terminal session",
      addDisabled: !connected || requestPending,
    },
    [changeScope, connected, createSession, requestPending, scope],
  );

  // Sticky Ctrl from the keys bar folds into the next key typed into the
  // renderer; bytes the modifier leaves untouched (protocol replies, digits)
  // keep it armed for the key it was meant for.
  // Every byte the operator sends lands at the live edge, so the pane follows
  // it there rather than leaving the window parked in history.
  const sendKeyInput = useCallback(
    (data: string) => {
      returnToLiveEdge();
      sendInput(data);
    },
    [returnToLiveEdge, sendInput],
  );

  const sendPastedText = useCallback(
    (text: string) => {
      returnToLiveEdge();
      sendPaste(text);
    },
    [returnToLiveEdge, sendPaste],
  );

  const sendTypedInput = useCallback(
    (data: string) => {
      if (!ctrlArmed) {
        sendKeyInput(data);
        return;
      }
      const modified = applyCtrlModifier(data);
      if (modified !== data) setCtrlArmed(false);
      sendKeyInput(modified);
    },
    [ctrlArmed, sendKeyInput],
  );

  // One bar at a time: the newest write that settled badly outranks the
  // refusal cue, which is itself only shown when no write awaits a decision.
  const settledWrite =
    writeSettlement.settled[writeSettlement.settled.length - 1] ?? null;

  const writeStatusText = describeWriteStatus(settledWrite, writeRefusal);

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

  // The notices below replace the terminal and its Collapse button, so the
  // expanded layout and a raised keyboard only hold while the terminal shows.
  const terminalShown =
    endedKey === null &&
    (sessionsLoaded ? joinedSessions.length > 0 : selectedKey !== null);
  const expandedShown = expanded && terminalShown;
  useEffect(() => {
    onExpandedChange?.(expandedShown);
    return () => onExpandedChange?.(false);
  }, [expandedShown, onExpandedChange]);
  const closeKeyboard = keyboard.close;
  useEffect(() => {
    if (!terminalShown) closeKeyboard();
  }, [closeKeyboard, terminalShown]);

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
    <div
      ref={rootRef}
      className="relative flex h-full min-h-0 flex-col"
      style={keyboard.height === null ? undefined : { height: keyboard.height }}
    >
      {focusNotice ? (
        <div
          className="absolute end-3 top-12 z-30 rounded-md border border-warning/40 bg-[var(--bg-secondary)] px-3 py-2 text-xs text-warning shadow-sm"
          role="status"
        >
          {focusNotice}
        </div>
      ) : null}

      {/* Terminal list mirrors the sessions-list placement: rows on top,
          the selected terminal's view below its status bar. Expanded, the
          terminal takes the list's room. */}
      {expanded ? null : (
        <>
          <div
            className="min-h-0 overflow-y-auto"
            style={{ height: `${listHeight}%` }}
          >
            <TerminalSessionList
              sessions={visibleSessions}
              value={selectedKey}
              onChange={chooseSession}
              onTerminate={terminateSession}
            />
            {visibleSessions.length === 0 ? (
              <div className="flex flex-col items-start gap-2 px-3 py-3 text-sm text-muted-foreground">
                <span>
                  No agent terminals. Shells and other panes are hidden.
                </span>
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  dense
                  onClick={() => changeScope("all")}
                >
                  Show all sessions
                </Button>
              </div>
            ) : null}
          </div>

          <ResizeHandle
            direction="vertical"
            onResize={setListHeight}
            panelHeight={listHeight}
            minHeight={15}
            maxHeight={70}
          />
        </>
      )}

      <div className="activity-panel-status-bar border-t">
        <span className="activity-panel-status-bar__title">
          {selected ? selected.label : "terminal"}
        </span>
        <Button
          type="button"
          variant="accent"
          size="sm"
          dense
          className={cn("ml-auto shrink-0", coarseHitAreaCls)}
          aria-label={expanded ? "Collapse terminal" : "Expand terminal"}
          title={expanded ? "Collapse terminal" : "Expand terminal"}
          onMouseDown={keepTerminalFocus}
          onClick={() => {
            if (expanded) keyboard.close();
            setExpanded(!expanded);
          }}
        >
          {expanded ? <ChevronsDownIcon /> : <ChevronsUpIcon />}
          <span className="activity-panel-action-btn__label">
            {expanded ? "Collapse" : "Expand"}
          </span>
        </Button>
      </div>

      <div className="relative min-h-0 flex-1 overflow-hidden bg-[var(--bg-primary)]">
        {/*
          Deliberately unkeyed. This used to carry key={streamingId}, which
          made every replacement attachment a full remount: renderer destroyed,
          WASM core reloaded, geometry remeasured, scroll position gone. The
          remount was standing in for a buffer reset — it was the only thing
          stopping a reconnect's overlapping history from being appended under
          the old. applyAttachHistory now resets the pane itself before it
          writes — RIS and ED 3 to clear the core, then a resize to rebuild
          wterm's count-driven scrollback mirror, which the clear alone leaves
          pointing at rows that are gone. Every attach delivers exactly one
          history frame, so that reset covers replacement and terminal
          switching alike, and the pane survives a reconnect as one instance.
        */}
        <TerminalView
          ref={viewRef}
          onReady={handleViewReady}
          onSizeChange={resizeTerminal}
          onProtocolResponse={sendTypedInput}
          minCols={isMobile ? 1 : undefined}
          readOnly={leaseLost}
          keyboardOnDemand={coarsePointer}
          onFocus={takeControlOnFocus}
          onBlur={() => {
            keyboard.close();
            releaseControl();
          }}
          onPaste={sendPastedText}
          onTakeControl={takeBackControl}
          scrollOffsetRows={scroll.rows}
          onScrollRows={scroll.scrollBy}
          onJumpToLive={scroll.jumpToLive}
        />

        {keyboard.rotatePrompt ? (
          <div
            className="absolute inset-0 z-10 grid place-items-center bg-[var(--bg-primary)]/72 p-4"
            onClick={keyboard.close}
          >
            <div
              className="flex max-w-xs flex-col items-center gap-2 rounded-lg border border-border bg-[var(--bg-secondary)] px-5 py-4 text-center"
              role="status"
            >
              <span className="flex text-accent">
                <SmartphoneIcon />
              </span>
              <p className="text-sm font-medium text-foreground">
                Turn your phone upright to type
              </p>
              <p className="text-xs leading-snug text-muted-foreground">
                With the keyboard up, landscape leaves about one terminal row.
              </p>
            </div>
          </div>
        ) : null}

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

      {settledWrite !== null || writeRefusal !== null ? (
        <div
          className="flex shrink-0 flex-wrap items-center gap-2 border-t border-border bg-[var(--bg-secondary)] px-2.5 py-1.5 text-xs text-[var(--text-primary)]"
          data-testid="terminal-write-status"
        >
          {/* The sentence and its reason are the live region; Retry and
              Discard stay outside it, because an atomic `role="status"`
              re-announces everything it wraps whenever any of it changes. */}
          <span className="font-medium" role="status">
            {writeStatusText}
          </span>
          {settledWrite?.reason ? (
            <code className="rounded bg-[var(--bg-tertiary)] px-1 py-0.5 font-mono text-[var(--text-secondary)]">
              {settledWrite.reason}
            </code>
          ) : null}
          {settledWrite?.retryable ? (
            <Button
              variant="secondary"
              size="sm"
              dense
              className={cn("px-1.5 py-0.5", coarseHitAreaCls)}
              onMouseDown={keepTerminalFocus}
              onClick={() =>
                retryWrite(settledWrite.attachmentId, settledWrite.seq)
              }
            >
              Retry
            </Button>
          ) : null}
          <Button
            variant="ghost"
            size="sm"
            dense
            className={cn("px-1.5 py-0.5", coarseHitAreaCls)}
            onMouseDown={keepTerminalFocus}
            onClick={() => {
              if (settledWrite === null) {
                dismissWriteRefusal();
                return;
              }
              discardWrite(settledWrite.attachmentId, settledWrite.seq);
            }}
          >
            Discard
          </Button>
        </div>
      ) : null}

      {selected && !selected.dead ? (
        <div className="shrink-0 border-t border-border px-2.5 py-1.5">
          <TerminalKeysBar
            sendInput={sendKeyInput}
            ctrlArmed={ctrlArmed}
            onCtrlArmedChange={setCtrlArmed}
            keyboard={
              coarsePointer
                ? { open: keyboard.open, onToggle: keyboard.toggle }
                : undefined
            }
          />
        </div>
      ) : null}
    </div>
  );
}
