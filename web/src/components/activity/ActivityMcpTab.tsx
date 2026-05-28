import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
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
import { ToolArgumentForm } from "../command-browser/ToolArgumentForm";
import { McpAddServerModal } from "../mcp/McpServerForm";
import { Heading } from "../shared/Heading";
import { ActivityPanelEmpty } from "./ActivityPanelEmpty";
import { ActivityPanelSearch } from "./ActivityPanelSearch";

type McpServerType = "internal" | "external";
type ToolDetailMode = "schema" | "execute";

type McpSelection =
  | { kind: "server"; serverName: string }
  | { kind: "tool"; serverName: string; toolName: string; mode: ToolDetailMode };

interface McpContextMenu {
  x: number;
  y: number;
  kind: "server" | "tool";
  serverName: string;
  toolName?: string;
  isExternal?: boolean;
}

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

const FILTERS: McpServerType[] = ["internal", "external"];

function getServerType(server: McpServer): McpServerType {
  return server.transport === "internal" ? "internal" : "external";
}

function matchesTool(tool: McpTool, query: string): boolean {
  return (
    tool.name.toLowerCase().includes(query) ||
    (tool.brief ?? "").toLowerCase().includes(query)
  );
}

function formatJson(value: unknown): string {
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2) ?? "undefined";
}

function MoreIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="1" />
      <circle cx="19" cy="12" r="1" />
      <circle cx="5" cy="12" r="1" />
    </svg>
  );
}

