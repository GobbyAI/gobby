import {
  useCallback,
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
import { cn } from "../../lib/utils";
import { McpAddServerModal } from "../mcp/McpServerForm";
import { SegmentedControl } from "../ui/SegmentedControl";
import { ResizeHandle } from "../chat/artifacts/ResizeHandle";
import { ActivityPanelEmpty } from "./ActivityPanelEmpty";
import { ActivityPanelSearch } from "./ActivityPanelSearch";
import { ActivityRowStatusDot } from "./ActivityRowStatusDot";
import { useRegisterActivityActions } from "./activityActions";
import { DEFAULT_TOP_PANEL_PERCENT } from "./constants";
import { McpDetailPanel, type McpExecutionResult } from "./mcp/McpDetailPanel";
import { McpQuickMenu } from "./mcp/McpQuickMenu";
import { ChevronIcon, MoreIcon } from "./mcp/mcpIcons";
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

export function ActivityMcpTab({
  servers,
  toolsByServer,
  status,
  isLoading,
  searchText,
  setSearchText,
  addServer,
  removeServer,
  setServerEnabled,
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
  const [showAddServer, setShowAddServer] = useState(false);
  const [schemaLoading, setSchemaLoading] = useState(false);
  const [toolSchema, setToolSchema] = useState<McpToolSchema | null>(null);
  const [argumentValues, setArgumentValues] = useState<Record<string, unknown>>({});
  const [executing, setExecuting] = useState(false);
  const [executionResult, setExecutionResult] = useState<McpExecutionResult | null>(
    null,
  );
  const [actionError, setActionError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [topHeight, setTopHeight] = useState(DEFAULT_TOP_PANEL_PERCENT);
  const schemaRequestIdRef = useRef(0);

  const normalizedSearch = searchText.trim().toLowerCase();
  const filteredServers = useMemo(
    () =>
      servers.filter((server) => {
        if (typeFilter !== "all" && getServerType(server) !== typeFilter) return false;
        if (!normalizedSearch) return true;
        if (server.name.toLowerCase().includes(normalizedSearch)) return true;
        return (toolsByServer[server.name] ?? []).some((tool) =>
          matchesTool(tool, normalizedSearch),
        );
      }),
    [normalizedSearch, typeFilter, servers, toolsByServer],
  );

  const getVisibleTools = useCallback(
    (server: McpServer) => {
      const tools = toolsByServer[server.name] ?? [];
      if (!normalizedSearch || server.name.toLowerCase().includes(normalizedSearch)) {
        return tools;
      }
      return tools.filter((tool) => matchesTool(tool, normalizedSearch));
    },
    [normalizedSearch, toolsByServer],
  );

  const selectedServer = selection?.serverName
    ? servers.find((server) => server.name === selection.serverName) ?? null
    : null;
  const selectedTool =
    selection?.kind === "tool"
      ? (toolsByServer[selection.serverName] ?? []).find(
          (tool) => tool.name === selection.toolName,
        ) ?? null
      : null;

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
      setSelection({ kind: "tool", serverName, toolName });
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
    [fetchToolSchema],
  );

  const openMenu = useCallback(
    (
      event: MouseEvent<HTMLButtonElement>,
      nextMenu: Omit<McpContextMenu, "x" | "y">,
    ) => {
      event.stopPropagation();
      const rect = event.currentTarget.getBoundingClientRect();
      const menuWidth = 190;
      setMenu({
        ...nextMenu,
        x: Math.max(0, Math.min(rect.right - menuWidth, window.innerWidth - menuWidth)),
        y: rect.bottom + 4,
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

  const handleOpenAddServer = useCallback(() => setShowAddServer(true), []);

  // Register the shared header Add/Refresh actions for the MCP view.
  useRegisterActivityActions(
    {
      onAdd: handleOpenAddServer,
      addLabel: "Add",
      addAriaLabel: "Add MCP server",
      onRefresh: handleRefreshTools,
      refreshing,
      refreshLabel: "Refresh",
      refreshAriaLabel: "Refresh MCP tools",
    },
    [handleOpenAddServer, handleRefreshTools, refreshing],
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
      if (!ok) setActionError(`Failed to ${next ? "enable" : "disable"} ${name}`);
    } catch (error) {
      setActionError(
        actionErrorMessage(error, `Failed to ${next ? "enable" : "disable"} ${name}`),
      );
    }
  }, [menu, setServerEnabled]);

  const handleExecute = useCallback(async () => {
    if (selection?.kind !== "tool") return;
    setExecuting(true);
    setExecutionResult(null);
    setActionError(null);
    try {
      const response = await callTool(
        selection.serverName,
        selection.toolName,
        argumentValues,
      );
      setExecutionResult({
        success: response.success,
        value: response.success
          ? response.result
          : { error: response.error ?? "Tool execution failed" },
      });
    } finally {
      setExecuting(false);
    }
  }, [argumentValues, callTool, selection]);

  if (isLoading) {
    return <ActivityPanelEmpty heading="MCP" body="Loading MCP servers..." />;
  }

  return (
    <div className="activity-mcp-tab">
      {ConfirmDialogElement}
      <div className="activity-panel-toolbar">
        <ActivityPanelSearch
          value={searchText}
          onChange={setSearchText}
          placeholder="Search MCP"
        />
        <SegmentedControl<McpTypeFilter>
          value={typeFilter}
          onChange={setTypeFilter}
          options={TYPE_FILTER_OPTIONS}
          ariaLabel="MCP server type"
          size="md"
          controlHeight="sm"
          className="activity-panel-toolbar-segmented"
        />
      </div>

      {actionError && (
        <button
          type="button"
          className="activity-mcp-error"
          onClick={() => setActionError(null)}
          aria-label={`Dismiss error: ${actionError}`}
        >
          {actionError}
        </button>
      )}

      <div
        className={cn(
          "activity-mcp-tree-shell",
          selection && "activity-mcp-tree-shell--split",
        )}
        style={selection ? { height: `${topHeight}%` } : undefined}
      >
        <div className="activity-mcp-tree" role="list" aria-label="MCP servers and tools">
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
              const expanded = normalizedSearch
                ? true
                : expandedServers.has(server.name);
              const serverType = getServerType(server);
              const health =
                status?.server_health?.[server.name]?.health ??
                (server.connected ? "healthy" : "unknown");
              const disabled = server.enabled === false;

              return (
                <div className="activity-mcp-server-group" key={server.name}>
                  <div
                    role="listitem"
                    className={cn(
                      "activity-mcp-row activity-mcp-server-row",
                      disabled && "activity-mcp-server-row--disabled",
                      selection?.kind === "server" &&
                        selection.serverName === server.name &&
                        "activity-mcp-row--selected",
                    )}
                  >
                    <button
                      type="button"
                      className="activity-mcp-row-main"
                      aria-label={`Toggle ${server.name} server tools`}
                      aria-expanded={expanded}
                      onClick={() => {
                        setSelection({ kind: "server", serverName: server.name });
                        toggleServer(server.name);
                      }}
                    >
                      <ChevronIcon open={expanded} />
                      <ActivityRowStatusDot
                        kind={healthToStatusKind(health)}
                        title={`Health: ${health}`}
                      />
                      <span className="activity-row-title">{server.name}</span>
                      <span
                        className={cn(
                          "activity-mcp-chip",
                          serverType === "internal"
                            ? "activity-mcp-chip--internal"
                            : "activity-mcp-chip--external",
                        )}
                      >
                        {serverType === "internal" ? "Internal" : "External"}
                      </span>
                    </button>
                    <button
                      type="button"
                      className="task-more-btn"
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
                      <MoreIcon />
                    </button>
                  </div>
                  {expanded && (
                    <div role="list" aria-label={`${server.name} tools`}>
                      {visibleTools.length === 0 ? (
                        <div className="activity-mcp-empty-row">No tools available</div>
                      ) : (
                        visibleTools.map((tool) => (
                          <div
                            key={`${server.name}.${tool.name}`}
                            role="listitem"
                            className={cn(
                              "activity-mcp-row activity-mcp-tool-row",
                              selection?.kind === "tool" &&
                                selection.serverName === server.name &&
                                selection.toolName === tool.name &&
                                "activity-mcp-row--selected",
                            )}
                          >
                            <button
                              type="button"
                              className="activity-mcp-row-main"
                              onClick={() => void loadToolSchema(server.name, tool.name)}
                            >
                              <span className="activity-row-title activity-mcp-tool-title">
                                {tool.name}
                              </span>
                              {tool.brief && (
                                <span
                                  className="activity-row-meta activity-mcp-tool-brief"
                                  title={tool.brief}
                                >
                                  {tool.brief}
                                </span>
                              )}
                            </button>
                            <button
                              type="button"
                              className="task-more-btn"
                              aria-label={`Open actions for ${server.name}.${tool.name}`}
                              onClick={(event) =>
                                openMenu(event, {
                                  kind: "tool",
                                  serverName: server.name,
                                  toolName: tool.name,
                                })
                              }
                            >
                              <MoreIcon />
                            </button>
                          </div>
                        ))
                      )}
                    </div>
                  )}
                </div>
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
            setMenu(null);
            setSelection({ kind: "server", serverName });
          }}
          onRefreshServer={() => {
            setMenu(null);
            void handleRefreshTools();
          }}
          onToggleEnabled={handleToggleEnabled}
          onRemoveServer={handleRemoveServer}
        />
      )}

      {showAddServer && (
        <McpAddServerModal
          onAdd={addServer}
          onClose={() => setShowAddServer(false)}
        />
      )}
    </div>
  );
}
