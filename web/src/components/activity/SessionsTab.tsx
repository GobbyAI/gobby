import { memo, useState, useEffect, useCallback, useRef, useMemo } from "react";

import { useSessionDetail } from "../../hooks/useSessionDetail";
import type { GobbySession } from "../../types/sessions";
import type { ChatMessage, SwappedSessionTarget } from "../../types/chat";
import { getSessionTitleText } from "../../lib/sessionTitle";
import { ResizeHandle } from "../chat/artifacts/ResizeHandle";
import { ArtifactContext } from "../chat/artifacts/ArtifactContext";
import { MessageItem } from "../chat/MessageItem";
import { MemoizedMarkdown } from "../shared/MemoizedMarkdown";
import { SourceIcon } from "../shared/SourceIcon";
import {
  SessionInteractionModal,
  type InteractionMode,
} from "./SessionInteractionModal";

interface RunningAgent {
  run_id: string;
  provider: string;
  pid?: number;
  mode?: string;
  started_at?: string;
  session_id?: string;
}

interface SessionsTabProps {
  sessions?: GobbySession[];
  isLoadingSessions?: boolean;
  onKillAgent?: (runId: string) => void;
  onExpireSession?: (sessionId: string) => void;
  onResumeSession?: (sessionId: string) => Promise<string> | string | void;
  chatSessionId?: string;
  focusSessionId?: string | null;
  onFocusHandled?: () => void;
  onSwapSession?: (target: SwappedSessionTarget) => void;
}

interface WatchingSessionEntry {
  id: string;
  type: "agent" | "session";
  label: string;
  provider: string;
  status: string;
  sessionType?: string;
  externalId?: string;
  agentRunId?: string | null;
  runId?: string;
  startedAt?: string;
  updatedAt?: string;
  seqNum?: number | null;
  hasTmux: boolean;
  sandboxEnabled: boolean;
}

interface SessionContextMenu {
  x: number;
  y: number;
  entry: WatchingSessionEntry;
}

type SessionStatusFilter = "live" | "expired";
type WatchingContentMode = "transcript" | "summary";

const WATCHING_SESSION_ID_KEY = "gobby-watching-session-id";
const LIVE_SESSION_STATUSES = new Set(["active", "paused", "handoff_ready"]);
const HIDDEN_SOURCES = new Set(["pipeline", "cron", "system"]);

function getBaseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL || "";
}

function getSessionTypeBadge(sessionType: string | undefined): {
  label: string;
  className: string;
} {
  if (sessionType === "web_chat") {
    return { label: "web", className: "session-kind-badge--web" };
  }
  return { label: "tmux", className: "session-kind-badge--tmux" };
}

function getAgentBadge(agentRunId: string | null | undefined): {
  label: string;
  className: string;
} | null {
  if (!agentRunId) return null;
  return { label: "auto", className: "session-kind-badge--auto" };
}

function getSandboxBadge(sandboxEnabled: boolean): {
  label: string;
  className: string;
} | null {
  if (!sandboxEnabled) return null;
  return { label: "SB", className: "session-kind-badge--sandbox" };
}

function renderBadges(entry: WatchingSessionEntry) {
  const typeBadge = getSessionTypeBadge(entry.sessionType);
  const agentBadge = getAgentBadge(entry.agentRunId);
  const sandboxBadge = getSandboxBadge(entry.sandboxEnabled);
  return (
    <>
      <span className={`session-kind-badge ${typeBadge.className}`}>
        {typeBadge.label}
      </span>
      {sandboxBadge && (
        <span className={`session-kind-badge ${sandboxBadge.className}`}>
          {sandboxBadge.label}
        </span>
      )}
      {agentBadge && (
        <span className={`session-kind-badge ${agentBadge.className}`}>
          {agentBadge.label}
        </span>
      )}
    </>
  );
}

function matchesStatusFilter(
  session: GobbySession,
  statusFilter: SessionStatusFilter,
): boolean {
  if (statusFilter === "expired") {
    return session.status === "expired";
  }
  return LIVE_SESSION_STATUSES.has(session.status);
}

