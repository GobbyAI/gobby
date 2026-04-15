import { memo, useState, useEffect, useCallback, useRef, useMemo } from "react";
import { ResizeHandle } from "../chat/artifacts/ResizeHandle";
import { SourceIcon } from "../shared/SourceIcon";
import type { GobbySession } from "../../hooks/useSessions";
import { useSessionDetail } from "../../hooks/useSessionDetail";
import { MessageItem } from "../chat/MessageItem";
import type { ChatMessage, SwappedSessionTarget } from "../../types/chat";
import { ArtifactContext } from "../chat/artifacts/ArtifactContext";
import { getSessionTitleText } from "../../lib/sessionTitle";
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
  projectId?: string | null;
  onKillAgent?: (runId: string) => void;
  onExpireSession?: (sessionId: string) => void;
  chatSessionId?: string;
  isMobile?: boolean;
  focusSessionId?: string | null;
  onFocusHandled?: () => void;
  onSwapSession?: (target: SwappedSessionTarget) => void;
}

interface SessionEntry {
  id: string;
  type: "agent" | "cli";
  label: string;
  provider: string;
  status: "active" | "paused";
  sessionType?: string;
  externalId?: string;
  agentRunId?: string | null;
  runId?: string;
  startedAt?: string;
  seqNum?: number | null;
  hasTmux: boolean;
}

interface SessionContextMenu {
  x: number;
  y: number;
  entry: SessionEntry;
}

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

function renderBadges(entry: SessionEntry) {
  const typeBadge = getSessionTypeBadge(entry.sessionType);
  const agentBadge = getAgentBadge(entry.agentRunId);
  return (
    <>
      <span className={`session-kind-badge ${typeBadge.className}`}>
        {typeBadge.label}
      </span>
      {agentBadge && (
        <span className={`session-kind-badge ${agentBadge.className}`}>
          {agentBadge.label}
        </span>
      )}
    </>
  );
}

