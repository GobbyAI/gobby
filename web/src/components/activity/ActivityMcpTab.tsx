import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent,
} from "react";

import type {
  McpServer,
  McpStatus,
  McpTool,
  McpToolSchema,
} from "../../hooks/useMcp";
import { useConfirmDialog } from "../../hooks/useConfirmDialog";
import {
  useTreeKeyboardNavigation,
  type TreeNavItem,
} from "../../hooks/useTreeKeyboardNavigation";
import { cn } from "../../lib/utils";
import { ResizeHandle } from "../shared/ResizeHandle";
import { Button } from "../ui/Button";
import { Chip } from "../ui/Chip";
import { coarseHitAreaCls } from "../ui/controlStyles";
import { ActivityPanelEmpty } from "./ActivityPanelEmpty";
import { ActivityToolbarSearchRow } from "./ActivityPanelSearch";
import { ActivityRowStatusDot } from "./ActivityRowStatusDot";
import { useRegisterActivityActions } from "./activityActions";
import { DEFAULT_TOP_PANEL_PERCENT } from "./constants";
import { McpDetailPanel, type McpExecutionResult } from "./mcp/McpDetailPanel";
import { McpQuickMenu } from "./mcp/McpQuickMenu";
import { McpServerFields, type McpServerDraft } from "./mcp/McpServerFields";
import {
  createMcpServerDraft,
  mcpServerToDraft,
  saveMcpServerDraft,
} from "./mcp/McpTabActions";
import { ChevronIcon } from "./mcp/mcpIcons";
import { KebabIcon } from "./QuickMenu";
import {
  getServerType,
  healthToStatusKind,
  type McpContextMenu,
  type McpSelection,
  type McpTypeFilter,
} from "./mcp/mcpShared";

export interface ActivityMcpTabProps {
  servers: McpServer[];
  toolsByServer: Record<string, McpTool[]>;
  status: McpStatus | null;
  isLoading: boolean;
  searchText: string;
  setSearchText: (value: string) => void;
  addServer: (params: {
    name: string;
    transport: string;
    url?: string;
    command?: string;
    args?: string[];
    enabled?: boolean;
  }) => Promise<boolean>;
  removeServer: (name: string) => Promise<boolean>;
  setServerEnabled: (name: string, enabled: boolean) => Promise<boolean>;
  fetchServers?: () => Promise<void>;
  refreshToolCache: () => Promise<boolean>;
  fetchToolSchema: (
    serverName: string,
    toolName: string,
  ) => Promise<McpToolSchema | null>;
  callTool: (
    serverName: string,
    toolName: string,
    args: Record<string, unknown>,
  ) => Promise<{ success: boolean; result?: unknown; error?: string }>;
}

const TYPE_FILTER_OPTIONS = [
  { value: "all" as const, label: "All" },
  { value: "internal" as const, label: "Internal" },
  { value: "external" as const, label: "External" },
];

function actionErrorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

function matchesTool(tool: McpTool, query: string): boolean {
  return (
    tool.name.toLowerCase().includes(query) ||
    (tool.brief ?? "").toLowerCase().includes(query)
  );
}

function serverMatchesQuery(
  server: McpServer,
  query: string,
  toolsByServer: Record<string, McpTool[]>,
): boolean {
  if (server.name.toLowerCase().includes(query)) return true;
  return (toolsByServer[server.name] ?? []).some((tool) =>
    matchesTool(tool, query),
  );
}

/**
 * A flattened ARIA-tree row (server, then its tools when expanded), in DOM
 * order. Extends TreeNavItem so the list can be passed straight to
 * useTreeKeyboardNavigation. Row IDs are stable, opaque, encoded keys —
 * resolve names via the rowById map, never by parsing the ID.
 */
interface McpTreeRow extends TreeNavItem {
  kind: "server" | "tool";
  serverName: string;
  toolName?: string;
}

function serverRowId(name: string): string {
  return `server:${encodeURIComponent(name)}`;
}

function toolRowId(serverName: string, toolName: string): string {
  // encodeURIComponent escapes ':' and NUL, so encoded server/tool names
  // can't collide with the "tool:" prefix or collision-safe NUL separator.
  return `tool:${encodeURIComponent(serverName)}\u0000${encodeURIComponent(toolName)}`;
}

