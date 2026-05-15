import { memo, useState, useEffect, useCallback, useRef, useMemo } from "react";

import { useSessionDetail } from "../../hooks/useSessionDetail";
import type { GobbySession } from "../../types/sessions";
import type { SwappedSessionTarget } from "../../types/chat";
import { getSessionTitleText } from "../../lib/sessionTitle";
import { SegmentedControl } from "../ui/SegmentedControl";
import { ResizeHandle } from "../chat/artifacts/ResizeHandle";
import { ArtifactContext } from "../chat/artifacts/ArtifactContext";
import { MemoizedMarkdown } from "../shared/MemoizedMarkdown";
import { SourceIcon } from "../shared/SourceIcon";
import {
  ClipboardListIcon,
  PlayIcon,
  SwapIcon,
  TranscriptIcon,
} from "../icons";
import {
  SessionInteractionModal,
  type InteractionMode,
} from "./SessionInteractionModal";
import { SessionsFilterDropdown } from "./SessionsFilterDropdown";
import {
  countActiveFilters,
  DEFAULT_LIVE_STATUSES,
  defaultSessionsFilters,
  matchesSessionsFilters,
  type SessionStatus,
  type SessionsFilters,
} from "./sessionsFilters";
import { DEFAULT_TOP_PANEL_PERCENT } from "./constants";
import { ActivityPanelEmpty, SessionsEmptyIcon } from "./ActivityPanelEmpty";
import { ActivityPanelSearch } from "./ActivityPanelSearch";
import { ActivityRowStatusDot } from "./ActivityRowStatusDot";
import {
  type RunningAgent,
  type WatchingSessionEntry,
  type SessionContextMenu,
  WATCHING_SESSION_ID_KEY,
  HIDDEN_SOURCES,
  getBaseUrl,
  resolveLocalFlag,
  renderBadges,
  matchesSearch,
  entryTimestamp,
  parseTimestamp,
} from "./SessionsTab.helpers";
import { WatchingTranscript } from "./WatchingTranscript";

// Canonical session-providers — sessions can exist for any of these regardless
// of current local availability, so the filter affordance reflects what was
// used historically rather than what is installable right now.
const SESSION_PROVIDERS: readonly string[] = [
  "claude",
  "codex",
  "droid",
  "gemini",
  "qwen",
];

const STATUS_MODE_OPTIONS = [
  { value: "live" as const, label: "Live" },
  { value: "expired" as const, label: "Expired" },
] as const;

function resolveSessionSummaryMarkdown(
  ...sessions: Array<
    Pick<GobbySession, "summary_markdown" | "digest_markdown"> | null | undefined
  >
): string | null {
  for (const session of sessions) {
    if (session?.summary_markdown?.trim()) {
      return session.summary_markdown;
    }
  }
  for (const session of sessions) {
    if (session?.digest_markdown?.trim()) {
      return session.digest_markdown;
    }
  }
  return null;
}

interface SessionsTabProps {
  sessions?: GobbySession[];
  isLoadingSessions?: boolean;
  filters?: SessionsFilters;
  onFiltersChange?: (filters: SessionsFilters) => void;
  onKillAgent?: (runId: string) => Promise<boolean | void> | boolean | void;
  onExpireSession?: (sessionId: string) => Promise<boolean | void> | boolean | void;
  onResumeSession?: (sessionId: string) => Promise<string> | string | void;
  chatSessionId?: string | null;
  focusSessionId?: string | null;
  onFocusHandled?: () => void;
  onSwapSession?: (target: SwappedSessionTarget) => void;
}

function FilterEmptyState({
  message,
  hasActiveFilters,
  activeFilterCount,
  onClear,
  hint = "Matching sessions will appear here.",
}: {
  message: string;
  hasActiveFilters: boolean;
  activeFilterCount: number;
  onClear: () => void;
  hint?: string;
}) {
  return (
    <ActivityPanelEmpty
      icon={<SessionsEmptyIcon />}
      heading="Sessions"
      body={hasActiveFilters && activeFilterCount > 0 ? message : hint}
      footer={
        hasActiveFilters && activeFilterCount > 0 ? (
          <button
            type="button"
            className="text-xs text-accent hover:underline"
            onClick={onClear}
          >
            Clear filters
          </button>
        ) : null
      }
    />
  );
}

