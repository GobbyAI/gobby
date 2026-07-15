import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useMemory, type GobbyMemory } from "../../hooks/useMemory";
import { useIsMobile } from "../../hooks/useIsMobile";
import { ResizeHandle } from "../chat/artifacts/ResizeHandle";
import { ActivityPanelEmpty, SessionsEmptyIcon } from "./ActivityPanelEmpty";
import { ActivityPanelSearch } from "./ActivityPanelSearch";
import { DEFAULT_TOP_PANEL_PERCENT } from "./constants";
import { DetailActionButton } from "./fields";
import { FilterIcon, RefreshIcon } from "../icons/AppIcons";
import {
  copyMemoryContent,
  deleteMemoryWithRefresh,
  saveMemoryDraft,
  type MemoryDraft,
} from "./memory/MemoryTabActions";
import {
  filterMemories,
  extractDreamPurgeGraceDays,
  filtersFromMemoryHook,
  memoryTypeCount,
  MEMORY_TYPE_OPTIONS,
  MEMORY_VISIBILITY_OPTIONS,
  type DreamPurgeGraceDays,
} from "./memory/MemoryTabData";
import { MemoryDetailPanel } from "./memory/MemoryDetailPanel";
import { MemoryGraphView } from "./memory/MemoryGraphView";
import { MemoryTabList } from "./memory/MemoryTabList";

interface MemoryTabProps {
  projectId?: string | null;
  requestPanelOverride?: () => void;
  releasePanelOverride?: () => void;
}

type MemoryViewMode = "detail" | "graph";

const noop = () => {};