export const SessionsTab = memo(function SessionsTab({
  projectId,
  onKillAgent,
  onExpireSession,
  chatSessionId,
  focusSessionId,
  onFocusHandled,
  onSwapSession,
}: SessionsTabProps) {
  const [agents, setAgents] = useState<RunningAgent[]>([]);
  const [activitySessions, setActivitySessions] = useState<GobbySession[]>([]);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(
    null,
  );
  const [topHeight, setTopHeight] = useState(35);
  const [expiringIds, setExpiringIds] = useState<Set<string>>(new Set());
  const [ctxMenu, setCtxMenu] = useState<SessionContextMenu | null>(null);
  const [modalMode, setModalMode] = useState<InteractionMode | null>(null);
  const [modalEntry, setModalEntry] = useState<SessionEntry | null>(null);
  const initialSelectionAppliedRef = useRef(false);
  const selectionClearedRef = useRef(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  // No-op artifact context for MessageItem rendering
  const noopArtifactCtx = useMemo(
    () => ({
      openCodeAsArtifact: () => {},
      openFileAsArtifact: () => {},
    }),
    [],
  );

  // Fetch agents and sessions from API
  const fetchData = useCallback(async () => {
    const baseUrl = getBaseUrl();
    const projectParam = projectId
      ? `&project_id=${encodeURIComponent(projectId)}`
      : "";
    try {
      const [agentsRes, activeRes, pausedRes] = await Promise.all([
        fetch(`${baseUrl}/api/agents/running`).then((r) =>
          r.ok ? r.json() : { agents: [] },
        ),
        fetch(
          `${baseUrl}/api/sessions?status=active&limit=50${projectParam}`,
        ).then((r) => (r.ok ? r.json() : { sessions: [] })),
        fetch(
          `${baseUrl}/api/sessions?status=paused&limit=20${projectParam}`,
        ).then((r) => (r.ok ? r.json() : { sessions: [] })),
      ]);
      setAgents(agentsRes.agents ?? agentsRes ?? []);
      const isWatchable = (s: GobbySession) =>
        s.source !== "pipeline" && s.source !== "cron";
      const active = (activeRes.sessions ?? activeRes ?? []).filter(isWatchable);
      const paused = (pausedRes.sessions ?? pausedRes ?? []).filter(isWatchable);
      setActivitySessions([...active, ...paused]);
      setExpiringIds(new Set());
      setFetchError(null);
    } catch (err) {
      console.error("Failed to fetch sessions:", err);
      setFetchError("Failed to load sessions");
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  // Initial fetch + poll every 5s
  useEffect(() => {
    fetchData();
    const interval = setInterval(fetchData, 5000);
    return () => clearInterval(interval);
  }, [fetchData]);

  // Build session entries, deduplicating agents that also appear in sessions
  const entries: SessionEntry[] = useMemo(() => {
    // Collect session IDs owned by agents so we can skip them in the CLI list
    const agentSessionIds = new Set(
      agents.map((a) => a.session_id).filter(Boolean) as string[],
    );

    const agentEntries: SessionEntry[] = agents.map((a) => {
      // Find matching session for richer metadata
      const matchedSession = a.session_id
        ? activitySessions.find((s) => s.id === a.session_id)
        : undefined;

      return {
        id: a.session_id ?? a.run_id,
        type: "agent" as const,
        label:
          matchedSession
            ? getSessionTitleText(matchedSession.title)
            : (a.mode === "agent"
            ? `Agent ${a.run_id.slice(0, 8)}`
            : `Session ${a.run_id.slice(0, 8)}`),
        provider: a.provider,
        status: "active" as const,
        sessionType: matchedSession?.session_type,
        externalId: matchedSession?.external_id,
        agentRunId: matchedSession?.agent_run_id ?? a.run_id,
        runId: a.run_id,
        startedAt: a.started_at,
        seqNum: matchedSession?.seq_num,
        hasTmux: !!a.pid,
      };
    });

    const sessionEntries: SessionEntry[] = activitySessions
      .filter((s) => !agentSessionIds.has(s.id))
      .map((s) => {
        return {
          id: s.id,
          type: "cli" as const,
          label: getSessionTitleText(s.title),
          provider: s.source ?? "unknown",
          status: (s.status === "paused" ? "paused" : "active") as
            | "active"
            | "paused",
          sessionType: s.session_type,
          externalId: s.external_id,
          agentRunId: s.agent_run_id,
          startedAt: s.updated_at,
          seqNum: s.seq_num,
          hasTmux: !!s.terminal_context,
        };
      });

    // Filter out entries being expired
    return [...agentEntries, ...sessionEntries].filter(
      (e) => !expiringIds.has(e.id) && e.id !== chatSessionId,
    );
  }, [agents, activitySessions, chatSessionId, expiringIds]);

  // Seed the watching pane once on load, keep it stable, and only fall back
  // when the selected entry disappears or the list becomes empty.
  useEffect(() => {
    if (loading) {
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
      setSelectedSessionId(hasFocusedEntry ? focusSessionId : entries[0].id);
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
    loading,
    onFocusHandled,
    selectedSessionId,
  ]);

  // Fetch selected session messages
  const { messages, isLoading, transcriptStatus } =
    useSessionDetail(selectedSessionId);
  const chatMessages: ChatMessage[] = useMemo(
    () =>
      messages.map((m) => {
        const chatMsg: ChatMessage = {
          id: m.id,
          role: (m.role as "user" | "assistant" | "system") || "assistant",
          content: m.content || "",
          timestamp: new Date(m.timestamp),
          contentBlocks: m.content_blocks,
        };
        if (m.content_blocks) {
          for (const block of m.content_blocks) {
            if (block.type === "tool_chain" && block.tool_calls) {
              // Filter out answered AskUserQuestion calls from activity panel
              const filtered = block.tool_calls.filter(
                (tc: { tool_name?: string; status?: string }) =>
                  !(
                    tc.tool_name === "AskUserQuestion" &&
                    tc.status !== "calling"
                  ),
              );
              block.tool_calls = filtered;
              chatMsg.toolCalls = [...(chatMsg.toolCalls || []), ...filtered];
            } else if (block.type === "thinking") {
              chatMsg.thinkingContent =
                (chatMsg.thinkingContent || "") + block.content;
            }
          }
        }
        return chatMsg;
      }),
    [messages],
  );

  // Auto-scroll when new messages arrive
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [chatMessages.length]);

  const handleSelect = useCallback((id: string) => {
    selectionClearedRef.current = false;
    setSelectedSessionId(id);
  }, []);

  const selectedEntry = useMemo(
    () => entries.find((entry) => entry.id === selectedSessionId) ?? null,
    [entries, selectedSessionId],
  );

  const emptyStateMessage = useMemo(() => {
    if (transcriptStatus?.content_state === "unparseable") {
      return "Transcript exists but could not be parsed";
    }
    if (transcriptStatus?.content_state === "missing") {
      return "Session has no transcript";
    }
    return "No messages yet";
  }, [transcriptStatus]);

  const handleExpire = useCallback(
    (entry: SessionEntry) => {
      setExpiringIds((prev) => new Set(prev).add(entry.id));
      if (entry.type === "agent" && entry.runId) {
        onKillAgent?.(entry.runId);
      } else {
        onExpireSession?.(entry.id);
      }
    },
    [onKillAgent, onExpireSession],
  );

  const handleMenuButtonClick = useCallback(
    (e: React.MouseEvent<HTMLButtonElement>, entry: SessionEntry) => {
      e.stopPropagation();
      const rect = e.currentTarget.getBoundingClientRect();
      const menuWidth = 160;
      setCtxMenu({ x: rect.left - menuWidth, y: rect.top, entry });
    },
    [],
  );

  const closeCtxMenu = useCallback(() => setCtxMenu(null), []);

  // Dismiss context menu on outside click
  useEffect(() => {
    if (!ctxMenu) return;
    const handler = () => setCtxMenu(null);
    window.addEventListener("click", handler);
    return () => window.removeEventListener("click", handler);
  }, [ctxMenu]);

  const openModal = useCallback(
    (mode: InteractionMode, entry: SessionEntry) => {
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

  if (loading) {
    return (
      <div className="activity-tab-empty">
        <p>Loading sessions...</p>
      </div>
    );
  }

  if (fetchError && entries.length === 0) {
    return (
      <div className="activity-tab-empty">
        <p>{fetchError}</p>
      </div>
    );
  }

  if (entries.length === 0) {
    return (
      <div className="activity-tab-empty">
        <p>No active sessions</p>
        <p className="text-xs text-muted-foreground mt-1">
          Agent, terminal, and web chat sessions will appear here when active
        </p>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      {/* Session list */}
      <div
        className={`overflow-y-auto ${selectedSessionId ? "border-b border-border" : "flex-1"}`}
        style={selectedSessionId ? { height: `${topHeight}%` } : undefined}
      >
        {entries.map((entry) => {
          const isSelected = entry.id === selectedSessionId;
          const isPaused = entry.status === "paused";
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
                <button
                  className="session-more-btn"
                  onClick={(e) => handleMenuButtonClick(e, entry)}
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
              </div>
            </div>
          );
        })}
      </div>

      {/* Resize handle */}
      {selectedSessionId && (
        <ResizeHandle
          direction="vertical"
          onResize={setTopHeight}
          panelHeight={topHeight}
          minHeight={15}
          maxHeight={80}
        />
      )}

      {/* Message area */}
      {selectedSessionId && (
        <div className="flex-1 flex flex-col min-h-0">
          {/* Session header */}
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
              {selectedEntry && onSwapSession && (
                <button
                  type="button"
                  className="session-pane-action"
                  onClick={() => {
                    if (selectedSessionId) {
                      onSwapSession({
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

          {/* Messages */}
          <ArtifactContext.Provider value={noopArtifactCtx}>
            <div className="flex-1 overflow-y-auto chat-scaled">
              {isLoading ? (
                <div className="activity-tab-empty">
                  <p>Loading messages...</p>
                </div>
              ) : chatMessages.length === 0 ? (
                <div className="activity-tab-empty">
                  <p>{emptyStateMessage}</p>
                </div>
              ) : (
                <>
                  {chatMessages.map((msg) => (
                    <MessageItem key={msg.id} message={msg} />
                  ))}
                  <div ref={messagesEndRef} />
                </>
              )}
            </div>
          </ArtifactContext.Provider>
        </div>
      )}

      {/* Context menu */}
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
          </div>
        </>
      )}

      {/* Interaction modal */}
      {modalMode && modalEntry && (
        <SessionInteractionModal
          open={true}
          onClose={closeModal}
          mode={modalMode}
          entry={modalEntry}
          fromSessionId={chatSessionId}
        />
      )}
    </div>
  );
});