type WatchingContentMode = "transcript" | "summary";

export const SessionsTab = memo(function SessionsTab({
  sessions = [],
  isLoadingSessions = false,
  filters: filtersProp,
  onFiltersChange,
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
  const [searchInput, setSearchInput] = useState("");
  const [contentMode, setContentMode] = useState<WatchingContentMode>("transcript");
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(() => {
    try {
      return localStorage.getItem(WATCHING_SESSION_ID_KEY);
    } catch {
      return null;
    }
  });
  const [topHeight, setTopHeight] = useState(DEFAULT_TOP_PANEL_PERCENT);
  const [expiringIds, setExpiringIds] = useState<Set<string>>(new Set());
  const [ctxMenu, setCtxMenu] = useState<SessionContextMenu | null>(null);
  const [modalMode, setModalMode] = useState<InteractionMode | null>(null);
  // Filter state is owned by App so the catalog hook can refetch with
  // server-side predicates (covers historical-tail filtering). Fall back to
  // local state if a legacy caller mounts SessionsTab without the props —
  // tests and standalone storybook entries do this.
  const [localFilters, setLocalFilters] = useState<SessionsFilters>(
    defaultSessionsFilters,
  );
  const filters = filtersProp ?? localFilters;
  const setFilters = useCallback(
    (next: SessionsFilters) => {
      if (onFiltersChange) {
        onFiltersChange(next);
      } else {
        setLocalFilters(next);
      }
    },
    [onFiltersChange],
  );
  const [showFilterDropdown, setShowFilterDropdown] = useState(false);

  const activeFilterCount = countActiveFilters(filters);
  const [modalEntry, setModalEntry] = useState<WatchingSessionEntry | null>(null);
  const initialSelectionAppliedRef = useRef(false);
  const selectionClearedRef = useRef(false);

  const noopArtifactCtx = useMemo(
    () => ({
      openCodeAsArtifact: () => {},
      openFileAsArtifact: () => {},
    }),
    [],
  );

  useEffect(() => {
    const timeout = window.setTimeout(() => {
      setSearch(searchInput);
    }, 250);
    return () => window.clearTimeout(timeout);
  }, [searchInput]);

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
    const fetchNow = () => {
      void fetchAgents();
    };
    const timeout = window.setTimeout(fetchNow, 0);
    const interval = window.setInterval(() => {
      void fetchAgents();
    }, 5000);
    return () => {
      window.clearTimeout(timeout);
      window.clearInterval(interval);
    };
  }, [fetchAgents]);

  useEffect(() => {
    const timeout = window.setTimeout(() => {
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
    }, 0);
    return () => window.clearTimeout(timeout);
  }, [sessions]);

  // Status (Live | Expired) is part of SessionsFilters now; the SegmentedControl
  // writes filters.statuses and matchesSessionsFilters runs the predicate. The
  // agent-entries block below also gates on this — agents are by definition
  // live, so hide them when the user is looking at Expired only.
  const statusMode: "live" | "expired" =
    filters.statuses.size === 1 && filters.statuses.has("expired")
      ? "expired"
      : "live";

  const setStatusMode = useCallback(
    (mode: "live" | "expired") => {
      setFilters({
        ...filters,
        statuses:
          mode === "expired"
            ? new Set<SessionStatus>(["expired"])
            : new Set<SessionStatus>(DEFAULT_LIVE_STATUSES),
      });
    },
    [filters, setFilters],
  );

  const visibleSessions = useMemo(
    () => {
      const now = new Date();
      return sessions
        .filter((session) => session.id !== chatSessionId)
        .filter((session) => !expiringIds.has(session.id))
        .filter((session) => !HIDDEN_SOURCES.has(session.source))
        .filter((session) => matchesSearch(session, search))
        .filter((session) => matchesSessionsFilters(session, filters, now))
        .sort((a, b) => {
          const aSeq = a.seq_num ?? -Infinity;
          const bSeq = b.seq_num ?? -Infinity;
          if (aSeq !== bSeq) return bSeq - aSeq;
          return parseTimestamp(b.created_at) - parseTimestamp(a.created_at);
        });
    },
    [chatSessionId, expiringIds, search, sessions, filters],
  );

  const entries: WatchingSessionEntry[] = useMemo(() => {
    const agentEntries: WatchingSessionEntry[] =
      statusMode === "live"
        ? agents.reduce<WatchingSessionEntry[]>((nextEntries, agent) => {
            const matchedSession = agent.session_id
              ? visibleSessions.find((session) => session.id === agent.session_id)
              : undefined;
            if (!matchedSession) {
              return nextEntries;
            }
            const sessionIsLocal = resolveLocalFlag(
              matchedSession.is_local,
              matchedSession.source,
              matchedSession.model,
            );
            const agentIsLocal = resolveLocalFlag(
              agent.is_local,
              agent.provider,
              agent.model,
            );
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
              inputTokens: matchedSession.usage_input_tokens ?? 0,
              outputTokens: matchedSession.usage_output_tokens ?? 0,
              totalTokens:
                (matchedSession.usage_input_tokens ?? 0) +
                (matchedSession.usage_output_tokens ?? 0),
              hasTmux: Boolean(matchedSession.terminal_context),
              sandboxEnabled: matchedSession.sandbox_enabled ?? false,
              isLocal: sessionIsLocal || agentIsLocal,
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
        inputTokens: session.usage_input_tokens ?? 0,
        outputTokens: session.usage_output_tokens ?? 0,
        totalTokens: (session.usage_input_tokens ?? 0) + (session.usage_output_tokens ?? 0),
        hasTmux: Boolean(session.terminal_context),
        sandboxEnabled: session.sandbox_enabled ?? false,
        isLocal: resolveLocalFlag(session.is_local, session.source, session.model),
      }));

    return [...agentEntries, ...sessionEntries].sort(
      (a, b) => entryTimestamp(b) - entryTimestamp(a),
    );
  }, [agents, statusMode, visibleSessions]);

  const isLoading = isLoadingSessions || agentsLoading;

  useEffect(() => {
    const timeout = window.setTimeout(() => {
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
          setContentMode("transcript");
          onFocusHandled?.();
        }
        return;
      }

      if (hasFocusedEntry && focusSessionId === selectedSessionId) {
        selectionClearedRef.current = false;
        setContentMode("transcript");
        onFocusHandled?.();
        return;
      }

      if (hasFocusedEntry && focusSessionId !== selectedSessionId) {
        selectionClearedRef.current = false;
        setContentMode("transcript");
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
    }, 0);
    return () => window.clearTimeout(timeout);
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
    const timeout = window.setTimeout(() => {
      setContentMode("transcript");
    }, 0);
    return () => window.clearTimeout(timeout);
  }, [selectedSessionId]);

  const {
    session: selectedSessionDetail,
    sessionError,
    clearSessionError,
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

  const summaryMarkdown = resolveSessionSummaryMarkdown(
    selectedSessionDetail,
    selectedCatalogSession,
  );
  const selectedSessionStatus = selectedSession?.status ?? selectedEntry?.status ?? null;
  const transcriptUnavailable =
    transcriptStatus?.content_state === "missing" ||
    transcriptStatus?.content_state === "unparseable";
  const hideResumeAndSwap =
    selectedSessionStatus === "expired" && transcriptUnavailable;
  const showSummaryButton =
    selectedEntry != null && (Boolean(summaryMarkdown) || contentMode === "summary");
  const showResumeButton =
    selectedEntry?.type === "session" &&
    selectedSessionId != null &&
    selectedSessionStatus !== "active" &&
    !hideResumeAndSwap &&
    Boolean(onResumeSession);
  const showSwapButton = Boolean(selectedEntry && onSwapSession && !hideResumeAndSwap);

  const handleExpire = useCallback(
    async (entry: WatchingSessionEntry) => {
      const expireEntry = () => {
        setExpiringIds((prev) => {
          const next = new Set(prev);
          next.add(entry.id);
          return next;
        });
      };
      const restoreEntry = () => {
        setExpiringIds((prev) => {
          const next = new Set(prev);
          next.delete(entry.id);
          return next;
        });
      };

      if (entry.type === "agent" && entry.runId) {
        if (!onKillAgent) {
          return false;
        }
        expireEntry();
        try {
          const didCancel = await onKillAgent(entry.runId);
          if (didCancel === false) {
            restoreEntry();
            return false;
          }
          return true;
        } catch {
          restoreEntry();
          return false;
        }
      }

      if (!onExpireSession) {
        return false;
      }

      expireEntry();
      try {
        const didExpire = await onExpireSession(entry.id);
        if (didExpire === false) {
          restoreEntry();
          return false;
        }
        return true;
      } catch {
        restoreEntry();
        return false;
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

  const handleSwapSelectedSession = useCallback(() => {
    if (!selectedSessionId || !selectedEntry) return;
    setContentMode("transcript");
    onSwapSession?.({
      sessionId: selectedSessionId,
      sessionType: selectedEntry.sessionType ?? null,
      agentRunId: selectedEntry.agentRunId ?? null,
    });
  }, [onSwapSession, selectedEntry, selectedSessionId]);

  const hasActiveFilters = activeFilterCount > 0 || search.trim().length > 0;
  const emptyListMessage = hasActiveFilters
    ? "No sessions match these filters."
    : statusMode === "expired"
      ? "No expired sessions"
      : "No live sessions";

  return (
    <div className="flex flex-col h-full">
      <div className="activity-panel-toolbar">
        <ActivityPanelSearch
          value={searchInput}
          onChange={setSearchInput}
          placeholder="Search"
        />
        <SegmentedControl<"live" | "expired">
          value={statusMode}
          onChange={setStatusMode}
          options={STATUS_MODE_OPTIONS}
          ariaLabel="Session status filter"
          size="md"
          controlHeight="sm"
          className="activity-panel-toolbar-segmented"
        />
        <button
          type="button"
          className="btn btn-accent btn-sm activity-panel-action-btn activity-filter-button"
          onClick={() => setShowFilterDropdown((v) => !v)}
          title="Filter sessions"
          aria-label="Filter sessions"
          aria-expanded={showFilterDropdown}
        >
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
            <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3" />
          </svg>
          <span className="activity-panel-action-btn__label">Filter</span>
          {activeFilterCount > 0 && (
            <span className="activity-filter-badge">{activeFilterCount}</span>
          )}
        </button>
        {showFilterDropdown && (
          <SessionsFilterDropdown
            filters={filters}
            onChange={setFilters}
            providerOptions={SESSION_PROVIDERS}
            onClose={() => setShowFilterDropdown(false)}
          />
        )}
      </div>

      <div
        className={`overflow-y-auto ${selectedSessionId ? "border-b border-border" : "flex-1"}`}
        style={selectedSessionId ? { height: `${topHeight}%` } : undefined}
      >
        {isLoading && entries.length === 0 ? (
          <ActivityPanelEmpty body="Loading sessions…" />
        ) : fetchError && entries.length === 0 ? (
          <ActivityPanelEmpty body={fetchError} />
        ) : entries.length === 0 ? (
          <FilterEmptyState
            message={emptyListMessage}
            hasActiveFilters={hasActiveFilters}
            activeFilterCount={activeFilterCount}
            onClear={() => setFilters(defaultSessionsFilters())}
          />
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
                role="button"
                tabIndex={0}
                className={`session-entry${isSelected ? " session-entry--active" : ""}${isPaused ? " session-entry--paused" : ""}`}
                onClick={() => handleSelect(entry.id)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    handleSelect(entry.id);
                  }
                }}
              >
                <div className="flex items-center gap-2 min-w-0 flex-1">
                  <ActivityRowStatusDot
                    kind={
                      entry.status === "active"
                        ? "success"
                        : entry.status === "expired"
                          ? "stopped"
                          : entry.status === "paused"
                            ? "paused"
                            : "warning"
                    }
                    pulse={entry.status === "active"}
                    label={`Session ${entry.status}`}
                  />
                  <SourceIcon source={entry.provider} size={14} />
                  <span className="activity-row-title">
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
          <div className="activity-panel-status-bar activity-panel-status-bar--detail">
            <div className="min-w-0 flex-1">
              <span className="activity-panel-status-bar__title">
                <span className="activity-panel-status-bar__watching-prefix">
                  Watching{" "}
                </span>
                {selectedEntry
                  ? selectedEntry.seqNum
                    ? `#${selectedEntry.seqNum}: ${selectedEntry.label}`
                    : selectedEntry.label
                  : "session"}
              </span>
            </div>
            <div className="activity-panel-status-bar__actions">
              {showSummaryButton && (
                <button
                  type="button"
                  className="btn btn-accent btn-sm activity-panel-action-btn"
                  onClick={() =>
                    setContentMode((current) =>
                      current === "summary" ? "transcript" : "summary",
                    )
                  }
                  aria-label={contentMode === "summary" ? "Transcript" : "Summary"}
                  title={contentMode === "summary" ? "Transcript" : "Summary"}
                >
                  {contentMode === "summary" ? (
                    <>
                      <TranscriptIcon />
                      <span className="activity-panel-action-btn__label">
                        Transcript
                      </span>
                    </>
                  ) : (
                    <>
                      <ClipboardListIcon />
                      <span className="activity-panel-action-btn__label">
                        Summary
                      </span>
                    </>
                  )}
                </button>
              )}
              {showResumeButton && (
                <button
                  type="button"
                  className="btn btn-accent btn-sm activity-panel-action-btn"
                  onClick={() => {
                    if (selectedSessionId) {
                      void onResumeSession?.(selectedSessionId);
                    }
                  }}
                  aria-label="Resume"
                  title="Resume"
                >
                  <PlayIcon />
                  <span className="activity-panel-action-btn__label">Resume</span>
                </button>
              )}
              {showSwapButton && selectedEntry && (
                <button
                  type="button"
                  className="btn btn-accent btn-sm activity-panel-action-btn"
                  onClick={handleSwapSelectedSession}
                  aria-label="Swap"
                  title="Swap"
                >
                  <SwapIcon />
                  <span className="activity-panel-action-btn__label">Swap</span>
                </button>
              )}
            </div>
          </div>

          {sessionError && (
            <div
              role="alert"
              className="flex items-center gap-2 border-b border-border px-3 py-2 text-sm text-[var(--color-error)]"
            >
              <span className="min-w-0 flex-1">{sessionError}</span>
              <button
                type="button"
                className="flex h-6 w-6 shrink-0 items-center justify-center rounded text-[var(--color-error)] hover:bg-[color-mix(in_srgb,var(--color-error)_12%,transparent)]"
                onClick={clearSessionError}
                aria-label="Dismiss session error"
                title="Dismiss"
              >
                &times;
              </button>
            </div>
          )}

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
                  <ActivityPanelEmpty body="No summary available" />
                )}
              </div>
            ) : (
              <WatchingTranscript
                sessionId={selectedSessionId}
                messages={messages}
                isLoading={isLoadingDetail}
                emptyStateMessage={transcriptEmptyStateMessage}
              />
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
                    void handleExpire(entry);
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
          fromSessionId={chatSessionId ?? undefined}
        />
      )}
    </div>
  );
});
