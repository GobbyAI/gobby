import { memo, useState, useEffect, useCallback, useRef, useMemo } from "react";

import { useSessionDetail } from "../../hooks/useSessionDetail";
import type { GobbySession } from "../../types/sessions";
import type { SwappedSessionTarget } from "../../types/chat";
import {
  countActiveFilters,
  defaultSessionsFilters,
  type SessionsFilters,
} from "./sessionsFilters";
import { DEFAULT_TOP_PANEL_PERCENT } from "./constants";
import { ActivityPanelEmpty, SessionsEmptyIcon } from "./ActivityPanelEmpty";
import {
  type WatchingSessionEntry,
  type SessionContextMenu,
  WATCHING_SESSION_ID_KEY,
} from "./SessionsTab.helpers";
import {
  resolveSessionStatusMode,
  statusesForMode,
  useRunningAgents,
  useWatchingSessionEntries,
} from "./SessionsTab.entries";
import {
  SessionsEntryList,
  SessionsTabToolbar,
} from "./SessionsTabList";
import {
  SessionsTabDetailPane,
  SessionsTabResizeHandle,
  type WatchingContentMode,
} from "./SessionsTabDetail";
import {
  SessionsContextMenu,
  SessionsInteractionModalHost,
  type InteractionMode,
} from "./SessionsTabMenu";
import { useAcpSessionDiscovery } from "./useAcpSessionDiscovery";

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
  // ACP lifecycle dispatch — distinct from the chat-conversation `onDeleteSession`.
  // Both target an ACP-backed row by canonical session id; gated upstream by
  // the row's advertised `acp` capabilities.
  onAcpCloseSession?: (sessionId: string) => Promise<boolean | void> | boolean | void;
  onAcpDeleteSession?: (sessionId: string) => Promise<boolean | void> | boolean | void;
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
  hint = "Matching sessions will appear here",
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