function matchesSearch(session: GobbySession, search: string): boolean {
  if (!search.trim()) {
    return true;
  }
  const query = search.trim().toLowerCase();
  return (
    (session.title && session.title.toLowerCase().includes(query)) ||
    session.ref.toLowerCase().includes(query) ||
    session.external_id.toLowerCase().includes(query)
  );
}

function entryTimestamp(entry: WatchingSessionEntry): number {
  const raw = entry.updatedAt ?? entry.startedAt ?? null;
  return raw ? new Date(raw).getTime() : 0;
}

export const SessionsTab = memo(function SessionsTab({
  sessions = [],
  isLoadingSessions = false,
  onKillAgent,
  onExpireSession,
  onResumeSession,
  chatSessionId,
  focusSessionId,
  onFocusHandled,
  onSwapSession,
}: SessionsTabProps) {
  const [agents, setAgents] = useState<RunningAgent[]>([]);
  const [agentsLoading, setAgentsLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<SessionStatusFilter>("live");
  const [contentMode, setContentMode] = useState<WatchingContentMode>("transcript");
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(() => {
    try {
      return localStorage.getItem(WATCHING_SESSION_ID_KEY);
    } catch {
      return null;
    }
  });
  const [topHeight, setTopHeight] = useState(35);
  const [expiringIds, setExpiringIds] = useState<Set<string>>(new Set());
  const [ctxMenu, setCtxMenu] = useState<SessionContextMenu | null>(null);
  const [modalMode, setModalMode] = useState<InteractionMode | null>(null);
  const [modalEntry, setModalEntry] = useState<WatchingSessionEntry | null>(null);
  const initialSelectionAppliedRef = useRef(false);
  const selectionClearedRef = useRef(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const noopArtifactCtx = useMemo(
    () => ({
      openCodeAsArtifact: () => {},
      openFileAsArtifact: () => {},
    }),
    [],
  );

  const fetchAgents = useCallback(async () => {
    const baseUrl = getBaseUrl();
    try {
      const response = await fetch(`${baseUrl}/api/agents/running`);
      const data = response.ok ? await response.json() : { agents: [] };
      setAgents(data.agents ?? data ?? []);
      setFetchError(null);
    } catch (error) {
      console.error("Failed to fetch running agents:", error);
      setFetchError("Failed to load running agents");
    } finally {
      setAgentsLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchAgents();
    const interval = window.setInterval(() => {
      void fetchAgents();
    }, 5000);
    return () => window.clearInterval(interval);
  }, [fetchAgents]);

  useEffect(() => {
    setExpiringIds((prev) => {
      const next = new Set<string>();
      for (const sessionId of prev) {
        const session = sessions.find((candidate) => candidate.id === sessionId);
        if (session && session.status !== "expired") {
          next.add(sessionId);
        }
      }
      return next;
    });
  }, [sessions]);

  const visibleSessions = useMemo(
    () =>
      sessions
        .filter((session) => session.id !== chatSessionId)
        .filter((session) => !expiringIds.has(session.id))
        .filter((session) => !HIDDEN_SOURCES.has(session.source))
        .filter((session) => matchesStatusFilter(session, statusFilter))
        .filter((session) => matchesSearch(session, search))
        .sort(
          (a, b) =>
            new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
        ),
    [chatSessionId, expiringIds, search, sessions, statusFilter],
  );

  const entries: WatchingSessionEntry[] = useMemo(() => {
    const agentEntries: WatchingSessionEntry[] =
      statusFilter === "live"
        ? agents.reduce<WatchingSessionEntry[]>((nextEntries, agent) => {
            const matchedSession = agent.session_id
              ? visibleSessions.find((session) => session.id === agent.session_id)
              : undefined;
            if (!matchedSession) {
              return nextEntries;
            }
            nextEntries.push({
              id: matchedSession.id,
              type: "agent",
              label: getSessionTitleText(matchedSession.title),
              provider: matchedSession.source ?? agent.provider,
              status: matchedSession.status,
              sessionType: matchedSession.session_type,
              externalId: matchedSession.external_id,
              agentRunId: matchedSession.agent_run_id ?? agent.run_id,
              runId: agent.run_id,
              startedAt: agent.started_at,
              updatedAt: matchedSession.updated_at,
              seqNum: matchedSession.seq_num,
              hasTmux: Boolean(matchedSession.terminal_context),
              sandboxEnabled: matchedSession.sandbox_enabled ?? false,
            });
            return nextEntries;
          }, [])
        : [];

    const agentSessionIds = new Set(agentEntries.map((entry) => entry.id));
    const sessionEntries = visibleSessions
      .filter((session) => !agentSessionIds.has(session.id))
      .map((session) => ({
        id: session.id,
        type: "session" as const,
        label: getSessionTitleText(session.title),
        provider: session.source,
        status: session.status,
        sessionType: session.session_type,
        externalId: session.external_id,
        agentRunId: session.agent_run_id,
        updatedAt: session.updated_at,
        seqNum: session.seq_num,
        hasTmux: Boolean(session.terminal_context),
        sandboxEnabled: session.sandbox_enabled ?? false,
      }));

    return [...agentEntries, ...sessionEntries].sort(
      (a, b) => entryTimestamp(b) - entryTimestamp(a),
    );
  }, [agents, statusFilter, visibleSessions]);

  const isLoading = isLoadingSessions || agentsLoading;

  useEffect(() => {
    if (isLoading) {
      return;
    }

    if (entries.length === 0) {
      selectionClearedRef.current = false;
      if (selectedSessionId !== null) {
        setSelectedSessionId(null);
      }
      return;
    }

    const hasFocusedEntry =
      focusSessionId != null && entries.some((entry) => entry.id === focusSessionId);

    if (!initialSelectionAppliedRef.current) {
      initialSelectionAppliedRef.current = true;
      selectionClearedRef.current = false;
      const persistedStillPresent =
        selectedSessionId != null &&
        entries.some((entry) => entry.id === selectedSessionId);
      const nextSelection = hasFocusedEntry
        ? focusSessionId
        : persistedStillPresent
          ? selectedSessionId
          : entries[0].id;
      if (nextSelection !== selectedSessionId) {
        setSelectedSessionId(nextSelection);
      }
      if (hasFocusedEntry) {
        onFocusHandled?.();
      }
      return;
    }

    if (hasFocusedEntry && focusSessionId !== selectedSessionId) {
      selectionClearedRef.current = false;
      setSelectedSessionId(focusSessionId);
      onFocusHandled?.();
      return;
    }

    if (!selectedSessionId) {
      if (selectionClearedRef.current) {
        return;
      }
      setSelectedSessionId(entries[0].id);
      return;
    }

    const stillPresent = entries.some((entry) => entry.id === selectedSessionId);
    if (!stillPresent) {
      if (selectedSessionId === chatSessionId && !hasFocusedEntry) {
        selectionClearedRef.current = true;
        setSelectedSessionId(null);
        return;
      }
      selectionClearedRef.current = false;
      setSelectedSessionId(entries[0].id);
    }
  }, [
    chatSessionId,
    entries,
    focusSessionId,
    isLoading,
    onFocusHandled,
    selectedSessionId,
  ]);

  useEffect(() => {
    try {
      if (selectedSessionId) {
        localStorage.setItem(WATCHING_SESSION_ID_KEY, selectedSessionId);
      } else {
        localStorage.removeItem(WATCHING_SESSION_ID_KEY);
      }
    } catch {
      /* ignore */
    }
  }, [selectedSessionId]);

  useEffect(() => {
    setContentMode("transcript");
  }, [selectedSessionId]);

  const {
    session: selectedSessionDetail,
    messages,
    isLoading: isLoadingDetail,
    transcriptStatus,
  } = useSessionDetail(selectedSessionId);

  const selectedEntry = useMemo(
    () => entries.find((entry) => entry.id === selectedSessionId) ?? null,
    [entries, selectedSessionId],
  );

  const selectedCatalogSession = useMemo(
    () => sessions.find((session) => session.id === selectedSessionId) ?? null,
    [selectedSessionId, sessions],
  );

  const selectedSession = selectedSessionDetail ?? selectedCatalogSession;

  const chatMessages: ChatMessage[] = useMemo(
    () =>
      messages.map((message) => {
        const chatMessage: ChatMessage = {
          id: message.id,
          role: (message.role as "user" | "assistant" | "system") || "assistant",
          content: message.content || "",
          timestamp: new Date(message.timestamp),
          contentBlocks: message.content_blocks,
        };
        if (message.content_blocks) {
          for (const block of message.content_blocks) {
            if (block.type === "tool_chain" && block.tool_calls) {
              const filteredCalls = block.tool_calls.filter(
                (toolCall: { tool_name?: string; status?: string }) =>
                  !(
                    toolCall.tool_name === "AskUserQuestion" &&
                    toolCall.status !== "calling"
                  ),
              );
              block.tool_calls = filteredCalls;
              chatMessage.toolCalls = [
                ...(chatMessage.toolCalls || []),
                ...filteredCalls,
              ];
            } else if (block.type === "thinking") {
              chatMessage.thinkingContent =
                (chatMessage.thinkingContent || "") + block.content;
            }
          }
        }
        return chatMessage;
      }),
    [messages],
  );

  useEffect(() => {
    if (contentMode === "transcript") {
      const messagesEnd = messagesEndRef.current;
      if (messagesEnd && typeof messagesEnd.scrollIntoView === "function") {
        messagesEnd.scrollIntoView({ behavior: "smooth" });
      }
    }
  }, [chatMessages.length, contentMode]);

  const handleSelect = useCallback((id: string) => {
    selectionClearedRef.current = false;
    setSelectedSessionId(id);
  }, []);

  const transcriptEmptyStateMessage = useMemo(() => {
    if (transcriptStatus?.content_state === "unparseable") {
      return "Transcript exists but could not be parsed";
    }
    if (transcriptStatus?.content_state === "missing") {
      return "Session has no transcript";
    }
    return "No messages yet";
  }, [transcriptStatus]);

  const summaryMarkdown =
    selectedSession?.summary_markdown ?? selectedSession?.digest_markdown ?? null;
  const selectedSessionStatus = selectedSession?.status ?? selectedEntry?.status ?? null;
  const transcriptUnavailable =
    transcriptStatus?.content_state === "missing" ||
    transcriptStatus?.content_state === "unparseable";
  const hideResumeAndSwap =
    selectedSessionStatus === "expired" && transcriptUnavailable;
  const showSummaryButton =
    selectedEntry != null && (!hideResumeAndSwap || Boolean(summaryMarkdown));
  const showResumeButton =
    selectedEntry?.type === "session" &&
    selectedSessionId != null &&
    selectedSessionStatus !== "active" &&
    !hideResumeAndSwap &&
    Boolean(onResumeSession);
  const showSwapButton = Boolean(selectedEntry && onSwapSession && !hideResumeAndSwap);

  const handleExpire = useCallback(
    (entry: WatchingSessionEntry) => {
      setExpiringIds((prev) => new Set(prev).add(entry.id));
      if (entry.type === "agent" && entry.runId) {
        onKillAgent?.(entry.runId);
      } else {
        onExpireSession?.(entry.id);
      }
    },
    [onExpireSession, onKillAgent],
  );

  const handleMenuButtonClick = useCallback(
    (event: React.MouseEvent<HTMLButtonElement>, entry: WatchingSessionEntry) => {
      event.stopPropagation();
      const rect = event.currentTarget.getBoundingClientRect();
      const menuWidth = 160;
      setCtxMenu({ x: rect.left - menuWidth, y: rect.top, entry });
    },
    [],
  );

  const closeCtxMenu = useCallback(() => setCtxMenu(null), []);

  useEffect(() => {
    if (!ctxMenu) return;
    const handler = () => setCtxMenu(null);
    window.addEventListener("click", handler);
    return () => window.removeEventListener("click", handler);
  }, [ctxMenu]);

  const openModal = useCallback(
    (mode: InteractionMode, entry: WatchingSessionEntry) => {
      closeCtxMenu();
      setModalMode(mode);
      setModalEntry(entry);
    },
    [closeCtxMenu],
  );

  const closeModal = useCallback(() => {
    setModalMode(null);
    setModalEntry(null);
  }, []);

  const emptyListMessage =
    statusFilter === "expired" ? "No expired sessions" : "No live sessions";

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <input
          type="search"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="Search sessions"
          className="flex-1 rounded-md border border-border bg-background px-3 py-1.5 text-sm text-foreground outline-none"
        />
        <select
          aria-label="Session status filter"
          value={statusFilter}
          onChange={(event) =>
            setStatusFilter(event.target.value as SessionStatusFilter)
          }
          className="rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground outline-none"
        >
          <option value="live">Live</option>
          <option value="expired">Expired</option>
        </select>
      </div>

      <div
        className={`overflow-y-auto ${selectedSessionId ? "border-b border-border" : "flex-1"}`}
        style={selectedSessionId ? { height: `${topHeight}%` } : undefined}
      >
        {isLoading && entries.length === 0 ? (
          <div className="activity-tab-empty">
            <p>Loading sessions...</p>
          </div>
        ) : fetchError && entries.length === 0 ? (
          <div className="activity-tab-empty">
            <p>{fetchError}</p>
          </div>
        ) : entries.length === 0 ? (
          <div className="activity-tab-empty">
            <p>{emptyListMessage}</p>
            <p className="text-xs text-muted-foreground mt-1">
              Matching sessions will appear here.
            </p>
          </div>
        ) : (
          entries.map((entry) => {
            const isSelected = entry.id === selectedSessionId;
            const isPaused = entry.status !== "active";
            const displayLabel = entry.seqNum
              ? `#${entry.seqNum}: ${entry.label}`
              : entry.label;

            return (
              <div
                key={`${entry.type}-${entry.id}`}
                className={`session-entry${isSelected ? " session-entry--active" : ""}${isPaused ? " session-entry--paused" : ""}`}
                onClick={() => handleSelect(entry.id)}
              >
                <div className="flex items-center gap-2 min-w-0 flex-1">
                  <SourceIcon source={entry.provider} size={14} />
                  <span className="text-sm text-foreground truncate">
                    {displayLabel}
                  </span>
                </div>
                <div className="flex items-center gap-1.5">
                  {renderBadges(entry)}
                  {entry.status !== "expired" && (
                    <button
                      className="session-more-btn"
                      onClick={(event) => handleMenuButtonClick(event, entry)}
                      title="Session actions"
                      aria-label="Session actions"
                    >
                      <svg
                        width="12"
                        height="12"
                        viewBox="0 0 24 24"
                        fill="currentColor"
                      >
                        <circle cx="12" cy="5" r="2" />
                        <circle cx="12" cy="12" r="2" />
                        <circle cx="12" cy="19" r="2" />
                      </svg>
                    </button>
                  )}
                </div>
              </div>
            );
          })
        )}
      </div>

      {selectedSessionId && entries.length > 0 && (
        <ResizeHandle
          direction="vertical"
          onResize={setTopHeight}
          panelHeight={topHeight}
          minHeight={15}
          maxHeight={80}
        />
      )}

      {selectedSessionId && (
        <div className="flex-1 flex flex-col min-h-0">
          <div
            className="flex items-center gap-3 px-3 border-b border-border"
            style={{ height: 40, background: "var(--bg-secondary)" }}
          >
            <div className="min-w-0 flex-1">
              <span className="block truncate text-xs text-muted-foreground">
                Watching{" "}
                {selectedEntry
                  ? selectedEntry.seqNum
                    ? `#${selectedEntry.seqNum}: ${selectedEntry.label}`
                    : selectedEntry.label
                  : "session"}
              </span>
            </div>
            <div className="flex flex-none items-center gap-3">
              {showSummaryButton && (
                <button
                  type="button"
                  className="session-pane-action"
                  onClick={() =>
                    setContentMode((current) =>
                      current === "summary" ? "transcript" : "summary",
                    )
                  }
                >
                  {contentMode === "summary" ? "Transcript" : "Summary"}
                </button>
              )}
              {showResumeButton && (
                <button
                  type="button"
                  className="session-pane-action"
                  onClick={() => {
                    if (selectedSessionId) {
                      void onResumeSession?.(selectedSessionId);
                    }
                  }}
                >
                  Resume
                </button>
              )}
              {showSwapButton && selectedEntry && (
                <button
                  type="button"
                  className="session-pane-action"
                  onClick={() => {
                    if (selectedSessionId) {
                      onSwapSession?.({
                        sessionId: selectedSessionId,
                        sessionType: selectedEntry.sessionType ?? null,
                        agentRunId: selectedEntry.agentRunId ?? null,
                      });
                    }
                  }}
                >
                  Swap
                </button>
              )}
            </div>
          </div>

          <ArtifactContext.Provider value={noopArtifactCtx}>
            {contentMode === "summary" ? (
              <div className="flex-1 overflow-y-auto p-4">
                {summaryMarkdown ? (
                  <div className="message-content">
                    <MemoizedMarkdown
                      content={summaryMarkdown}
                      id={`watch-summary-${selectedSessionId}`}
                    />
                  </div>
                ) : (
                  <div className="activity-tab-empty">
                    <p>No summary available</p>
                  </div>
                )}
              </div>
            ) : (
              <div className="flex-1 overflow-y-auto chat-scaled">
                {isLoadingDetail ? (
                  <div className="activity-tab-empty">
                    <p>Loading messages...</p>
                  </div>
                ) : chatMessages.length === 0 ? (
                  <div className="activity-tab-empty">
                    <p>{transcriptEmptyStateMessage}</p>
                  </div>
                ) : (
                  <>
                    {chatMessages.map((message) => (
                      <MessageItem key={message.id} message={message} />
                    ))}
                    <div ref={messagesEndRef} />
                  </>
                )}
              </div>
            )}
          </ArtifactContext.Provider>
        </div>
      )}

      {ctxMenu && (
        <>
          <div className="session-ctx-backdrop" onClick={closeCtxMenu} />
          <div
            className="session-ctx-menu"
            style={{ position: "fixed", left: ctxMenu.x, top: ctxMenu.y }}
          >
            <button
              className="session-ctx-item"
              onClick={() => openModal("context", ctxMenu.entry)}
            >
              Send Context
            </button>
            <button
              className="session-ctx-item"
              onClick={() => openModal("command", ctxMenu.entry)}
            >
              Send Command
            </button>
            {ctxMenu.entry.hasTmux && (
              <>
                <button
                  className="session-ctx-item"
                  onClick={() => openModal("keys", ctxMenu.entry)}
                >
                  Send Keys
                </button>
                <button
                  className="session-ctx-item"
                  onClick={() => openModal("pane", ctxMenu.entry)}
                >
                  Capture Pane
                </button>
              </>
            )}
            {ctxMenu.entry.status !== "expired" && (
              <>
                <div className="session-ctx-divider" />
                <button
                  className="session-ctx-item session-ctx-item--destructive"
                  onClick={() => {
                    const entry = ctxMenu.entry;
                    closeCtxMenu();
                    handleExpire(entry);
                  }}
                >
                  Expire Session
                </button>
              </>
            )}
          </div>
        </>
      )}

      {modalMode && modalEntry && (
        <SessionInteractionModal
          open={true}
          onClose={closeModal}
          mode={modalMode}
          entry={{
            id: modalEntry.id,
            type: modalEntry.type === "agent" ? "agent" : "cli",
            label: modalEntry.label,
            hasTmux: modalEntry.hasTmux,
            runId: modalEntry.runId,
            seqNum: modalEntry.seqNum,
          }}
          fromSessionId={chatSessionId}
        />
      )}
    </div>
  );
});
