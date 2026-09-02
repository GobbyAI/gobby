import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { useWebSocketEvent } from "./useWebSocketEvent";

export interface McpServer {
  name: string;
  state: string;
  connected: boolean;
  available: boolean;
  transport: string;
  enabled?: boolean;
  note?: string;
  description?: string | null;
  url?: string | null;
  command?: string | null;
  args?: string[] | null;
  env?: Record<string, string> | null;
  headers?: Record<string, string> | null;
  project_id?: string | null;
  requires_oauth?: boolean;
  oauth_provider?: string | null;
  connect_timeout?: number;
}

export interface McpTool {
  name: string;
  brief: string;
  call_count?: number;
  success_rate?: number | null;
  avg_latency_ms?: number | null;
}

export interface McpServerHealth {
  state: string;
  health: string;
  failures: number;
}

export interface McpStatus {
  total_servers: number;
  connected_servers: number;
  cached_tools: number;
  server_health: Record<string, McpServerHealth>;
}

export interface McpToolSchema {
  name: string;
  description?: string;
  inputSchema: Record<string, unknown> | null;
}

export interface McpTemplateParam {
  name: string;
  required: boolean;
  secret: boolean;
  env: string | null;
  arg_flag: string | null;
  choices: string[];
  description: string | null;
}

export interface McpTemplate {
  name: string;
  description: string;
  owner: string | null;
  scope: "global" | "project";
  params: McpTemplateParam[];
}