export const SessionsTab = memo(function SessionsTab({
  sessions = [],
  isLoadingSessions = false,
  filters: filtersProp,
  onFiltersChange,
  onKillAgent,
  onExpireSession,
  onResumeSession,
  onAcpCloseSession,
  onAcpDeleteSession,
  chatSessionId,
  focusSessionId,
  onFocusHandled,
  onSwapSession,
}: SessionsTabProps) {
  const { agents, agentsLoading, fetchError } = useRunningAgents();
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
  const providerOptions = useMemo(
    () => Array.from(new Set(sessions.map((session) => session.source))),
    [sessions],
  );
  const [modalEntry, setModalEntry] = useState<WatchingSessionEntry | null>(null);
  const initialSelectionAppliedRef = useRef(false);
  const selectionClearedRef = useRef(false);

  useEffect(() => {
    const timeout = window.setTimeout(() => {
      setSearch(searchInput);
    }, 250);
    return () => window.clearTimeout(timeout);
  }, [searchInput]);

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
  const statusMode = resolveSessionStatusMode(filters);

  // §C5: reconcile agent-side ACP sessions when the panel opens and on each
  // Live | Expired change; discovered rows flow back through the canonical
  // session list + WebSocket feed (no separate discover button or data path).
  useAcpSessionDiscovery(statusMode);

  const setStatusMode = useCallback(
    (mode: typeof statusMode) => {
      setFilters({
        ...filters,
        statuses: statusesForMode(mode),
      });
    },
    [filters, setFilters],
  );

  const entries = useWatchingSessionEntries({
    agents,
    chatSessionId,
    expiringIds,
    filters,
    search,
    sessions,
    statusMode,
  });

  const isLoading = isLoadingSessions || agentsLoading;

  const persistWatchingSessionId = useCallback((id: string) => {
    try {
      localStorage.setItem(WATCHING_SESSION_ID_KEY, id);
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    const timeout = window.setTimeout(() => {
      if (isLoading) {
        return;
      }

      if (entries.length === 0) {
        return;
      }

      const hasFocusedEntry =
        focusSessionId != null && entries.some((entry) => entry.id === focusSessionId);

      if (!initialSelectionAppliedRef.current) {
        initialSelectionAppliedRef.current = true;
        selectionClearedRef.current = false;
        const selectedStillPresent =
          selectedSessionId != null &&
          entries.some((entry) => entry.id === selectedSessionId);
        const nextSelection = hasFocusedEntry
          ? focusSessionId
          : selectedStillPresent
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
    const timeout = window.setTimeout(() => {
      setContentMode("transcript");
    }, 0);
    return () => window.clearTimeout(timeout);
  }, [selectedSessionId]);

  const {
    session: selectedSessionDetail,
    sessionError,
    transcriptDownloadUrl,
    clearSessionError,
    messages,
    isLoading: isLoadingDetail,
    transcriptStatus,
    hasMore: hasMoreMessages,
    loadMore: loadMoreMessages,
    hasNewer: hasNewerMessages,
    loadNewer: loadNewerMessages,
    isLoadingOlder,
    isLoadingNewer,
    setTranscriptAtBottom,
    firstItemIndex,
    transcriptDegradedReason,
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
    persistWatchingSessionId(id);
    setSelectedSessionId(id);
  }, [persistWatchingSessionId]);

  const handleResumeSession = useCallback(
    (sessionId: string) => onResumeSession?.(sessionId),
    [onResumeSession],
  );

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
    Boolean(onResumeSession) &&
    // ACP rows only offer resume when the agent advertises the capability;
    // non-ACP rows are unchanged.
    (selectedEntry.acp ? selectedEntry.acp.capabilities.resume === true : true);
  const showSwapButton = Boolean(selectedEntry && onSwapSession && !hideResumeAndSwap);

  // Optimistically hide a row while a lifecycle action is in flight, restoring
  // it if the action reports failure (returns `false` or throws). Shared by
  // Expire, Close, and Delete so all three feel instant and recover identically.
  const runOptimisticRowAction = useCallback(
    async (
      entryId: string,
      action: () => Promise<boolean | void> | boolean | void,
    ): Promise<boolean> => {
      const restoreEntry = () => {
        setExpiringIds((prev) => {
          const next = new Set(prev);
          next.delete(entryId);
          return next;
        });
      };
      setExpiringIds((prev) => {
        const next = new Set(prev);
        next.add(entryId);
        return next;
      });
      try {
        const outcome = await action();
        if (outcome === false) {
          restoreEntry();
          return false;
        }
        return true;
      } catch {
        restoreEntry();
        return false;
      }
    },
    [],
  );

  const handleExpire = useCallback(
    async (entry: WatchingSessionEntry) => {
      const runId = entry.runId;
      if (entry.type === "agent" && runId) {
        if (!onKillAgent) {
          return false;
        }
        return runOptimisticRowAction(entry.id, () => onKillAgent(runId));
      }
      if (!onExpireSession) {
        return false;
      }
      return runOptimisticRowAction(entry.id, () => onExpireSession(entry.id));
    },
    [onExpireSession, onKillAgent, runOptimisticRowAction],
  );

  const handleClose = useCallback(
    async (entry: WatchingSessionEntry) => {
      if (!onAcpCloseSession) {
        return false;
      }
      return runOptimisticRowAction(entry.id, () => onAcpCloseSession(entry.id));
    },
    [onAcpCloseSession, runOptimisticRowAction],
  );

  const handleDelete = useCallback(
    async (entry: WatchingSessionEntry) => {
      if (!onAcpDeleteSession) {
        return false;
      }
      return runOptimisticRowAction(entry.id, () => onAcpDeleteSession(entry.id));
    },
    [onAcpDeleteSession, runOptimisticRowAction],
  );

  const handleMenuButtonClick = useCallback(
    (event: React.MouseEvent<HTMLButtonElement>, entry: WatchingSessionEntry) => {
      event.stopPropagation();
      const rect = event.currentTarget.getBoundingClientRect();
      setCtxMenu({
        x: rect.left,
        y: rect.top,
        width: rect.width,
        height: rect.height,
        entry,
        trigger: event.currentTarget,
      });
    },
    [],
  );

  const closeCtxMenu = useCallback(() => setCtxMenu(null), []);

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
  }, [
    onSwapSession,
    selectedEntry,
    selectedSessionId,
  ]);

  const hasActiveFilters = activeFilterCount > 0 || search.trim().length > 0;
  const emptyListMessage = hasActiveFilters
    ? "No sessions match these filters"
    : statusMode === "expired"
      ? "No expired sessions"
      : "No live sessions";

  return (
    <div className="flex flex-col h-full">
      <SessionsTabToolbar
        activeFilterCount={activeFilterCount}
        filters={filters}
        onFiltersChange={setFilters}
        onSearchChange={setSearchInput}
        onStatusModeChange={setStatusMode}
        providerOptions={providerOptions}
        searchInput={searchInput}
        showFilterDropdown={showFilterDropdown}
        statusMode={statusMode}
        toggleFilterDropdown={() => setShowFilterDropdown((v) => !v)}
        closeFilterDropdown={() => setShowFilterDropdown(false)}
      />

      <SessionsEntryList
        emptyState={
          <FilterEmptyState
            message={emptyListMessage}
            hasActiveFilters={hasActiveFilters}
            activeFilterCount={activeFilterCount}
            onClear={() => setFilters(defaultSessionsFilters())}
          />
        }
        entries={entries}
        fetchError={fetchError}
        isLoading={isLoading}
        onMenuButtonClick={handleMenuButtonClick}
        onSelect={handleSelect}
        selectedSessionId={selectedSessionId}
        topHeight={topHeight}
      />

      <SessionsTabResizeHandle
        selectedSessionId={selectedSessionId}
        entriesLength={entries.length}
        topHeight={topHeight}
        onResize={setTopHeight}
      />

      {selectedSessionId && (
        <SessionsTabDetailPane
          clearSessionError={clearSessionError}
          contentMode={contentMode}
          firstItemIndex={firstItemIndex}
          hasMoreMessages={hasMoreMessages}
          hasNewerMessages={hasNewerMessages}
          isLoadingDetail={isLoadingDetail}
          isLoadingNewer={isLoadingNewer}
          isLoadingOlder={isLoadingOlder}
          loadMoreMessages={loadMoreMessages}
          loadNewerMessages={loadNewerMessages}
          messages={messages}
          onResumeSession={onResumeSession ? handleResumeSession : undefined}
          onSwapSelectedSession={handleSwapSelectedSession}
          selectedEntry={selectedEntry}
          selectedSessionId={selectedSessionId}
          sessionError={sessionError}
          transcriptDownloadUrl={transcriptDownloadUrl}
          setContentMode={setContentMode}
          setTranscriptAtBottom={setTranscriptAtBottom}
          showResumeButton={showResumeButton}
          showSummaryButton={showSummaryButton}
          showSwapButton={showSwapButton}
          summaryMarkdown={summaryMarkdown}
          transcriptDegradedReason={transcriptDegradedReason}
          transcriptEmptyStateMessage={transcriptEmptyStateMessage}
        />
      )}

      <SessionsContextMenu
        chatSessionId={chatSessionId}
        closeCtxMenu={closeCtxMenu}
        ctxMenu={ctxMenu}
        handleClose={handleClose}
        handleDelete={handleDelete}
        handleExpire={handleExpire}
        onResumeSession={onResumeSession ? handleResumeSession : undefined}
        openModal={openModal}
      />

      <SessionsInteractionModalHost
        chatSessionId={chatSessionId}
        closeModal={closeModal}
        modalEntry={modalEntry}
        modalMode={modalMode}
      />
    </div>
  );
});
