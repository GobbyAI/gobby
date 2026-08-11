import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useMemory, type GobbyMemory } from "../../hooks/useMemory";
import { useIsMobile } from "../../hooks/useIsMobile";
import { configurationClient } from "../../api/config";
import { ResizeHandle } from "../shared/ResizeHandle";
import { Button } from "../ui/Button";
import { Input } from "../ui/Input";
import { coarseHitAreaCls } from "../ui/controlStyles";
import { ActivityPanelEmpty, SessionsEmptyIcon } from "./ActivityPanelEmpty";
import { ActivityToolbarSearchRow } from "./ActivityPanelSearch";
import { useRegisterActivityActions } from "./activityActions";
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
  extractDreamPurgeGraceDays,
  extractGraphLimits,
  filtersFromMemoryHook,
  memoryTypeCount,
  MEMORY_TYPE_OPTIONS,
  MEMORY_VISIBILITY_OPTIONS,
  type DreamPurgeGraceDays,
} from "./memory/MemoryTabData";
import { DEFAULT_GRAPH_LIMITS, type GraphLimits } from "./memory/KnowledgeGraphModel";
import { MemoryDetailPanel } from "./memory/MemoryDetailPanel";
import { MemoryGraphView } from "./memory/MemoryGraphView";
import { MemoryTabList } from "./memory/MemoryTabList";

interface MemoryTabProps {
  projectId?: string | null;
  requestPanelOverride?: () => void;
  releasePanelOverride?: () => void;
}

type MemoryViewMode = "detail" | "graph";
type MemoryScopeSegment = "all" | "project";

const MEMORY_SCOPE_OPTIONS = [
  { value: "all", label: "All" },
  { value: "project", label: "Project" },
] as const;

const noop = () => {};

