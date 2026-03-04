import type { GobbySession } from "../../hooks/useSessions";
import type { AgentDefInfo } from "../../hooks/useAgentDefinitions";
import { formatRelativeTime } from "../../utils/formatTime";
import { useState, useEffect, useRef, useMemo } from "react";
import { AgentPickerDropdown } from "./AgentPickerDropdown";

interface AgentInfo {
  run_id: string;
  provider: string;
  pid?: number;
  mode?: string;
  started_at?: string;
  session_id?: string;
  tmux_session_name?: string;
}

interface ConversationPickerProps {
  sessions: GobbySession[];
  activeSessionId: string | null;
  deletingIds?: Set<string>;
  onNewChat: (agentName?: string) => void;
  onSelectSession: (session: GobbySession) => void;
  onDeleteSession?: (session: GobbySession) => void;
  onRenameSession?: (id: string, title: string) => void;
  agents?: AgentInfo[];
  onNavigateToAgent?: (agent: AgentInfo) => void;
  onKillAgent?: (runId: string) => void;
  cliSessions?: GobbySession[];
  viewingSessionId?: string | null;
  attachedSessionId?: string | null;
  onViewCliSession?: (session: GobbySession) => void;
  onDetachFromSession?: () => void;
  agentDefinitions?: AgentDefInfo[];
  agentGlobalDefs?: AgentDefInfo[];
  agentProjectDefs?: AgentDefInfo[];
  agentShowScopeToggle?: boolean;
  agentHasGlobal?: boolean;
  agentHasProject?: boolean;
}

const PROVIDER_COLORS: Record<string, string> = {
  claude: "#c084fc",
  gemini: "#4ade80",
  codex: "#3b82f6",
  unknown: "#737373",
};

const SOURCE_COLORS: Record<string, string> = {
  claude_code: "#c084fc",
  gemini_cli: "#4ade80",
  codex: "#3b82f6",
  windsurf: "#38bdf8",
  cursor: "#f472b6",
  copilot: "#818cf8",
  unknown: "#737373",
};

const TERMINAL_INITIAL_LIMIT = 5;

