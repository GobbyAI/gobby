import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useMemory, type GobbyMemory } from "../../hooks/useMemory";
import { useIsMobile } from "../../hooks/useIsMobile";
import { ResizeHandle } from "../chat/artifacts/ResizeHandle";
import { ActivityPanelEmpty, SessionsEmptyIcon } from "./ActivityPanelEmpty";
import { DEFAULT_TOP_PANEL_PERCENT } from "./constants";
import { DetailActionButton } from "./fields";
import {
  copyMemoryContent,
  deleteMemoryWithRefresh,
  saveMemoryDraft,
  type MemoryDraft,
} from "./memory/MemoryTabActions";
import {
  filterMemories,
  filtersFromMemoryHook,
  memoryTypeCount,
  MEMORY_TYPE_OPTIONS,
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
    stats,
    isLoading,
    filters,
    setFilters,
    updateMemory,
    deleteMemory,
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
  const confirmLeaveRef = useRef<(next: () => void) => void>((next) => next());

  const tabFilters = useMemo(
    () =>
      filtersFromMemoryHook({
        ...filters,
        search,
      }),
    [filters, search],
  );
  const filteredMemories = useMemo(
    () => filterMemories(memories, tabFilters),
    [memories, tabFilters],
  );
  const selectedMemory = useMemo(
    () => memories.find((memory) => memory.id === selectedId) ?? null,
    [memories, selectedId],
  );

  useEffect(() => {
    if (filteredMemories.length === 0) {
      if (selectedId !== null) setSelectedId(null);
      return;
    }
    if (!selectedId || !filteredMemories.some((memory) => memory.id === selectedId)) {
      setSelectedId(filteredMemories[0].id);
    }
  }, [filteredMemories, selectedId]);

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

  const hasDetail = Boolean(selectedMemory);

  const detailActions = isMobile ? (
    <span className="text-xs text-muted-foreground">Graph opens on desktop only.</span>
  ) : (
    <DetailActionButton label="Graph" onClick={handleOpenGraph} />
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
      <div className="flex min-h-11 flex-wrap items-center gap-2 border-b border-border px-3 py-2">
        <input
          type="search"
          name="search-memories"
          aria-label="Search memories"
          className="min-h-9 min-w-0 flex-1 rounded-md border border-border bg-[var(--bg-secondary)] px-3 text-sm text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent pointer-coarse:min-h-11"
          placeholder="Search"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <div className="relative">
          <button
            type="button"
            className="inline-flex min-h-9 items-center justify-center rounded-md border border-border px-3 text-xs font-medium text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent pointer-coarse:min-h-11"
            aria-label="Filter memories"
            aria-expanded={filtersOpen}
            onClick={() => setFiltersOpen((open) => !open)}
          >
            Filters
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
            </div>
          )}
        </div>
        <button
          type="button"
          className="inline-flex min-h-9 items-center justify-center rounded-md border border-border px-3 text-xs font-medium text-muted-foreground hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent pointer-coarse:min-h-11"
          aria-label="Refresh memories"
          disabled={isLoading}
          onClick={refreshMemories}
        >
          Refresh
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
                memories.length === 0
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