export const MemoryTab = memo(function MemoryTab({
  projectId,
  requestPanelOverride = noop,
  releasePanelOverride = noop,
}: MemoryTabProps) {
  const {
    memories,
    searchResults,
    stats,
    isLoading,
    filters,
    setFilters,
    updateMemory,
    deleteMemory,
    restoreMemory,
    promoteMemoryToGlobal,
    searchMemories,
    refreshMemories,
    fetchKnowledgeGraph,
    fetchEntityNeighbors,
  } = useMemory(projectId);
  const isMobile = useIsMobile();
  const [search, setSearch] = useState("");
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [topHeight, setTopHeight] = useState(DEFAULT_TOP_PANEL_PERCENT);
  const [viewMode, setViewMode] = useState<MemoryViewMode>("detail");
  const [purgeGraceDays, setPurgeGraceDays] = useState<DreamPurgeGraceDays | null>(null);
  const confirmLeaveRef = useRef<(next: () => void) => void>((next) => next());

  const tabFilters = useMemo(
    () =>
      filtersFromMemoryHook({
        ...filters,
        search: "",
      }),
    [filters],
  );
  const displayedMemories = useMemo(
    () => (search.trim() ? (searchResults ?? []) : memories),
    [memories, search, searchResults],
  );
  const filteredMemories = useMemo(
    () => filterMemories(displayedMemories, tabFilters),
    [displayedMemories, tabFilters],
  );
  const selectedMemory = useMemo(
    () => displayedMemories.find((memory) => memory.id === selectedId) ?? null,
    [displayedMemories, selectedId],
  );

  useEffect(() => {
    searchMemories(search);
  }, [search, searchMemories]);

  useEffect(() => {
    if (filteredMemories.length === 0) {
      if (selectedId !== null) setSelectedId(null);
      return;
    }
    if (!selectedId || !filteredMemories.some((memory) => memory.id === selectedId)) {
      setSelectedId(filteredMemories[0].id);
    }
  }, [filteredMemories, selectedId]);

  useEffect(() => {
    const controller = new AbortController();
    async function fetchPurgeConfig() {
      try {
        const response = await fetch("/api/config/values", { signal: controller.signal });
        if (!response.ok) {
          setError(`Failed to load memory purge settings (${response.status})`);
          return;
        }
        const payload = await response.json();
        setPurgeGraceDays(extractDreamPurgeGraceDays(payload.values));
      } catch (configError) {
        if (configError instanceof DOMException && configError.name === "AbortError") return;
        console.warn("Failed to load memory dream purge config", configError);
        setError("Failed to load memory purge settings");
      }
    }
    void fetchPurgeConfig();
    return () => controller.abort();
  }, []);

  const patchFilters = useCallback(
    (patch: Partial<typeof filters>) => {
      setFilters({
        ...filters,
        ...patch,
      });
      setFiltersOpen(false);
    },
    [filters, setFilters],
  );

  const handleSelect = useCallback((memory: GobbyMemory) => {
    confirmLeaveRef.current(() => setSelectedId(memory.id));
  }, []);

  const handleOpenGraph = useCallback(() => {
    confirmLeaveRef.current(() => {
      setViewMode("graph");
      requestPanelOverride();
    });
  }, [requestPanelOverride]);

  const handleCloseGraph = useCallback(() => {
    setViewMode("detail");
  }, []);

  const handleSave = useCallback(
    async (draft: MemoryDraft) => {
      try {
        return await saveMemoryDraft({ draft, updateMemory });
      } catch (saveError) {
        setError(saveError instanceof Error ? saveError.message : String(saveError));
        return false;
      }
    },
    [updateMemory],
  );

  const handleCopy = useCallback(async (memory: GobbyMemory) => {
    setBusyId(memory.id);
    setError(null);
    try {
      await copyMemoryContent(memory);
    } catch (copyError) {
      setError(copyError instanceof Error ? copyError.message : "Failed to copy memory");
    } finally {
      setBusyId(null);
    }
  }, []);

  const handleDelete = useCallback(
    async (memory: GobbyMemory) => {
      if (!window.confirm(`Delete memory ${memory.id.slice(0, 8)}?`)) return;
      setBusyId(memory.id);
      setError(null);
      try {
        const deleted = await deleteMemoryWithRefresh({ memory, deleteMemory });
        if (deleted && selectedId === memory.id) setSelectedId(null);
      } catch (deleteError) {
        setError(deleteError instanceof Error ? deleteError.message : "Failed to delete memory");
      } finally {
        setBusyId(null);
      }
    },
    [deleteMemory, selectedId],
  );

  const handleRestore = useCallback(
    async (memory: GobbyMemory) => {
      setBusyId(memory.id);
      setError(null);
      try {
        const restored = await restoreMemory(memory.id);
        if (!restored) {
          setError("Failed to restore memory");
        }
      } catch (restoreError) {
        setError(restoreError instanceof Error ? restoreError.message : "Failed to restore memory");
      } finally {
        setBusyId(null);
      }
    },
    [restoreMemory],
  );

  const handlePromoteToGlobal = useCallback(
    async (memory: GobbyMemory) => {
      if (memory.project_id === null) return;
      setBusyId(memory.id);
      setError(null);
      try {
        const promoted = await promoteMemoryToGlobal(memory.id);
        if (promoted) {
          setSelectedId(promoted.id);
        } else {
          setError("Failed to promote memory");
        }
      } catch (promoteError) {
        setError(promoteError instanceof Error ? promoteError.message : "Failed to promote memory");
      } finally {
        setBusyId(null);
      }
    },
    [promoteMemoryToGlobal],
  );

  const hasDetail = Boolean(selectedMemory);

  const detailActions = (
    <>
      {selectedMemory && selectedMemory.project_id !== null && (
        <DetailActionButton
          label={busyId === selectedMemory.id ? "Promoting..." : "Promote to global"}
          variant="secondary"
          disabled={busyId === selectedMemory.id}
          onClick={() => void handlePromoteToGlobal(selectedMemory)}
        />
      )}
      {isMobile ? (
        <span className="text-xs text-muted-foreground">Graph opens on desktop only.</span>
      ) : (
        <DetailActionButton label="Graph" onClick={handleOpenGraph} />
      )}
    </>
  );

  if (viewMode === "graph") {
    return (
      <MemoryGraphView
        fetchKnowledgeGraph={fetchKnowledgeGraph}
        fetchEntityNeighbors={fetchEntityNeighbors}
        releasePanelOverride={releasePanelOverride}
        onClose={handleCloseGraph}
      />
    );
  }

  return (
    <div className="flex h-full flex-col">
      <div className="activity-panel-toolbar">
        <ActivityPanelSearch
          value={search}
          onChange={setSearch}
          placeholder="Search"
          ariaLabel="Search memories"
        />
        <div className="relative">
          <button
            type="button"
            className="btn btn-accent btn-sm activity-panel-action-btn activity-filter-button"
            aria-label="Filter memories"
            aria-expanded={filtersOpen}
            onClick={() => setFiltersOpen((open) => !open)}
          >
            <FilterIcon />
            <span className="activity-panel-action-btn__label">Filters</span>
          </button>
          {filtersOpen && (
            <div className="absolute right-0 top-10 z-20 w-56 rounded-md border border-border bg-[var(--bg-primary)] p-2 shadow-lg">
              <div className="mb-2 px-1 text-xs font-medium text-muted-foreground">Type</div>
              {MEMORY_TYPE_OPTIONS.map((option) => (
                <label
                  key={option.value}
                  className="flex min-h-9 items-center justify-between gap-2 rounded-md px-2 text-sm hover:bg-muted"
                >
                  <span>
                    {option.label}
                    <span className="ml-1 text-xs text-muted-foreground">
                      {memoryTypeCount(stats, option.value)}
                    </span>
                  </span>
                  <input
                    type="checkbox"
                    aria-label={option.label}
                    checked={filters.memoryType === option.value}
                    onChange={() =>
                      patchFilters({
                        memoryType: filters.memoryType === option.value ? null : option.value,
                        recentOnly: false,
                      })
                    }
                  />
                </label>
              ))}
              <label className="mt-1 flex min-h-9 items-center justify-between gap-2 rounded-md px-2 text-sm hover:bg-muted">
                <span>
                  Last 24 hours
                  <span className="ml-1 text-xs text-muted-foreground">
                    {stats?.recent_count ?? 0}
                  </span>
                </span>
                <input
                  type="checkbox"
                  aria-label="Last 24 hours"
                  checked={filters.recentOnly}
                  onChange={() =>
                    patchFilters({
                      recentOnly: !filters.recentOnly,
                      memoryType: null,
                    })
                  }
                />
              </label>
              <div className="mt-2 mb-1 px-1 text-xs font-medium text-muted-foreground">
                Visibility
              </div>
              <div role="radiogroup" aria-label="Visibility" className="flex flex-col">
                {MEMORY_VISIBILITY_OPTIONS.map((option) => (
                  <label
                    key={option.value}
                    className="flex min-h-9 items-center justify-between gap-2 rounded-md px-2 text-sm hover:bg-muted"
                  >
                    <span>{option.label}</span>
                    <input
                      type="radio"
                      name="memory-visibility"
                      aria-label={option.label}
                      checked={filters.visibility === option.value}
                      onChange={() => patchFilters({ visibility: option.value })}
                    />
                  </label>
                ))}
              </div>
            </div>
          )}
        </div>
        <button
          type="button"
          className="btn btn-accent btn-sm activity-panel-action-btn"
          aria-label="Refresh memories"
          disabled={isLoading}
          onClick={refreshMemories}
        >
          <RefreshIcon />
          <span className="activity-panel-action-btn__label">Refresh</span>
        </button>
      </div>

      {error && (
        <button
          type="button"
          className="min-h-11 border-b border-border bg-error-soft px-3 py-2 text-left text-sm text-error"
          onClick={() => setError(null)}
          aria-label={`Dismiss error: ${error}`}
        >
          {error}
        </button>
      )}

      <div className="flex min-h-0 flex-1 flex-col">
        <div
          className={hasDetail ? "overflow-y-auto border-b border-border" : "flex-1 overflow-y-auto"}
          style={hasDetail ? { height: `${topHeight}%` } : undefined}
        >
          {isLoading && memories.length === 0 ? (
            <ActivityPanelEmpty
              icon={<SessionsEmptyIcon />}
              heading="Memory"
              body="Loading saved context..."
            />
          ) : filteredMemories.length === 0 ? (
            <ActivityPanelEmpty
              icon={<SessionsEmptyIcon />}
              heading="Memory"
              body={
                filters.visibility === "hidden"
                  ? "No hidden memories — dream hasn't flagged anything."
                  : search.trim()
                    ? "No memories match the current filters."
                    : memories.length === 0
                    ? "Memories captured during sessions appear here."
                    : "No memories match the current filters."
              }
            />
          ) : (
            <MemoryTabList
              memories={filteredMemories}
              selectedId={selectedId}
              busyId={busyId}
              onSelect={handleSelect}
              onCopy={(memory) => void handleCopy(memory)}
              onDelete={(memory) => void handleDelete(memory)}
              onRestore={(memory) => void handleRestore(memory)}
            />
          )}
        </div>

        {hasDetail && (
          <ResizeHandle
            direction="vertical"
            onResize={setTopHeight}
            panelHeight={topHeight}
            minHeight={25}
            maxHeight={75}
          />
        )}

        {selectedMemory && (
          <div className="min-h-0 flex-1">
            <MemoryDetailPanel
              key={selectedMemory.id}
              memory={selectedMemory}
              onSave={handleSave}
              onRestore={(memory) => handleRestore(memory)}
              purgeGraceDays={purgeGraceDays}
              actions={detailActions}
              onConfirmLeaveChange={(handler) => {
                confirmLeaveRef.current = handler;
              }}
            />
          </div>
        )}
      </div>
    </div>
  );
});
