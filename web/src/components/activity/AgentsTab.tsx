import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useConfirmDialog } from "../../hooks/useConfirmDialog";
import { ResizeHandle } from "../shared/ResizeHandle";
import { useRegisterActivityActions } from "./activityActions";
import { ActivityPanelEmpty, TasksEmptyIcon } from "./ActivityPanelEmpty";
import { ActivityToolbarSearchRow } from "./ActivityPanelSearch";
import { DEFAULT_TOP_PANEL_PERCENT } from "./constants";
import {
  deleteAgentDefinition,
  duplicateAgentDefinition,
  saveAgentDraft,
  setAgentEnabled,
} from "./agents/AgentsTabActions";
import {
  AGENT_SOURCE_OPTIONS,
  type AgentDefInfo,
  type AgentDraft,
  type AgentSourceFilter,
  filterAgentDefinitions,
  getAgentKey,
  getAgentNames,
  loadAgentDefinitions,
  loadPipelineList,
  loadProviderCatalog,
  nextCopyName,
} from "./agents/AgentsTabData";
import { AgentsDetailPanel } from "./agents/AgentsDetailPanel";
import { AgentsTabList } from "./agents/AgentsTabList";

interface AgentsTabProps {
  projectId?: string | null;
}

export const AgentsTab = memo(function AgentsTab({ projectId }: AgentsTabProps) {
  const { confirm, ConfirmDialogElement } = useConfirmDialog();
  const [definitions, setDefinitions] = useState<AgentDefInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const [sourceFilter, setSourceFilter] = useState<AgentSourceFilter>("installed");
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [topHeight, setTopHeight] = useState(DEFAULT_TOP_PANEL_PERCENT);
  const [providerCatalog, setProviderCatalog] = useState<
    Awaited<ReturnType<typeof loadProviderCatalog>>
  >([]);
  const [pipelines, setPipelines] = useState<{ id: string; name: string }[]>([]);
  const confirmLeaveRef = useRef<(next: () => void) => void>((next) => next());

  const selectedAgent = useMemo(
    () => definitions.find((definition) => getAgentKey(definition) === selectedKey) ?? null,
    [definitions, selectedKey],
  );

  const filteredAgents = useMemo(
    () => filterAgentDefinitions(definitions, { sourceFilter, searchText: search }),
    [definitions, search, sourceFilter],
  );

  const refreshDefinitions = useCallback(
    async (selectName?: string) => {
      setLoading(true);
      setError(null);
      try {
        const [nextDefinitions, nextCatalog, nextPipelines] = await Promise.all([
          loadAgentDefinitions(projectId),
          loadProviderCatalog(),
          loadPipelineList(),
        ]);
        setDefinitions(nextDefinitions);
        setProviderCatalog(nextCatalog);
        setPipelines(nextPipelines);
        setSelectedKey((current) => {
          if (selectName) {
            const selected = nextDefinitions.find(
              (definition) => definition.definition.name === selectName,
            );
            if (selected) return getAgentKey(selected);
          }
          if (current && nextDefinitions.some((definition) => getAgentKey(definition) === current)) {
            return current;
          }
          return nextDefinitions[0] ? getAgentKey(nextDefinitions[0]) : null;
        });
      } catch (loadError) {
        setError(loadError instanceof Error ? loadError.message : "Failed to load agents");
      } finally {
        setLoading(false);
      }
    },
    [projectId],
  );

  useEffect(() => {
    void refreshDefinitions();
  }, [refreshDefinitions]);

  const handleSelect = (agent: AgentDefInfo) => {
    confirmLeaveRef.current(() => {
      setCreating(false);
      setSelectedKey(getAgentKey(agent));
    });
  };

  const handleCreate = () => {
    confirmLeaveRef.current(() => {
      setCreating(true);
      setSelectedKey(null);
    });
  };

  const closeSearch = () => {
    setSearchOpen(false);
    setSearch("");
  };

  useRegisterActivityActions(
    {
      selector: {
        value: sourceFilter,
        onChange: (value) => setSourceFilter(value as AgentSourceFilter),
        options: AGENT_SOURCE_OPTIONS,
        ariaLabel: "Agent source",
      },
      search: {
        open: searchOpen,
        onToggle: searchOpen ? closeSearch : () => setSearchOpen(true),
        ariaLabel: "Search agents",
      },
      onAdd: handleCreate,
      addLabel: "Agent",
      addAriaLabel: "New agent",
    },
    [sourceFilter, searchOpen],
  );

  const handleSave = useCallback(
    async (definitionId: string | null, draft: AgentDraft) => {
      const saved = await saveAgentDraft({ definitionId, draft, projectId });
      if (saved) {
        setCreating(false);
        await refreshDefinitions(draft.form.name);
      }
      return saved;
    },
    [projectId, refreshDefinitions],
  );

  const withBusy = async (agent: AgentDefInfo, action: () => Promise<void>) => {
    const key = getAgentKey(agent);
    setBusyKey(key);
    setError(null);
    try {
      await action();
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : String(actionError));
    } finally {
      setBusyKey(null);
    }
  };

  const handleToggleEnabled = (agent: AgentDefInfo) => {
    void withBusy(agent, async () => {
      await setAgentEnabled(agent, !agent.enabled);
      await refreshDefinitions(agent.definition.name);
    });
  };

  const handleDuplicate = (agent: AgentDefInfo) => {
    void withBusy(agent, async () => {
      const copyName = nextCopyName(agent.definition.name, getAgentNames(definitions));
      await duplicateAgentDefinition(agent, copyName, projectId);
      await refreshDefinitions(copyName);
    });
  };

  const handleDelete = (agent: AgentDefInfo) => {
    void withBusy(agent, async () => {
      const confirmed = await confirm({
        title: "Delete agent definition?",
        description: `Delete "${agent.definition.name}" from the database?`,
        confirmLabel: "Delete",
        destructive: true,
      });
      if (!confirmed) return;
      await deleteAgentDefinition(agent);
      await refreshDefinitions();
    });
  };

  return (
    <div className="flex h-full min-h-0 flex-col bg-[var(--bg-primary)]">
      {ConfirmDialogElement}
      {searchOpen && (
        <ActivityToolbarSearchRow
          value={search}
          onChange={setSearch}
          placeholder="Search agents..."
          ariaLabel="Search agents"
          onClose={closeSearch}
        />
      )}
      {error && (
        <div className="border-b border-border bg-[var(--color-error-soft)] px-3 py-2 text-sm text-error">
          {error}
        </div>
      )}
      <div className="flex min-h-0 flex-1 flex-col">
        <div
          className="min-h-0 overflow-y-auto"
          style={creating || selectedAgent ? { height: `${topHeight}%` } : undefined}
        >
          {loading ? (
            <ActivityPanelEmpty heading="Agents" body="Loading agent definitions..." />
          ) : filteredAgents.length === 0 ? (
            <ActivityPanelEmpty
              icon={<TasksEmptyIcon />}
              heading="No agents"
              body="No agent definitions match the current filters."
            />
          ) : (
            <AgentsTabList
              agents={filteredAgents}
              selectedKey={selectedKey}
              busyKey={busyKey}
              onSelect={handleSelect}
              onToggleEnabled={handleToggleEnabled}
              onDuplicate={handleDuplicate}
              onDelete={handleDelete}
            />
          )}
        </div>
        {(creating || selectedAgent) && (
          <>
            <ResizeHandle
              direction="vertical"
              panelHeight={topHeight}
              minHeight={25}
              maxHeight={75}
              onResize={setTopHeight}
            />
            <div className="min-h-0 flex-1">
              <AgentsDetailPanel
                agent={selectedAgent}
                creating={creating}
                providerCatalog={providerCatalog}
                pipelines={pipelines}
                projectId={projectId}
                onSave={handleSave}
                onError={setError}
                onConfirmLeaveChange={(handler) => {
                  confirmLeaveRef.current = handler;
                }}
              />
            </div>
          </>
        )}
      </div>
    </div>
  );
});