function getBaseUrl(): string {
  return "";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseMcpTemplateParam(value: unknown): McpTemplateParam | null {
  if (!isRecord(value) || typeof value.name !== "string") return null;
  return {
    name: value.name,
    required: value.required === true,
    secret: value.secret === true,
    env: typeof value.env === "string" ? value.env : null,
    arg_flag: typeof value.arg_flag === "string" ? value.arg_flag : null,
    choices: Array.isArray(value.choices)
      ? value.choices.filter(
          (choice): choice is string => typeof choice === "string",
        )
      : [],
    description:
      typeof value.description === "string" ? value.description : null,
  };
}

function parseMcpTemplates(value: unknown): McpTemplate[] {
  if (!isRecord(value) || !Array.isArray(value.templates)) return [];
  return value.templates.flatMap((item) => {
    if (
      !isRecord(item) ||
      typeof item.name !== "string" ||
      (item.scope !== "global" && item.scope !== "project")
    ) {
      return [];
    }
    const params = Array.isArray(item.params)
      ? item.params.flatMap((param) => {
          const parsed = parseMcpTemplateParam(param);
          return parsed ? [parsed] : [];
        })
      : [];
    return [
      {
        name: item.name,
        description:
          typeof item.description === "string" ? item.description : "",
        owner: typeof item.owner === "string" ? item.owner : null,
        scope: item.scope,
        params,
      },
    ];
  });
}

export function useMcp() {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [toolsByServer, setToolsByServer] = useState<Record<string, McpTool[]>>(
    {},
  );
  const [status, setStatus] = useState<McpStatus | null>(null);
  const [templates, setTemplates] = useState<McpTemplate[]>([]);
  const [templatesLoading, setTemplatesLoading] = useState(false);
  const [templatesError, setTemplatesError] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [searchText, setSearchText] = useState("");
  const mcpDebounceRef = useRef<number | null>(null);
  const templatesLoadedKeyRef = useRef<string | null>(null);
  const templatesRequestRef = useRef<{
    key: string;
    requestId: number;
    promise: Promise<void>;
  } | null>(null);
  const templatesRequestIdRef = useRef(0);

  const fetchServers = useCallback(async () => {
    try {
      const baseUrl = getBaseUrl();
      const response = await fetch(`${baseUrl}/api/mcp/servers`);
      if (response.ok) {
        const data = await response.json();
        setServers(data.servers || []);
      }
    } catch (e) {
      console.error("Failed to fetch MCP servers:", e);
    }
  }, []);

  const fetchTools = useCallback(async () => {
    try {
      const baseUrl = getBaseUrl();
      const response = await fetch(
        `${baseUrl}/api/mcp/tools?include_metrics=true`,
      );
      if (response.ok) {
        const data = await response.json();
        setToolsByServer(data.tools || {});
      }
    } catch (e) {
      console.error("Failed to fetch MCP tools:", e);
    }
  }, []);

  const fetchStatus = useCallback(async () => {
    try {
      const baseUrl = getBaseUrl();
      const response = await fetch(`${baseUrl}/api/mcp/status`);
      if (response.ok) {
        const data = await response.json();
        setStatus(data);
      }
    } catch (e) {
      console.error("Failed to fetch MCP status:", e);
    }
  }, []);

  const fetchTemplates = useCallback(async (projectId?: string) => {
    const key = projectId || "global";
    if (templatesLoadedKeyRef.current === key) return;
    if (templatesRequestRef.current?.key === key) {
      return templatesRequestRef.current.promise;
    }

    const requestId = templatesRequestIdRef.current + 1;
    templatesRequestIdRef.current = requestId;
    setTemplatesLoading(true);
    setTemplatesError(null);

    const request = (async () => {
      try {
        const params = new URLSearchParams({
          scope: projectId ? "project" : "global",
        });
        if (projectId) params.set("project_id", projectId);
        const response = await fetch(
          `${getBaseUrl()}/api/mcp/templates?${params.toString()}`,
        );
        if (!response.ok) {
          throw new Error(
            `Template request failed with status ${response.status}`,
          );
        }
        const nextTemplates = parseMcpTemplates(await response.json());
        if (templatesRequestIdRef.current !== requestId) return;
        setTemplates(nextTemplates);
        templatesLoadedKeyRef.current = key;
      } catch (error: unknown) {
        if (templatesRequestIdRef.current !== requestId) return;
        templatesLoadedKeyRef.current = null;
        setTemplatesError("MCP templates could not be loaded. Try again.");
        console.error("Failed to fetch MCP templates:", error);
      } finally {
        if (templatesRequestIdRef.current === requestId) {
          setTemplatesLoading(false);
          templatesRequestRef.current = null;
        }
      }
    })();

    templatesRequestRef.current = { key, requestId, promise: request };
    return request;
  }, []);

  const refreshAll = useCallback(async () => {
    setIsLoading(true);
    await Promise.all([fetchServers(), fetchTools(), fetchStatus()]);
    setIsLoading(false);
  }, [fetchServers, fetchTools, fetchStatus]);

  const addServer = useCallback(
    async (params: {
      name: string;
      transport: string;
      url?: string;
      command?: string;
      args?: string[];
      enabled?: boolean;
    }): Promise<boolean> => {
      try {
        const baseUrl = getBaseUrl();
        const response = await fetch(`${baseUrl}/api/mcp/servers`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(params),
        });
        if (response.ok) {
          const data = await response.json();
          if (data.success) {
            await fetchServers();
            return true;
          }
        }
      } catch (e) {
        console.error("Failed to add MCP server:", e);
      }
      return false;
    },
    [fetchServers],
  );

  const importServer = useCallback(
    async (params: {
      from_project?: string;
      github_url?: string;
      query?: string;
      servers?: string[];
    }): Promise<boolean> => {
      try {
        const baseUrl = getBaseUrl();
        const response = await fetch(`${baseUrl}/api/mcp/servers/import`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(params),
        });
        if (response.ok) {
          const data = await response.json();
          if (data.success) {
            await refreshAll();
            return true;
          }
        }
      } catch (e) {
        console.error("Failed to import MCP server:", e);
      }
      return false;
    },
    [refreshAll],
  );

  const removeServer = useCallback(
    async (name: string): Promise<boolean> => {
      try {
        const baseUrl = getBaseUrl();
        const response = await fetch(
          `${baseUrl}/api/mcp/servers/${encodeURIComponent(name)}`,
          {
            method: "DELETE",
          },
        );
        if (response.ok) {
          const data = await response.json();
          if (data.success) {
            await fetchServers();
            return true;
          }
        }
      } catch (e) {
        console.error("Failed to remove MCP server:", e);
      }
      return false;
    },
    [fetchServers],
  );

  const setServerEnabled = useCallback(
    async (name: string, enabled: boolean): Promise<boolean> => {
      try {
        const baseUrl = getBaseUrl();
        const response = await fetch(
          `${baseUrl}/api/mcp/servers/${encodeURIComponent(name)}`,
          {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled }),
          },
        );
        if (response.ok) {
          const data = await response.json();
          if (data.success) {
            await fetchServers();
            return true;
          }
        }
      } catch (e) {
        console.error("Failed to update MCP server:", e);
      }
      return false;
    },
    [fetchServers],
  );

  const refreshToolCache = useCallback(async (): Promise<boolean> => {
    try {
      const baseUrl = getBaseUrl();
      const response = await fetch(`${baseUrl}/api/mcp/refresh`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (response.ok) {
        const data = await response.json();
        if (data.success) {
          await fetchTools();
          return true;
        }
      }
    } catch (e) {
      console.error("Failed to refresh MCP tools:", e);
    }
    return false;
  }, [fetchTools]);

  const fetchToolSchema = useCallback(
    async (
      serverName: string,
      toolName: string,
    ): Promise<McpToolSchema | null> => {
      try {
        const baseUrl = getBaseUrl();
        const response = await fetch(`${baseUrl}/api/mcp/tools/schema`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            server_name: serverName,
            tool_name: toolName,
          }),
        });
        if (response.ok) {
          const data = await response.json();
          if (data.success) {
            return {
              name: data.name,
              description: data.description,
              inputSchema: data.inputSchema,
            };
          }
        }
      } catch (e) {
        console.error("Failed to fetch tool schema:", e);
      }
      return null;
    },
    [],
  );

  const callTool = useCallback(
    async (
      serverName: string,
      toolName: string,
      args: Record<string, unknown>,
    ): Promise<{ success: boolean; result?: unknown; error?: string }> => {
      try {
        const baseUrl = getBaseUrl();
        const response = await fetch(`${baseUrl}/api/mcp/tools/call`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            server_name: serverName,
            tool_name: toolName,
            arguments: args,
          }),
        });
        if (!response.ok) {
          const data = await response.json().catch(() => null);
          const detail =
            typeof data?.detail === "string" ? data.detail : data?.error;
          return {
            success: false,
            error:
              typeof detail === "string"
                ? detail
                : `Tool call failed with status ${response.status}`,
          };
        }
        const data = await response.json();
        return {
          success: data.success,
          result: data.result,
          error: data.error || data.result?.error,
        };
      } catch (e) {
        return { success: false, error: String(e) };
      }
    },
    [],
  );

  const totalToolCount = useMemo(() => {
    return Object.values(toolsByServer).reduce(
      (sum, tools) => sum + tools.length,
      0,
    );
  }, [toolsByServer]);

  // Auto-fetch on mount
  useEffect(() => {
    refreshAll();
  }, [refreshAll]);

  // Cleanup debounce timer on unmount
  useEffect(() => {
    return () => {
      if (mcpDebounceRef.current) window.clearTimeout(mcpDebounceRef.current);
    };
  }, []);

  // Real-time updates via WebSocket (debounced to avoid redundant fetches)
  useWebSocketEvent(
    "mcp_event",
    useCallback(() => {
      if (mcpDebounceRef.current) window.clearTimeout(mcpDebounceRef.current);
      mcpDebounceRef.current = window.setTimeout(() => {
        refreshAll();
      }, 500);
    }, [refreshAll]),
  );

  return {
    servers,
    toolsByServer,
    status,
    templates,
    templatesLoading,
    templatesError,
    isLoading,
    totalToolCount,
    fetchServers,
    fetchTools,
    fetchStatus,
    fetchTemplates,
    refreshAll,
    addServer,
    importServer,
    removeServer,
    setServerEnabled,
    refreshToolCache,
    fetchToolSchema,
    callTool,
    searchText,
    setSearchText,
  };
}