export function ConversationPicker({
  sessions,
  activeSessionId,
  deletingIds,
  onNewChat,
  onSelectSession,
  onDeleteSession,
  onRenameSession,
  agents = [],
  onNavigateToAgent,
  onKillAgent,
  cliSessions = [],
  viewingSessionId,
  onViewCliSession,
  onDetachFromSession,
  agentDefinitions = [],
  agentGlobalDefs = [],
  agentProjectDefs = [],
  agentShowScopeToggle = false,
  agentHasGlobal = false,
  agentHasProject = false,
}: ConversationPickerProps) {
  const [search, setSearch] = useState("");
  const [isOpen, setIsOpen] = useState(true);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState("");
  const saveOnBlurRef = useRef(true);
  const [showAllTerminal, setShowAllTerminal] = useState(false);
  const [showAgentPicker, setShowAgentPicker] = useState(false);

  const filtered = search
    ? sessions.filter(
        (s) =>
          (s.title && s.title.toLowerCase().includes(search.toLowerCase())) ||
          s.ref.toLowerCase().includes(search.toLowerCase()),
      )
    : sessions;

  return (
    <div className={`conversation-picker ${isOpen ? "" : "collapsed"}`}>
      <div className="conversation-picker-header">
        {isOpen && <span className="conversation-picker-title">Chats</span>}
        <div className="conversation-picker-actions">
          {isOpen && (
            <>
              <button
                type="button"
                className="terminals-action-btn"
                onClick={() => {
                  // If only 1 agent (or none), start chat immediately
                  if (agentDefinitions.length <= 1) {
                    onNewChat();
                  } else {
                    setShowAgentPicker(!showAgentPicker);
                  }
                }}
                title="New Chat"
              >
                <PlusIcon />
              </button>
              {showAgentPicker && (
                <AgentPickerDropdown
                  definitions={agentDefinitions}
                  globalDefs={agentGlobalDefs}
                  projectDefs={agentProjectDefs}
                  showScopeToggle={agentShowScopeToggle}
                  hasGlobal={agentHasGlobal}
                  hasProject={agentHasProject}
                  onSelect={(agentName) => {
                    onNewChat(agentName);
                    setShowAgentPicker(false);
                  }}
                  onClose={() => setShowAgentPicker(false)}
                />
              )}
            </>
          )}
          <button
            type="button"
            className="terminals-sidebar-toggle"
            onClick={() => setIsOpen(!isOpen)}
            title={isOpen ? "Collapse" : "Expand"}
          >
            {isOpen ? "\u25C0" : "\u25B6"}
          </button>
        </div>
      </div>

      {isOpen && (
        <>
          <div className="conversation-picker-search">
            <input
              className="sessions-filter-input"
              type="text"
              placeholder="Search chats..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>

          <div className="conversation-picker-content">
          <div className="session-group">
            <div className="sessions-list">
              {filtered.length === 0 && (
                <div className="terminals-empty-sidebar">No conversations</div>
              )}
              {filtered.map((session) => {
                const seqLabel = session.seq_num != null ? `#${session.seq_num}` : null;
                const titleText = session.title || `Chat ${session.ref}`;
                const title = seqLabel ? `${seqLabel}: ${titleText}` : titleText;
                const isActive = session.external_id === activeSessionId && !viewingSessionId;
                const isDeleting = deletingIds?.has(session.id) ?? false;
                return (
                  <div
                    key={session.id}
                    className={`session-item ${isActive ? "attached" : ""} ${isDeleting ? "deleting" : ""}`}
                    onClick={() => {
                      if (!isDeleting && editingId !== session.id)
                        onSelectSession(session);
                    }}
                    role="button"
                    tabIndex={0}
                    onKeyDown={(e) => {
                      if (
                        editingId !== session.id &&
                        (e.key === "Enter" || e.key === " ")
                      ) {
                        e.preventDefault();
                        onSelectSession(session);
                      }
                    }}
                  >
                    <div className="session-item-main">
                      <span className={`session-source-dot ${session.status === "paused" ? "status-paused" : "web-chat"}`} />
                      {editingId === session.id ? (
                        <input
                          className="session-name-input"
                          value={editValue}
                          onChange={(e) => setEditValue(e.target.value)}
                          onBlur={() => {
                            if (saveOnBlurRef.current && onRenameSession) {
                              onRenameSession(session.id, editValue);
                            }
                            saveOnBlurRef.current = true;
                            setEditingId(null);
                          }}
                          onKeyDown={(e) => {
                            if (e.key === "Enter") {
                              saveOnBlurRef.current = false;
                              if (onRenameSession)
                                onRenameSession(session.id, editValue);
                              setEditingId(null);
                            } else if (e.key === "Escape") {
                              saveOnBlurRef.current = false;
                              setEditingId(null);
                            }
                          }}
                          onClick={(e) => e.stopPropagation()}
                          aria-label="Rename chat"
                          autoFocus
                        />
                      ) : (
                        <span
                          className="session-name"
                          title={title}
                          onDoubleClick={(e) => {
                            if (!onRenameSession) return;
                            e.stopPropagation();
                            setEditingId(session.id);
                            setEditValue(title);
                          }}
                        >
                          {title}
                        </span>
                      )}
                    </div>
                    <div className="session-item-actions">
                      <span className="session-pid">
                        {formatRelativeTime(session.updated_at)}
                      </span>
                      {onDeleteSession && !isDeleting && (
                        <button
                          type="button"
                          className="session-delete-btn"
                          title="Delete chat"
                          onClick={(e) => {
                            e.stopPropagation();
                            onDeleteSession(session);
                          }}
                        >
                          <TrashIcon />
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>

          {(agents.length > 0 || cliSessions.length > 0) && (
            <div className="session-group">
              <div className="session-group-label">
                Terminal Sessions ({agents.length + cliSessions.length})
              </div>
              {agents.map((agent) => (
                <div
                  key={agent.run_id}
                  className="session-item"
                  {...(onNavigateToAgent
                    ? {
                        onClick: () => onNavigateToAgent(agent),
                        role: "button" as const,
                        tabIndex: 0,
                        onKeyDown: (e: React.KeyboardEvent) => {
                          if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            onNavigateToAgent(agent);
                          }
                        },
                      }
                    : {})}
                >
                  <div className="session-item-main">
                    <span
                      className="session-source-dot"
                      style={{
                        background:
                          PROVIDER_COLORS[agent.provider] ??
                          PROVIDER_COLORS.unknown,
                      }}
                    />
                    <span className="session-name">{agent.provider}</span>
                    {agent.mode && (
                      <span className="session-badge agent-badge">
                        {agent.mode}
                      </span>
                    )}
                  </div>
                  <div className="session-item-actions">
                    <span className="session-pid">
                      <AgentUptime startedAt={agent.started_at} />
                    </span>
                    {onKillAgent && (
                      <button
                        type="button"
                        className="session-delete-btn"
                        title="Kill agent"
                        onClick={(e) => {
                          e.stopPropagation();
                          onKillAgent(agent.run_id);
                        }}
                      >
                        <TrashIcon />
                      </button>
                    )}
                  </div>
                </div>
              ))}
              {(showAllTerminal
                ? cliSessions
                : cliSessions.slice(0, Math.max(0, TERMINAL_INITIAL_LIMIT - agents.length))
              ).map((session) => {
                const seqLabel = session.seq_num != null ? `#${session.seq_num}` : null;
                const titleText = session.title || session.ref || "CLI Session";
                const title = seqLabel ? `${seqLabel}: ${titleText}` : titleText;
                const isViewing = session.id === viewingSessionId;
                const isPaused = session.status === "paused";
                return (
                  <div
                    key={session.id}
                    className={`session-item ${isViewing ? "attached" : ""} ${isPaused ? "session-item-muted" : ""}`}
                    onClick={() => {
                      if (isViewing) {
                        onDetachFromSession?.();
                      } else {
                        onViewCliSession?.(session);
                      }
                    }}
                    role="button"
                    tabIndex={0}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        if (isViewing) onDetachFromSession?.();
                        else onViewCliSession?.(session);
                      }
                    }}
                  >
                    <div className="session-item-main">
                      <span
                        className="session-source-dot"
                        style={{
                          background:
                            SOURCE_COLORS[session.source] ??
                            SOURCE_COLORS.unknown,
                        }}
                      />
                      <span className="session-name" title={title}>
                        {title}
                      </span>
                      {session.model && (
                        <span className="session-badge">{session.model}</span>
                      )}
                    </div>
                    <div className="session-item-actions">
                      <span className="session-pid">
                        {formatRelativeTime(session.updated_at)}
                      </span>
                    </div>
                  </div>
                );
              })}
              {!showAllTerminal && agents.length + cliSessions.length > TERMINAL_INITIAL_LIMIT && (
                <button
                  type="button"
                  className="session-show-more"
                  onClick={() => setShowAllTerminal(true)}
                >
                  Show {agents.length + cliSessions.length - TERMINAL_INITIAL_LIMIT} more
                </button>
              )}
              {showAllTerminal && agents.length + cliSessions.length > TERMINAL_INITIAL_LIMIT && (
                <button
                  type="button"
                  className="session-show-more"
                  onClick={() => setShowAllTerminal(false)}
                >
                  Show less
                </button>
              )}
            </div>
          )}
          </div>
        </>
      )}
    </div>
  );
}

function AgentUptime({ startedAt }: { startedAt?: string }) {
  const startTime = useMemo(() => {
    if (startedAt) {
      const t = new Date(startedAt).getTime();
      if (!Number.isNaN(t)) return t;
    }
    return null;
  }, [startedAt]);
  const [uptime, setUptime] = useState(startTime ? "0s" : "—");

  useEffect(() => {
    if (startTime === null) return;
    const update = () => {
      const elapsed = Math.floor((Date.now() - startTime) / 1000);
      if (elapsed < 60) setUptime(`${elapsed}s`);
      else if (elapsed < 3600) setUptime(`${Math.floor(elapsed / 60)}m`);
      else
        setUptime(
          `${Math.floor(elapsed / 3600)}h${Math.floor((elapsed % 3600) / 60)}m`,
        );
    };
    update();
    const interval = setInterval(update, 1000);
    return () => clearInterval(interval);
  }, [startTime]);

  return <>{uptime}</>;
}

function TrashIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <polyline points="3 6 5 6 21 6" />
      <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" />
    </svg>
  );
}

function PlusIcon() {
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
    >
      <line x1="12" y1="5" x2="12" y2="19" />
      <line x1="5" y1="12" x2="19" y2="12" />
    </svg>
  );
}
