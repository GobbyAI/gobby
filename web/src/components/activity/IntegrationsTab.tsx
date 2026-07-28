import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { Channel } from "../../hooks/useIntegrations";
import { useWebSocketEvent } from "../../hooks/useWebSocketEvent";
import { ResizeHandle } from "../shared/ResizeHandle";
import { Button } from "../ui/Button";
import { ActivityPanelEmpty, TasksEmptyIcon } from "./ActivityPanelEmpty";
import { ActivityToolbarSearchRow } from "./ActivityPanelSearch";
import { useRegisterActivityActions } from "./activityActions";
import { DEFAULT_TOP_PANEL_PERCENT } from "./constants";
import {
  removeIntegrationChannel,
  saveIntegrationDraft,
  toggleIntegrationChannel,
} from "./integrations/IntegrationsTabActions";
import {
  isCommunicationsUnavailable,
  loadIntegrationChannels,
} from "./integrations/IntegrationsTabData";
import { ChannelDetailPanel } from "./integrations/ChannelDetailPanel";
import { ChannelsList } from "./integrations/ChannelsList";
import { IntegrationsFilterPanel } from "./integrations/IntegrationsTabToolbar";
import { MessagesView } from "./integrations/MessagesView";
import {
  filterChannels,
  type IntegrationDraft,
  type IntegrationFilters,
} from "./integrations/IntegrationsTabModel";

const initialFilters: IntegrationFilters = {
  search: "",
  channelType: "all",
  status: "all",
};

type DetailViewMode = "config" | "messages";