function ChevronIcon({ open }: { open: boolean }) {
  return (
    <span
      className={cn(
        "activity-mcp-row-chevron",
        open && "activity-mcp-row-chevron--open",
      )}
      aria-hidden="true"
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
      >
        <polyline points="9 18 15 12 9 6" />
      </svg>
    </span>
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
  refreshToolCache,
  fetchToolSchema,
  callTool,
}: ActivityMcpTabProps) {
  const { confirm, ConfirmDialogElement } = useConfirmDialog();
  const [serverTypes, setServerTypes] = useState<Set<McpServerType>>(
    () => new Set(FILTERS),
  );
  const [showFilterDropdown, setShowFilterDropdown] = useState(false);
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
  const [executionResult, setExecutionResult] = useState<{
    success: boolean;
    data: string;
  } | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const schemaRequestIdRef = useRef(0);

  const normalizedSearch = searchText.trim().toLowerCase();
  const filteredServers = useMemo(
    () =>
      servers.filter((server) => {
        if (!serverTypes.has(getServerType(server))) return false;
        if (!normalizedSearch) return true;
        if (server.name.toLowerCase().includes(normalizedSearch)) return true;
        return (toolsByServer[server.name] ?? []).some((tool) =>
          matchesTool(tool, normalizedSearch),
        );
      }),
    [normalizedSearch, serverTypes, servers, toolsByServer],
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

  const activeFilterCount = FILTERS.length - serverTypes.size;
  const selectedServer =
    selection?.serverName
      ? servers.find((server) => server.name === selection.serverName) ?? null
      : null;
  const selectedTool =
    selection?.kind === "tool"
      ? (toolsByServer[selection.serverName] ?? []).find(
          (tool) => tool.name === selection.toolName,
        ) ?? null
      : null;

  const toggleFilter = useCallback((filter: McpServerType) => {
    setServerTypes((prev) => {
      const next = new Set(prev);
      if (next.has(filter)) next.delete(filter);
      else next.add(filter);
      return next;
    });
  }, []);

  const toggleServer = useCallback((name: string) => {
    setExpandedServers((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }, []);

  const loadToolSchema = useCallback(
    async (serverName: string, toolName: string, mode: ToolDetailMode) => {
      const requestId = schemaRequestIdRef.current + 1;
      schemaRequestIdRef.current = requestId;
      setSelection({ kind: "tool", serverName, toolName, mode });
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

  const handleRefreshTools = useCallback(async () => {
    setRefreshing(true);
    setActionError(null);
    try {
      const ok = await refreshToolCache();
      if (!ok) setActionError("Failed to refresh MCP tools");
    } finally {
      setRefreshing(false);
    }
  }, [refreshToolCache]);

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
        data: formatJson(
          response.success
            ? response.result
            : { error: response.error ?? "Tool execution failed" },
        ),
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
        <div className="activity-panel-toolbar__end">
          <button
            type="button"
            className="btn btn-accent btn-sm activity-panel-action-btn activity-filter-button"
            onClick={() => setShowFilterDropdown((value) => !value)}
            aria-label="Filter MCP server types"
            aria-expanded={showFilterDropdown}
            title="Filter MCP server types"
          >
            <FilterIcon />
            <span className="activity-panel-action-btn__label">Filter</span>
            {activeFilterCount > 0 && (
              <span className="activity-filter-badge">{activeFilterCount}</span>
            )}
          </button>
          <button
            type="button"
            className="btn btn-accent btn-sm activity-panel-action-btn"
            onClick={handleRefreshTools}
            disabled={refreshing}
            title="Refresh tools"
            aria-label="Refresh MCP tools"
          >
            <RefreshIcon />
            <span className="activity-panel-action-btn__label">
              {refreshing ? "Refreshing" : "Refresh"}
            </span>
          </button>
          <button
            type="button"
            className="btn btn-primary btn-sm activity-panel-action-btn"
            onClick={() => setShowAddServer(true)}
            title="Add server"
            aria-label="Add MCP server"
          >
            <PlusIcon />
            <span className="activity-panel-action-btn__label">Add</span>
          </button>
        </div>
        {showFilterDropdown && (
          <div className="activity-mcp-filter-menu" role="menu">
            {FILTERS.map((filter) => (
              <button
                key={filter}
                type="button"
                role="menuitemcheckbox"
                aria-checked={serverTypes.has(filter)}
                className={cn(
                  "activity-filter-dropdown__item",
                  serverTypes.has(filter) && "activity-filter-dropdown__item--active",
                )}
                onClick={() => toggleFilter(filter)}
              >
                <span className="activity-mcp-filter-check" aria-hidden="true">
                  {serverTypes.has(filter) ? "[x]" : "[ ]"}
                </span>
                {filter === "internal" ? "Internal" : "External"}
              </button>
            ))}
          </div>
        )}
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

      <div className="activity-mcp-tree" role="tree" aria-label="MCP servers and tools">
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
            const allTools = toolsByServer[server.name] ?? [];
            const expanded = normalizedSearch
              ? true
              : expandedServers.has(server.name);
            const serverType = getServerType(server);
            const health =
              status?.server_health?.[server.name]?.health ??
              (server.connected ? "healthy" : "unknown");

            return (
              <div className="activity-mcp-server-group" key={server.name}>
                <div
                  role="treeitem"
                  aria-level={1}
                  aria-expanded={expanded}
                  aria-selected={
                    selection?.kind === "server" &&
                    selection.serverName === server.name
                  }
                  className={cn(
                    "activity-mcp-row activity-mcp-server-row",
                    selection?.kind === "server" &&
                      selection.serverName === server.name &&
                      "activity-mcp-row--selected",
                  )}
                >
                  <button
                    type="button"
                    className="activity-mcp-row-main"
                    onClick={() => {
                      setSelection({ kind: "server", serverName: server.name });
                      toggleServer(server.name);
                    }}
                  >
                    <ChevronIcon open={expanded} />
                    <span
                      className={cn(
                        "activity-mcp-health-dot",
                        `activity-mcp-health-dot--${health}`,
                      )}
                      title={`Health: ${health}`}
                    />
                    <span className="activity-mcp-server-name">{server.name}</span>
                    <span className="activity-mcp-badge">
                      {serverType === "internal" ? "Internal" : "External"}
                    </span>
                    <span className="activity-mcp-state">{server.state}</span>
                    <span className="activity-mcp-count">
                      {allTools.length} tool{allTools.length === 1 ? "" : "s"}
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
                      })
                    }
                  >
                    <MoreIcon />
                  </button>
                </div>
                {expanded && (
                  <div role="group">
                    {visibleTools.length === 0 ? (
                      <div className="activity-mcp-empty-row">No tools available</div>
                    ) : (
                      visibleTools.map((tool) => (
                        <div
                          key={`${server.name}.${tool.name}`}
                          role="treeitem"
                          aria-level={2}
                          aria-selected={
                            selection?.kind === "tool" &&
                            selection.serverName === server.name &&
                            selection.toolName === tool.name
                          }
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
                            onClick={() =>
                              void loadToolSchema(server.name, tool.name, "schema")
                            }
                          >
                            <span className="activity-mcp-tool-name">{tool.name}</span>
                            {tool.brief && (
                              <span className="activity-mcp-tool-brief">
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
        onExecute={handleExecute}
        onSwitchToExecute={() => {
          if (selection?.kind === "tool") {
            setSelection({ ...selection, mode: "execute" });
          }
        }}
        status={status}
        toolsByServer={toolsByServer}
      />

      {menu && (
        <McpQuickMenu
          menu={menu}
          onClose={() => setMenu(null)}
          onViewSchema={() => {
            if (menu.kind === "tool" && menu.toolName) {
              setMenu(null);
              void loadToolSchema(menu.serverName, menu.toolName, "schema");
            }
          }}
          onExecuteTool={() => {
            if (menu.kind === "tool" && menu.toolName) {
              setMenu(null);
              void loadToolSchema(menu.serverName, menu.toolName, "execute");
            }
          }}
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

interface McpDetailPanelProps {
  selection: McpSelection | null;
  server: McpServer | null;
  tool: McpTool | null;
  schema: McpToolSchema | null;
  schemaLoading: boolean;
  argumentValues: Record<string, unknown>;
  onArgumentValuesChange: (values: Record<string, unknown>) => void;
  executing: boolean;
  executionResult: { success: boolean; data: string } | null;
  onExecute: () => void;
  onSwitchToExecute: () => void;
  status: McpStatus | null;
  toolsByServer: Record<string, McpTool[]>;
}

function McpDetailPanel({
  selection,
  server,
  tool,
  schema,
  schemaLoading,
  argumentValues,
  onArgumentValuesChange,
  executing,
  executionResult,
  onExecute,
  onSwitchToExecute,
  status,
  toolsByServer,
}: McpDetailPanelProps) {
  if (!selection) {
    return (
      <div className="activity-mcp-detail">
        <div className="activity-panel-status-bar activity-panel-status-bar--detail">
          <span className="activity-panel-status-bar__title">MCP</span>
        </div>
        <div className="activity-mcp-detail-body activity-mcp-detail-body--empty">
          Select a server or tool.
        </div>
      </div>
    );
  }

  if (selection.kind === "server") {
    const health = server ? status?.server_health?.[server.name] : null;
    return (
      <div className="activity-mcp-detail">
        <div className="activity-panel-status-bar activity-panel-status-bar--detail">
          <span className="activity-panel-status-bar__title">
            {server?.name ?? selection.serverName}
          </span>
        </div>
        <div className="activity-mcp-detail-body">
          {server ? (
            <dl className="activity-mcp-kv">
              <div>
                <dt>Type</dt>
                <dd>{getServerType(server) === "internal" ? "Internal" : "External"}</dd>
              </div>
              <div>
                <dt>Transport</dt>
                <dd>{server.transport}</dd>
              </div>
              <div>
                <dt>State</dt>
                <dd>{server.state}</dd>
              </div>
              <div>
                <dt>Health</dt>
                <dd>{health?.health ?? "unknown"}</dd>
              </div>
              <div>
                <dt>Tools</dt>
                <dd>{toolsByServer[server.name]?.length ?? 0}</dd>
              </div>
              {server.note && (
                <div>
                  <dt>Note</dt>
                  <dd>{server.note}</dd>
                </div>
              )}
            </dl>
          ) : (
            <p className="activity-mcp-detail-muted">Server not found.</p>
          )}
        </div>
      </div>
    );
  }

  const title = `${selection.serverName}.${selection.toolName}`;
  return (
    <div className="activity-mcp-detail">
      <div className="activity-panel-status-bar activity-panel-status-bar--detail">
        <span className="activity-panel-status-bar__title">{title}</span>
        {selection.mode === "schema" && (
          <button
            type="button"
            className="btn btn-accent btn-sm activity-panel-action-btn"
            onClick={onSwitchToExecute}
          >
            Execute tool...
          </button>
        )}
      </div>
      <div className="activity-mcp-detail-body">
        {schemaLoading ? (
          <p className="activity-mcp-detail-muted">Loading schema...</p>
        ) : schema ? (
          <>
            {(schema.description || tool?.brief) && (
              <p className="activity-mcp-description">
                {schema.description || tool?.brief}
              </p>
            )}
            <section className="activity-mcp-section">
              <Heading level={3} className="activity-mcp-section-title">
                Input Schema
              </Heading>
              <pre className="activity-mcp-json">
                <code>{JSON.stringify(schema.inputSchema, null, 2)}</code>
              </pre>
            </section>
            {selection.mode === "execute" && (
              <section className="activity-mcp-section">
                <Heading level={3} className="activity-mcp-section-title">
                  Execute
                </Heading>
                <ToolArgumentForm
                  schema={schema.inputSchema}
                  values={argumentValues}
                  onChange={onArgumentValuesChange}
                  disabled={executing}
                />
                <button
                  type="button"
                  className="btn btn-primary btn-sm activity-mcp-execute-btn"
                  onClick={onExecute}
                  disabled={executing}
                >
                  {executing ? "Executing..." : "Execute"}
                </button>
                {executionResult && (
                  <pre
                    className={cn(
                      "activity-mcp-json activity-mcp-result",
                      !executionResult.success && "activity-mcp-result--error",
                    )}
                    aria-label="Tool result JSON"
                  >
                    {executionResult.data}
                  </pre>
                )}
              </section>
            )}
          </>
        ) : (
          <p className="activity-mcp-detail-muted">Schema unavailable.</p>
        )}
      </div>
    </div>
  );
}

interface McpQuickMenuProps {
  menu: McpContextMenu;
  onClose: () => void;
  onViewSchema: () => void;
  onExecuteTool: () => void;
  onRemoveServer: () => void;
}

function McpQuickMenu({
  menu,
  onClose,
  onViewSchema,
  onExecuteTool,
  onRemoveServer,
}: McpQuickMenuProps) {
  const menuRef = useRef<HTMLDivElement>(null);
  const menuStyle: CSSProperties = {
    position: "fixed",
    left: menu.x,
    top: menu.y,
  };

  useEffect(() => {
    const first = menuRef.current?.querySelector<HTMLButtonElement>(
      '[role="menuitem"]:not(:disabled)',
    );
    first?.focus();
  }, [menu.kind, menu.serverName, menu.toolName]);

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
    }
  };

  return (
    <>
      <div className="session-ctx-backdrop" onClick={onClose} />
      <div
        ref={menuRef}
        className="session-ctx-menu"
        style={menuStyle}
        role="menu"
        aria-label={menu.kind === "tool" ? "MCP tool actions" : "MCP server actions"}
        tabIndex={-1}
        onKeyDown={handleKeyDown}
      >
        {menu.kind === "tool" ? (
          <>
            <button className="session-ctx-item" role="menuitem" onClick={onViewSchema}>
              View schema
            </button>
            <button className="session-ctx-item" role="menuitem" onClick={onExecuteTool}>
              Execute tool...
            </button>
          </>
        ) : menu.isExternal ? (
          <button
            className="session-ctx-item session-ctx-item--destructive"
            role="menuitem"
            onClick={onRemoveServer}
          >
            Remove server...
          </button>
        ) : (
          <button className="session-ctx-item" role="menuitem" disabled>
            No server actions
          </button>
        )}
      </div>
    </>
  );
}

function FilterIcon() {
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
      <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3" />
    </svg>
  );
}

function RefreshIcon() {
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
      <polyline points="23 4 23 10 17 10" />
      <polyline points="1 20 1 14 7 14" />
      <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10" />
      <path d="M20.49 15a9 9 0 0 1-14.85 3.36L1 14" />
    </svg>
  );
}

function PlusIcon() {
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
      <line x1="12" y1="5" x2="12" y2="19" />
      <line x1="5" y1="12" x2="19" y2="12" />
    </svg>
  );
}
