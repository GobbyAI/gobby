import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  useWiki,
  type WikiEnvelope,
  type WikiSourceRecord,
} from "../../hooks/useWiki";
import { ResizeHandle } from "../chat/artifacts/ResizeHandle";
import { WikiChatActions } from "../chat/WikiChatActions";
import { ActivityPanelEmpty, SessionsEmptyIcon } from "./ActivityPanelEmpty";
import { ActivityPanelSearch } from "./ActivityPanelSearch";
import { DEFAULT_TOP_PANEL_PERCENT } from "./constants";
import { WikiSourceRemovalDialog } from "./WikiSourceRemovalDialog";
import { FilterIcon, RefreshIcon } from "../icons/AppIcons";
import {
  WIKI_SOURCE_FILTERS,
  buildWikiSummary,
  filterWikiSources,
  type WikiSourceFilter,
} from "./wiki/WikiTabData";
import { WikiDetailPanel } from "./wiki/WikiDetailPanel";
import { WikiTabList } from "./wiki/WikiTabList";

interface WikiTabProps {
  projectId?: string | null;
}

export const WikiTab = memo(function WikiTab({ projectId }: WikiTabProps) {
  const { status, health, sources, isLoading, error, refresh, removeSource } = useWiki({ projectId });
  const [search, setSearch] = useState("");
  const [sourceFilter, setSourceFilter] = useState<WikiSourceFilter>("all");
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [topHeight, setTopHeight] = useState(DEFAULT_TOP_PANEL_PERCENT);
  const [removingSource, setRemovingSource] = useState<WikiSourceRecord | null>(null);
  const [preview, setPreview] = useState<WikiEnvelope | null>(null);
  const [removalError, setRemovalError] = useState<string | null>(null);
  const [isPreviewLoading, setIsPreviewLoading] = useState(false);
  const [isConfirming, setIsConfirming] = useState(false);
  const removalPreviewRequestRef = useRef(0);

  const summary = useMemo(
    () => buildWikiSummary({ status, health, isLoading, projectId }),
    [health, isLoading, projectId, status],
  );
  const filteredSources = useMemo(
    () => filterWikiSources(sources, search, sourceFilter),
    [search, sourceFilter, sources],
  );
  const selectedSource = useMemo(
    () => filteredSources.find((source) => source.id === selectedId) ?? null,
    [filteredSources, selectedId],
  );

  useEffect(() => {
    if (filteredSources.length === 0) {
      if (selectedId !== null) setSelectedId(null);
      return;
    }
    if (!selectedId || !filteredSources.some((source) => source.id === selectedId)) {
      setSelectedId(filteredSources[0].id);
    }
  }, [filteredSources, selectedId]);

  const openRemoval = useCallback(async (source: WikiSourceRecord) => {
    const requestId = removalPreviewRequestRef.current + 1;
    removalPreviewRequestRef.current = requestId;
    setBusyId(source.id);
    setRemovingSource(source);
    setPreview(null);
    setRemovalError(null);
    setIsPreviewLoading(true);
    try {
      const nextPreview = await removeSource({ id: source.id, dry_run: true });
      if (removalPreviewRequestRef.current === requestId) {
        setPreview(nextPreview);
      }
    } catch (nextError) {
      if (removalPreviewRequestRef.current === requestId) {
        setRemovalError(String(nextError));
      }
    } finally {
      if (removalPreviewRequestRef.current === requestId) {
        setIsPreviewLoading(false);
        setBusyId(null);
      }
    }
  }, [removeSource]);

  const closeRemoval = useCallback(() => {
    removalPreviewRequestRef.current += 1;
    setRemovingSource(null);
    setPreview(null);
    setRemovalError(null);
    setIsPreviewLoading(false);
  }, []);

  const confirmRemoval = useCallback(async ({ keep_asset }: { keep_asset: boolean }) => {
    if (!removingSource) return;
    setBusyId(removingSource.id);
    setIsConfirming(true);
    setRemovalError(null);
    try {
      await removeSource({ id: removingSource.id, yes: true, keep_asset });
      closeRemoval();
      await refresh();
    } catch (nextError) {
      setRemovalError(String(nextError));
    } finally {
      setIsConfirming(false);
      setBusyId(null);
    }
  }, [closeRemoval, refresh, removeSource, removingSource]);

  const selectedFilterLabel =
    WIKI_SOURCE_FILTERS.find((filter) => filter.value === sourceFilter)?.label ??
    "All sources";

  return (
    <div className="flex h-full flex-col">
      <div className="activity-panel-toolbar">
        <ActivityPanelSearch
          value={search}
          onChange={setSearch}
          placeholder="Search"
          ariaLabel="Search wiki sources"
        />
        <div className="relative">
          <button
            type="button"
            className="btn btn-accent btn-sm activity-panel-action-btn activity-filter-button"
            aria-label="Filter wiki sources"
            aria-expanded={filtersOpen}
            onClick={() => setFiltersOpen((open) => !open)}
          >
            <FilterIcon />
            <span className="activity-panel-action-btn__label">{selectedFilterLabel}</span>
          </button>
          {filtersOpen && (
            <div className="absolute right-0 top-10 z-20 w-56 rounded-md border border-border bg-[var(--bg-primary)] p-2 shadow-lg">
              {WIKI_SOURCE_FILTERS.map((filter) => (
                <label
                  key={filter.value}
                  className="flex min-h-9 items-center justify-between gap-2 rounded-md px-2 text-sm hover:bg-muted"
                >
                  <span>{filter.label}</span>
                  <input
                    type="radio"
                    name="wiki-source-filter"
                    aria-label={filter.label}
                    checked={sourceFilter === filter.value}
                    onChange={() => {
                      setSourceFilter(filter.value);
                      setFiltersOpen(false);
                    }}
                  />
                </label>
              ))}
            </div>
          )}
        </div>
        <button
          type="button"
          className="btn btn-accent btn-sm activity-panel-action-btn"
          aria-label="Refresh wiki"
          disabled={isLoading}
          onClick={() => void refresh()}
        >
          <RefreshIcon />
          <span className="activity-panel-action-btn__label">Refresh</span>
        </button>
        <div className="ml-auto">
          <WikiChatActions projectId={projectId} onActionComplete={() => void refresh()} />
        </div>
      </div>

      {error && (
        <div className="border-b border-border bg-error-soft px-3 py-2 text-sm text-error">
          {error}
        </div>
      )}

      <div className="flex min-h-0 flex-1 flex-col">
        <div
          className="overflow-y-auto border-b border-border"
          style={{ height: `${topHeight}%` }}
        >
          {isLoading && sources.length === 0 ? (
            <ActivityPanelEmpty
              icon={<SessionsEmptyIcon />}
              heading="Wiki"
              body="Loading wiki sources..."
            />
          ) : filteredSources.length === 0 ? (
            <ActivityPanelEmpty
              icon={<SessionsEmptyIcon />}
              heading="Wiki"
              body={
                sources.length === 0
                  ? "Sources attached to CodeWiki appear here."
                  : "No wiki sources match the current filters."
              }
            />
          ) : (
            <WikiTabList
              sources={filteredSources}
              selectedId={selectedId}
              busyId={busyId}
              onSelect={(source) => setSelectedId(source.id)}
              onRemove={(source) => void openRemoval(source)}
            />
          )}
        </div>

        <ResizeHandle
          direction="vertical"
          onResize={setTopHeight}
          panelHeight={topHeight}
          minHeight={25}
          maxHeight={75}
        />

        <div className="min-h-0 flex-1">
          <WikiDetailPanel source={selectedSource} summary={summary} />
        </div>
      </div>

      <WikiSourceRemovalDialog
        source={removingSource}
        preview={preview}
        isPreviewLoading={isPreviewLoading}
        isConfirming={isConfirming}
        error={removalError}
        onCancel={closeRemoval}
        onConfirm={(options) => void confirmRemoval(options)}
      />
    </div>
  );
});