export const IntegrationsTab = memo(function IntegrationsTab() {
  const [channels, setChannels] = useState<Channel[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [communicationsUnavailable, setCommunicationsUnavailable] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [isCreating, setIsCreating] = useState(false);
  const [viewMode, setViewMode] = useState<DetailViewMode>("config");
  const [topHeight, setTopHeight] = useState(DEFAULT_TOP_PANEL_PERCENT);
  const [filters, setFilters] = useState<IntegrationFilters>(initialFilters);
  const confirmLeaveRef = useRef<(next: () => void) => void>((next) => next());

  const refreshChannels = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    setCommunicationsUnavailable(false);
    try {
      const loaded = await loadIntegrationChannels();
      setChannels(loaded);
      return loaded;
    } catch (loadError) {
      if (isCommunicationsUnavailable(loadError)) {
        setCommunicationsUnavailable(true);
      } else {
        setError(loadError instanceof Error ? loadError.message : "Failed to load integrations");
      }
      setChannels([]);
      return [];
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshChannels();
  }, [refreshChannels]);

  useWebSocketEvent("comms_event", () => {
    void refreshChannels();
  });

  const filteredChannels = useMemo(
    () => filterChannels(channels, filters),
    [channels, filters],
  );
  const selectedChannel = useMemo(
    () => channels.find((channel) => channel.id === selectedId) ?? null,
    [channels, selectedId],
  );
  const hasDetail = isCreating || Boolean(selectedChannel);

  useEffect(() => {
    if (isCreating) return;
    if (filteredChannels.length === 0) {
      if (selectedId !== null) setSelectedId(null);
      return;
    }
    if (!selectedId || !filteredChannels.some((channel) => channel.id === selectedId)) {
      setSelectedId(filteredChannels[0].id);
    }
  }, [filteredChannels, isCreating, selectedId]);

  const replaceChannel = useCallback((updated: Channel) => {
    setChannels((current) =>
      current.some((channel) => channel.id === updated.id)
        ? current.map((channel) => (channel.id === updated.id ? updated : channel))
        : [updated, ...current],
    );
    setSelectedId(updated.id);
    setIsCreating(false);
    setViewMode("config");
  }, []);

  const withChannelBusy = useCallback(async (channel: Channel, action: () => Promise<void>) => {
    setBusyId(channel.id);
    setError(null);
    try {
      await action();
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : String(actionError));
    } finally {
      setBusyId(null);
    }
  }, []);

  const handleSelect = useCallback((channel: Channel) => {
    confirmLeaveRef.current(() => {
      setIsCreating(false);
      setViewMode("config");
      setSelectedId(channel.id);
    });
  }, []);

  const handleAdd = useCallback(() => {
    confirmLeaveRef.current(() => {
      setIsCreating(true);
      setViewMode("config");
      setSelectedId(null);
    });
  }, []);

  const handleSave = useCallback(
    async (draft: IntegrationDraft) => {
      try {
        const saved = await saveIntegrationDraft(draft);
        replaceChannel(saved);
        return true;
      } catch (saveError) {
        setError(saveError instanceof Error ? saveError.message : String(saveError));
        return false;
      }
    },
    [replaceChannel],
  );

  const handleToggle = useCallback(
    (channel: Channel) => {
      void withChannelBusy(channel, async () => {
        const updated = await toggleIntegrationChannel(channel);
        replaceChannel(updated);
      });
    },
    [replaceChannel, withChannelBusy],
  );

  const handleDelete = useCallback(
    (channel: Channel) => {
      if (!window.confirm(`Delete ${channel.name}?`)) return;
      void withChannelBusy(channel, async () => {
        await removeIntegrationChannel(channel);
        const loaded = await refreshChannels();
        if (selectedId === channel.id) {
          setSelectedId(loaded[0]?.id ?? null);
        }
      });
    },
    [refreshChannels, selectedId, withChannelBusy],
  );

  const [showFilters, setShowFilters] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const closeSearch = () => {
    setSearchOpen(false);
    setFilters({ ...filters, search: "" });
  };
  const activeFilterCount =
    (filters.channelType === "all" ? 0 : 1) + (filters.status === "all" ? 0 : 1);

  useRegisterActivityActions(
    {
      filter: {
        open: showFilters,
        onToggle: () => setShowFilters((value) => !value),
        ariaLabel: "Filter integrations",
        activeCount: activeFilterCount,
      },
      search: {
        open: searchOpen,
        onToggle: searchOpen ? closeSearch : () => setSearchOpen(true),
        ariaLabel: "Search integrations",
      },
      onAdd: handleAdd,
      addAriaLabel: "New channel",
    },
    [showFilters, activeFilterCount, searchOpen, filters, handleAdd],
  );

  return (
    <div className="relative flex h-full flex-col">
      {searchOpen && (
        <ActivityToolbarSearchRow
          value={filters.search}
          onChange={(search) => setFilters({ ...filters, search })}
          placeholder="Search integrations"
          ariaLabel="Search integrations"
          onClose={closeSearch}
        />
      )}
      {showFilters && (
        <IntegrationsFilterPanel filters={filters} onFiltersChange={setFilters} />
      )}

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
          {isLoading && channels.length === 0 ? (
            <ActivityPanelEmpty
              icon={<TasksEmptyIcon />}
              heading="Integrations"
              body="Loading communication channels..."
            />
          ) : communicationsUnavailable ? (
            <ActivityPanelEmpty
              icon={<TasksEmptyIcon />}
              heading="Communications not configured"
              body="Start the communications manager before managing notification channels."
              footer={
                <span className="text-xs text-muted-foreground">
                  Configuration must expose /api/comms/channels before channels can load.
                </span>
              }
            />
          ) : filteredChannels.length === 0 ? (
            <ActivityPanelEmpty
              icon={<TasksEmptyIcon />}
              heading="Integrations"
              body={
                channels.length === 0
                  ? "Communication channels appear here after they are configured."
                  : "No integrations match the current filters."
              }
              footer={
                channels.length === 0 ? (
                  <Button type="button" variant="accent" size="sm" onClick={handleAdd}>
                    Create channel
                  </Button>
                ) : undefined
              }
            />
          ) : (
            <ChannelsList
              channels={filteredChannels}
              selectedId={selectedId}
              busyId={busyId}
              onSelect={handleSelect}
              onToggle={handleToggle}
              onDelete={handleDelete}
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

        {hasDetail && (
          <div className="min-h-0 flex-1">
            {viewMode === "messages" && selectedChannel && !isCreating ? (
              <MessagesView
                key={selectedChannel.id}
                channel={selectedChannel}
                onClose={() => setViewMode("config")}
              />
            ) : (
              <ChannelDetailPanel
                key={isCreating ? "new" : selectedChannel?.id}
                mode={isCreating ? "create" : "edit"}
                channel={selectedChannel}
                onSave={handleSave}
                onCancelCreate={() => setIsCreating(false)}
                onShowMessages={() => setViewMode("messages")}
                onError={setError}
                onConfirmLeaveChange={(handler) => {
                  confirmLeaveRef.current = handler;
                }}
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
});