function GraphIcon() {
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
      <circle cx="5" cy="6" r="2.5" />
      <circle cx="19" cy="6" r="2.5" />
      <circle cx="12" cy="18" r="2.5" />
      <path d="M7.3 7.7 10.7 15.9" />
      <path d="M16.7 7.7 13.3 15.9" />
      <path d="M7.5 6h9" />
    </svg>
  );
}

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
    fetchKnowledgeGraph,
    fetchEntityNeighbors,
  } = useMemory(projectId);
  const isMobile = useIsMobile();
  const [search, setSearch] = useState("");
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [requestedSelectedId, setSelectedId] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [topHeight, setTopHeight] = useState(DEFAULT_TOP_PANEL_PERCENT);
  const [viewMode, setViewMode] = useState<MemoryViewMode>("detail");
  const [scope, setScope] = useState<MemoryScopeSegment>("project");
  const [purgeGraceDays, setPurgeGraceDays] = useState<DreamPurgeGraceDays | null>(null);
  const [graphLimits, setGraphLimits] = useState<GraphLimits>(DEFAULT_GRAPH_LIMITS);
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
    () =>
      filterMemories(displayedMemories, tabFilters).filter(
        (memory) => scope === "all" || !memory.is_global,
      ),
    [displayedMemories, scope, tabFilters],
  );
  const selectedId =
    requestedSelectedId !== null &&
    filteredMemories.some((memory) => memory.id === requestedSelectedId)
      ? requestedSelectedId
      : (filteredMemories[0]?.id ?? null);
  const selectedMemory = useMemo(
    () => displayedMemories.find((memory) => memory.id === selectedId) ?? null,
    [displayedMemories, selectedId],
  );

  useEffect(() => {
    searchMemories(search);
  }, [search, searchMemories]);

  useEffect(() => {
    async function fetchPurgeConfig() {
      try {
        const snapshot = await configurationClient.fetchValues();
        if (!snapshot) {
          setError("Failed to load memory purge settings");
          return;
        }
        setPurgeGraceDays(extractDreamPurgeGraceDays(snapshot.desired));
        setGraphLimits(extractGraphLimits(snapshot.desired));
      } catch (configError) {
        console.warn("Failed to load memory dream purge config", configError);
        setError("Failed to load memory purge settings");
      }
    }
    void fetchPurgeConfig();
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

  const [searchOpen, setSearchOpen] = useState(false);
  const closeSearch = () => {
    setSearchOpen(false);
    setSearch("");
  };
  const activeFilterCount =
    (filters.memoryType ? 1 : 0) +
    (filters.recentOnly ? 1 : 0) +
    (filters.visibility !== "active" ? 1 : 0);

  // The graph view replaces the whole tab, so it registers no header toolbar.
  useRegisterActivityActions(
    viewMode === "graph"
      ? null
      : {
          selector: {
            value: scope,
            onChange: (value) => setScope(value as MemoryScopeSegment),
            options: MEMORY_SCOPE_OPTIONS,
            ariaLabel: "Memory scope",
          },
          filter: {
            open: filtersOpen,
            onToggle: () => setFiltersOpen((open) => !open),
            ariaLabel: "Filter memories",
            activeCount: activeFilterCount,
          },
          search: {
            open: searchOpen,
            onToggle: searchOpen ? closeSearch : () => setSearchOpen(true),
            ariaLabel: "Search memories",
          },
        },
    [viewMode, scope, filtersOpen, activeFilterCount, searchOpen],
  );

  // Persist graph limits to daemon config — the same `ui.*` paths the
  // Runtime & Infrastructure settings section edits (#19157). Optimistic:
  // the graph refetches immediately; a failed save just logs.
  const handleGraphLimitsChange = useCallback((next: GraphLimits) => {
    setGraphLimits(next);
    void configurationClient.patch({
      ui: {
        knowledge_graph_limit: next.entities,
        knowledge_graph_relationship_limit: next.relationships,
      },
    }).then((result) => {
      if (result.kind !== "success") {
        console.warn("Failed to persist graph limits", result.message);
      }
    }).catch((saveError: unknown) => {
      console.warn("Failed to persist graph limits", saveError);
    });
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
      if (memory.is_global) return;
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

  const detailActions = isMobile ? (
    <span className="text-xs text-muted-foreground">Graph opens on desktop only.</span>
  ) : (
    <DetailActionButton label="Show Graph" icon={<GraphIcon />} onClick={handleOpenGraph} />
  );

  if (viewMode === "graph") {
    return (
      <MemoryGraphView
        fetchKnowledgeGraph={fetchKnowledgeGraph}
        fetchEntityNeighbors={fetchEntityNeighbors}
        releasePanelOverride={releasePanelOverride}
        onClose={handleCloseGraph}
        limits={graphLimits}
        onLimitsChange={handleGraphLimitsChange}
      />
    );
  }

  return (
    <div className="relative flex h-full flex-col">
      {searchOpen && (
        <ActivityToolbarSearchRow
          value={search}
          onChange={setSearch}
          placeholder="Search"
          ariaLabel="Search memories"
          onClose={closeSearch}
        />
      )}
      {filtersOpen && (
        <div className="absolute right-2 top-1 z-20 w-56 rounded-md border border-border bg-[var(--bg-primary)] p-2 shadow-lg">
              <div className="mb-2 px-1 text-xs font-medium text-muted-foreground">Type</div>
              {MEMORY_TYPE_OPTIONS.map((option) => (
                <div
                  key={option.value}
                  className="flex min-h-9 items-center justify-between gap-2 rounded-md px-2 text-sm hover:bg-muted"
                >
                  <span>
                    {option.label}
                    <span className="ml-1 text-xs text-muted-foreground">
                      {memoryTypeCount(stats, option.value)}
                    </span>
                  </span>
                  <Input
                    type="checkbox"
                    wrapperClassName="w-auto"
                    className="size-4 rounded-sm p-0"
                    aria-label={option.label}
                    checked={filters.memoryType === option.value}
                    onChange={() =>
                      patchFilters({
                        memoryType: filters.memoryType === option.value ? null : option.value,
                        recentOnly: false,
                      })
                    }
                  />
                </div>
              ))}
              <div className="mt-1 flex min-h-9 items-center justify-between gap-2 rounded-md px-2 text-sm hover:bg-muted">
                <span>
                  Last 24 hours
                  <span className="ml-1 text-xs text-muted-foreground">
                    {stats?.recent_count ?? 0}
                  </span>
                </span>
                <Input
                  type="checkbox"
                  wrapperClassName="w-auto"
                  className="size-4 rounded-sm p-0"
                  aria-label="Last 24 hours"
                  checked={filters.recentOnly}
                  onChange={() =>
                    patchFilters({
                      recentOnly: !filters.recentOnly,
                      memoryType: null,
                    })
                  }
                />
              </div>
              <div className="mt-2 mb-1 px-1 text-xs font-medium text-muted-foreground">
                Visibility
              </div>
              <div role="radiogroup" aria-label="Visibility" className="flex flex-col">
                {MEMORY_VISIBILITY_OPTIONS.map((option) => (
                  <div
                    key={option.value}
                    className="flex min-h-9 items-center justify-between gap-2 rounded-md px-2 text-sm hover:bg-muted"
                  >
                    <span>{option.label}</span>
                    <Input
                      type="radio"
                      wrapperClassName="w-auto"
                      className="size-4 rounded-full p-0"
                      name="memory-visibility"
                      aria-label={option.label}
                      checked={filters.visibility === option.value}
                      onChange={() => patchFilters({ visibility: option.value })}
                    />
                  </div>
                ))}
              </div>
            </div>
          )}

      {error && (
        <Button
          type="button"
          variant="destructive"
          size="sm"
          className={`min-h-11 w-full justify-start rounded-none border-x-0 border-t-0 border-b-border bg-error-soft px-3 py-2 text-left text-sm text-error ${coarseHitAreaCls}`}
          onClick={() => setError(null)}
          aria-label={`Dismiss error: ${error}`}
        >
          {error}
        </Button>
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
              onPromote={(memory) => void handlePromoteToGlobal(memory)}
              promoting={busyId === selectedMemory.id}
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