export function ActivityMcpTab({
  servers,
  toolsByServer,
  status,
  isLoading,
  searchText,
  setSearchText,
  removeServer,
  setServerEnabled,
  fetchServers,
  refreshToolCache,
  fetchToolSchema,
  callTool,
}: ActivityMcpTabProps) {
  const { confirm, ConfirmDialogElement } = useConfirmDialog();
  const [typeFilter, setTypeFilter] = useState<McpTypeFilter>("all");
  const [expandedServers, setExpandedServers] = useState<Set<string>>(
    () => new Set(),
  );
  const [selection, setSelection] = useState<McpSelection | null>(null);
  const [menu, setMenu] = useState<McpContextMenu | null>(null);
  const [schemaLoading, setSchemaLoading] = useState(false);
  const [toolSchema, setToolSchema] = useState<McpToolSchema | null>(null);
  const [argumentValues, setArgumentValues] = useState<Record<string, unknown>>(
    {},
  );
  const [executing, setExecuting] = useState(false);
  const [executionResult, setExecutionResult] =
    useState<McpExecutionResult | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [topHeight, setTopHeight] = useState(DEFAULT_TOP_PANEL_PERCENT);
  const schemaRequestIdRef = useRef(0);
  const executionRequestIdRef = useRef(0);

  const updateSelection = useCallback((next: McpSelection | null) => {
    executionRequestIdRef.current += 1;
    setExecuting(false);
    setSelection(next);
  }, []);

  const normalizedSearch = searchText.trim().toLowerCase();
  const filteredServers = useMemo(
    () =>
      servers
        .filter((server) => {
          if (typeFilter !== "all" && getServerType(server) !== typeFilter)
            return false;
          if (!normalizedSearch) return true;
          return serverMatchesQuery(server, normalizedSearch, toolsByServer);
        })
        .sort((left, right) =>
          left.name.localeCompare(right.name, undefined, {
            sensitivity: "base",
          }),
        ),
    [normalizedSearch, typeFilter, servers, toolsByServer],
  );

  const getVisibleTools = useCallback(
    (server: McpServer) => {
      const tools = toolsByServer[server.name] ?? [];
      if (
        !normalizedSearch ||
        server.name.toLowerCase().includes(normalizedSearch)
      ) {
        return tools;
      }
      return tools.filter((tool) => matchesTool(tool, normalizedSearch));
    },
    [normalizedSearch, toolsByServer],
  );

  // Default-select the first visible server so the bottom pane is never empty
  // (#19152). Never overrides an in-progress create draft.
  useEffect(() => {
    if (selection?.kind === "server" && selection.viewMode === "create") return;
    const selectionVisible =
      selection != null &&
      filteredServers.some((server) => server.name === selection.serverName);
    const first = filteredServers[0];
    const next = selectionVisible
      ? selection
      : first
        ? ({
            kind: "server",
            serverName: first.name,
            viewMode: getServerType(first) === "external" ? "fields" : "detail",
          } as const)
        : null;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- guarded visible-selection synchronization
    if (next !== selection) updateSelection(next);
  }, [filteredServers, selection, updateSelection]);

  const selectedServer = selection?.serverName
    ? (servers.find((server) => server.name === selection.serverName) ?? null)
    : null;
  const selectedTool =
    selection?.kind === "tool"
      ? ((toolsByServer[selection.serverName] ?? []).find(
          (tool) => tool.name === selection.toolName,
        ) ?? null)
      : null;
  const serverDraftSource = useMemo(() => {
    if (selection?.kind !== "server") return null;
    if (selection.viewMode === "create") return createMcpServerDraft();
    if (!selectedServer || getServerType(selectedServer) !== "external")
      return null;
    return mcpServerToDraft(selectedServer);
  }, [selectedServer, selection]);

  const toggleServer = useCallback((name: string) => {
    setExpandedServers((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }, []);

  const loadToolSchema = useCallback(
    async (serverName: string, toolName: string) => {
      const requestId = schemaRequestIdRef.current + 1;
      schemaRequestIdRef.current = requestId;
      updateSelection({ kind: "tool", serverName, toolName });
      setToolSchema(null);
      setSchemaLoading(true);
      setArgumentValues({});
      setExecutionResult(null);
      setActionError(null);
      const schema = await fetchToolSchema(serverName, toolName);
      if (schemaRequestIdRef.current !== requestId) return;
      setToolSchema(schema);
      setSchemaLoading(false);
    },
    [fetchToolSchema, updateSelection],
  );

  // Search auto-opens matching servers (handled here, not in an effect), but
  // `expandedServers` stays the single source of truth so ArrowLeft/chevron can
  // still collapse a server mid-search (it re-opens on the next keystroke).
  const handleSearchChange = useCallback(
    (value: string) => {
      setSearchText(value);
      const query = value.trim().toLowerCase();
      if (!query) return;
      setExpandedServers((prev) => {
        const next = new Set(prev);
        for (const server of servers) {
          if (typeFilter !== "all" && getServerType(server) !== typeFilter)
            continue;
          if (serverMatchesQuery(server, query, toolsByServer))
            next.add(server.name);
        }
        return next;
      });
    },
    [setSearchText, servers, typeFilter, toolsByServer],
  );

  // Flattened tree rows in DOM order (server, then its tools when expanded).
  const treeRows = useMemo<McpTreeRow[]>(() => {
    const rows: McpTreeRow[] = [];
    for (const server of filteredServers) {
      const expanded = expandedServers.has(server.name);
      rows.push({
        id: serverRowId(server.name),
        kind: "server",
        serverName: server.name,
        depth: 0,
        isExpandable: true,
        isExpanded: expanded,
      });
      if (!expanded) continue;
      for (const tool of getVisibleTools(server)) {
        rows.push({
          id: toolRowId(server.name, tool.name),
          kind: "tool",
          serverName: server.name,
          toolName: tool.name,
          depth: 1,
          isExpandable: false,
          isExpanded: false,
        });
      }
    }
    return rows;
  }, [filteredServers, expandedServers, getVisibleTools]);

  const rowById = useMemo(
    () => new Map(treeRows.map((row) => [row.id, row])),
    [treeRows],
  );

  const selectedId = useMemo(() => {
    if (!selection) return null;
    if (selection.kind === "tool") {
      return toolRowId(selection.serverName, selection.toolName);
    }
    return selection.serverName ? serverRowId(selection.serverName) : null;
  }, [selection]);

  const selectRow = useCallback(
    (id: string) => {
      const row = rowById.get(id);
      if (!row) return;
      if (row.kind === "tool" && row.toolName) {
        void loadToolSchema(row.serverName, row.toolName);
        return;
      }
      const server = servers.find((entry) => entry.name === row.serverName);
      const viewMode =
        server && getServerType(server) === "external" ? "fields" : "detail";
      updateSelection({ kind: "server", serverName: row.serverName, viewMode });
    },
    [loadToolSchema, rowById, servers, updateSelection],
  );

  const toggleRow = useCallback(
    (id: string) => {
      const row = rowById.get(id);
      if (row?.kind === "server") toggleServer(row.serverName);
    },
    [rowById, toggleServer],
  );

  const { setRowRef, handleKeyDown, getTabIndex } = useTreeKeyboardNavigation({
    items: treeRows,
    selectedId,
    onSelect: selectRow,
    onToggle: toggleRow,
    selectionFollowsFocus: false,
  });

  const openMenu = useCallback(
    (
      event: MouseEvent<HTMLButtonElement>,
      nextMenu: Omit<McpContextMenu, "x" | "y">,
    ) => {
      event.stopPropagation();
      const rect = event.currentTarget.getBoundingClientRect();
      setMenu({
        ...nextMenu,
        x: rect.left,
        y: rect.top,
        width: rect.width,
        height: rect.height,
      });
    },
    [],
  );

  const handleRefreshTools = useCallback(async () => {
    setRefreshing(true);
    setActionError(null);
    try {
      const ok = await refreshToolCache();
      if (!ok) setActionError("Failed to refresh MCP tools");
    } catch (error) {
      setActionError(actionErrorMessage(error, "Failed to refresh MCP tools"));
    } finally {
      setRefreshing(false);
    }
  }, [refreshToolCache]);

  const handleOpenAddServer = useCallback(() => {
    updateSelection({ kind: "server", serverName: "", viewMode: "create" });
    setActionError(null);
  }, [updateSelection]);

  const handleSaveServerDraft = useCallback(
    async (draft: McpServerDraft) => {
      setActionError(null);
      try {
        const ok =
          selection?.kind === "server" && selection.viewMode === "create"
            ? await saveMcpServerDraft({ mode: "create", draft })
            : selectedServer
              ? await saveMcpServerDraft({
                  mode: "edit",
                  originalName: selectedServer.name,
                  originalEnabled: selectedServer.enabled !== false,
                  draft,
                })
              : false;

        if (!ok) {
          setActionError(`Failed to save ${draft.name || "MCP server"}`);
          return false;
        }

        await fetchServers?.();
        updateSelection({
          kind: "server",
          serverName: draft.name,
          viewMode: "fields",
        });
        return true;
      } catch (error) {
        setActionError(actionErrorMessage(error, "Failed to save MCP server"));
        return false;
      }
    },
    [fetchServers, selectedServer, selection, updateSelection],
  );

  const [searchOpen, setSearchOpen] = useState(false);
  const closeSearch = () => {
    setSearchOpen(false);
    setSearchText("");
  };

  // Register the shared header toolbar for the MCP view. Tool-cache refresh
  // stays available from the server context menu; the header has no Refresh
  // button (#19152).
  useRegisterActivityActions(
    {
      selector: {
        value: typeFilter,
        onChange: (value) => setTypeFilter(value as McpTypeFilter),
        options: TYPE_FILTER_OPTIONS,
        ariaLabel: "MCP server type",
      },
      search: {
        open: searchOpen,
        onToggle: searchOpen ? closeSearch : () => setSearchOpen(true),
        ariaLabel: "Search MCP",
      },
      onAdd: handleOpenAddServer,
      addAriaLabel: "New MCP server",
    },
    [typeFilter, searchOpen, handleOpenAddServer, setSearchText],
  );

  const handleRemoveServer = useCallback(async () => {
    if (!menu || menu.kind !== "server") return;
    const name = menu.serverName;
    setMenu(null);
    const confirmed = await confirm({
      title: `Remove "${name}"?`,
      description: "External MCP server configuration will be removed.",
      confirmLabel: "Remove",
      destructive: true,
    });
    if (!confirmed) return;
    const ok = await removeServer(name);
    if (!ok) setActionError(`Failed to remove ${name}`);
  }, [confirm, menu, removeServer]);

  const handleToggleEnabled = useCallback(async () => {
    if (!menu || menu.kind !== "server") return;
    const name = menu.serverName;
    const next = menu.enabled === false;
    setMenu(null);
    try {
      const ok = await setServerEnabled(name, next);
      if (!ok)
        setActionError(`Failed to ${next ? "enable" : "disable"} ${name}`);
    } catch (error) {
      setActionError(
        actionErrorMessage(
          error,
          `Failed to ${next ? "enable" : "disable"} ${name}`,
        ),
      );
    }
  }, [menu, setServerEnabled]);

  const handleExecute = useCallback(async () => {
    if (selection?.kind !== "tool") return;
    const requestId = executionRequestIdRef.current + 1;
    executionRequestIdRef.current = requestId;
    setExecuting(true);
    setExecutionResult(null);
    setActionError(null);
    try {
      const response = await callTool(
        selection.serverName,
        selection.toolName,
        argumentValues,
      );
      if (executionRequestIdRef.current !== requestId) return;
      setExecutionResult({
        success: response.success,
        value: response.success
          ? response.result
          : { error: response.error ?? "Tool execution failed" },
      });
    } finally {
      if (executionRequestIdRef.current === requestId) setExecuting(false);
    }
  }, [argumentValues, callTool, selection]);

  if (isLoading) {
    return <ActivityPanelEmpty heading="MCP" body="Loading MCP servers..." />;
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {ConfirmDialogElement}
      {searchOpen && (
        <ActivityToolbarSearchRow
          value={searchText}
          onChange={handleSearchChange}
          placeholder="Search MCP"
          ariaLabel="Search MCP"
          onClose={closeSearch}
        />
      )}

      {actionError && (
        <Button
          type="button"
          variant="destructive"
          size="sm"
          className={cn(
            "w-full cursor-pointer rounded-none border-0 border-b border-[var(--border)] bg-[color-mix(in_srgb,var(--color-error)_10%,transparent)] px-3 py-[0.4rem] text-left text-[length:var(--text-xs)] text-[var(--color-error)]",
            coarseHitAreaCls,
          )}
          onClick={() => setActionError(null)}
          aria-label={`Dismiss error: ${actionError}`}
        >
          {actionError}
        </Button>
      )}

      <div
        className={cn(
          "flex min-h-0 flex-[1_1_auto] flex-col",
          selection && "flex-[0_0_auto]",
        )}
        style={selection ? { height: `${topHeight}%` } : undefined}
      >
        <div
          className="min-h-0 flex-[1_1_auto] overflow-y-auto bg-[var(--bg-primary)]"
          role="tree"
          aria-label="MCP servers and tools"
          aria-live="polite"
          aria-busy={refreshing}
        >
          {filteredServers.length === 0 ? (
            <ActivityPanelEmpty
              heading="MCP"
              body={
                servers.length === 0
                  ? "MCP servers appear here after they are configured"
                  : "No MCP servers match the current filters"
              }
            />
          ) : (
            filteredServers.map((server) => {
              const visibleTools = getVisibleTools(server);
              const expanded = expandedServers.has(server.name);
              const serverType = getServerType(server);
              const health =
                status?.server_health?.[server.name]?.health ??
                (server.connected ? "healthy" : "unknown");
              const disabled = server.enabled === false;
              const serverId = serverRowId(server.name);
              const serverSelected =
                selection?.kind === "server" &&
                selection.serverName === server.name;

              return (
                <Fragment key={server.name}>
                  <div
                    ref={(node) => setRowRef(serverId, node)}
                    role="treeitem"
                    tabIndex={getTabIndex(serverId)}
                    aria-level={1}
                    aria-expanded={expanded}
                    aria-selected={serverSelected}
                    aria-label={`${server.name} server, ${
                      serverType === "internal" ? "Internal" : "External"
                    }`}
                    className={cn(
                      "flex min-h-[var(--activity-panel-row-height)] cursor-pointer items-center gap-[0.45rem] py-[0.35rem] pr-1 pl-3 text-[var(--text-primary)] transition-[background,box-shadow] duration-150 hover:bg-[var(--bg-tertiary)] [&:not(:first-child)]:border-t [&:not(:first-child)]:border-[var(--border)]",
                      disabled && "opacity-[0.55]",
                      serverSelected &&
                        "bg-[color-mix(in_srgb,var(--accent)_8%,transparent)]",
                    )}
                    onClick={() => selectRow(serverId)}
                    onKeyDown={(event) => handleKeyDown(serverId, event)}
                  >
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      className={cn(
                        "inline-flex size-5 shrink-0 cursor-pointer items-center justify-center rounded-[0.35rem] border-0 bg-transparent p-0 text-[var(--text-muted)] transition-colors duration-150 hover:bg-[var(--bg-primary)] hover:text-[var(--text-primary)]",
                        coarseHitAreaCls,
                      )}
                      aria-label={`${expanded ? "Collapse" : "Expand"} ${
                        server.name
                      } tools`}
                      title={expanded ? "Collapse tools" : "Expand tools"}
                      onClick={(event) => {
                        event.stopPropagation();
                        toggleServer(server.name);
                      }}
                    >
                      <ChevronIcon open={expanded} />
                    </Button>
                    <ActivityRowStatusDot
                      kind={healthToStatusKind(health)}
                      title={`Health: ${health}`}
                    />
                    <span className="activity-row-title">{server.name}</span>
                    <Chip tone={serverType === "internal" ? "accent" : "info"}>
                      {serverType === "internal" ? "Internal" : "External"}
                    </Chip>
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      className={cn("task-more-btn", coarseHitAreaCls)}
                      aria-label={`Open actions for ${server.name} server`}
                      onClick={(event) =>
                        openMenu(event, {
                          kind: "server",
                          serverName: server.name,
                          isExternal: serverType === "external",
                          enabled: server.enabled,
                        })
                      }
                    >
                      <KebabIcon />
                    </Button>
                  </div>
                  {expanded &&
                    (visibleTools.length === 0 ? (
                      <div
                        className="py-[0.45rem] pr-3 pl-[2.55rem] text-[length:var(--text-sm)] text-[var(--text-secondary)]"
                        role="treeitem"
                        aria-level={2}
                        aria-disabled="true"
                        tabIndex={-1}
                      >
                        No tools available
                      </div>
                    ) : (
                      visibleTools.map((tool) => {
                        const toolId = toolRowId(server.name, tool.name);
                        const toolSelected =
                          selection?.kind === "tool" &&
                          selection.serverName === server.name &&
                          selection.toolName === tool.name;
                        return (
                          <div
                            key={toolId}
                            ref={(node) => setRowRef(toolId, node)}
                            role="treeitem"
                            tabIndex={getTabIndex(toolId)}
                            aria-level={2}
                            aria-selected={toolSelected}
                            aria-label={
                              tool.brief
                                ? `${tool.name} tool, ${tool.brief}`
                                : `${tool.name} tool`
                            }
                            className={cn(
                              "flex min-h-[var(--activity-panel-row-height)] cursor-pointer items-center gap-[0.45rem] py-[0.35rem] pr-1 pl-[2.55rem] text-[var(--text-primary)] transition-[background,box-shadow] duration-150 hover:bg-[var(--bg-tertiary)]",
                              toolSelected &&
                                "bg-[color-mix(in_srgb,var(--accent)_8%,transparent)]",
                            )}
                            onClick={() => selectRow(toolId)}
                            onKeyDown={(event) => handleKeyDown(toolId, event)}
                          >
                            <span className="activity-row-title flex-[0_1_auto]">
                              {tool.name}
                            </span>
                            {tool.brief && (
                              <span
                                className="activity-row-meta min-w-0 flex-[1_1_auto] truncate"
                                title={tool.brief}
                              >
                                {tool.brief}
                              </span>
                            )}
                            <Button
                              type="button"
                              variant="ghost"
                              size="icon"
                              className={cn("task-more-btn", coarseHitAreaCls)}
                              aria-label={`Open actions for ${server.name}.${tool.name}`}
                              onClick={(event) =>
                                openMenu(event, {
                                  kind: "tool",
                                  serverName: server.name,
                                  toolName: tool.name,
                                })
                              }
                            >
                              <KebabIcon />
                            </Button>
                          </div>
                        );
                      })
                    ))}
                </Fragment>
              );
            })
          )}
        </div>
      </div>

      {selection && (
        <>
          <ResizeHandle
            direction="vertical"
            onResize={setTopHeight}
            panelHeight={topHeight}
            minHeight={15}
            maxHeight={80}
          />
          {selection.kind === "server" &&
          (selection.viewMode === "create" || serverDraftSource) ? (
            <McpServerFields
              mode={selection.viewMode === "create" ? "create" : "edit"}
              source={serverDraftSource}
              onSave={handleSaveServerDraft}
              onDiscard={() => {
                if (selection.viewMode === "create") updateSelection(null);
              }}
            />
          ) : (
            <McpDetailPanel
              selection={selection}
              server={selectedServer}
              tool={selectedTool}
              schema={toolSchema}
              schemaLoading={schemaLoading}
              argumentValues={argumentValues}
              onArgumentValuesChange={setArgumentValues}
              executing={executing}
              executionResult={executionResult}
              onCallTool={handleExecute}
              status={status}
              toolsByServer={toolsByServer}
            />
          )}
        </>
      )}

      {menu && (
        <McpQuickMenu
          menu={menu}
          onClose={() => setMenu(null)}
          onViewSchema={() => {
            if (menu.kind === "tool" && menu.toolName) {
              setMenu(null);
              void loadToolSchema(menu.serverName, menu.toolName);
            }
          }}
          onCallTool={() => {
            if (menu.kind === "tool" && menu.toolName) {
              setMenu(null);
              void loadToolSchema(menu.serverName, menu.toolName);
            }
          }}
          onViewServer={() => {
            const serverName = menu.serverName;
            const viewMode = menu.isExternal ? "fields" : "detail";
            setMenu(null);
            updateSelection({ kind: "server", serverName, viewMode });
          }}
          onRefreshServer={() => {
            setMenu(null);
            void handleRefreshTools();
          }}
          onToggleEnabled={handleToggleEnabled}
          onRemoveServer={handleRemoveServer}
        />
      )}
    </div>
  );
}
