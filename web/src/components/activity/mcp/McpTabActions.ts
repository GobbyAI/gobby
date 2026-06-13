import type { McpServer } from "../../../hooks/useMcp";

export interface McpServerDraft {
  name: string;
  description: string;
  transport: string;
  url: string;
  command: string;
  args: string[];
  env: Record<string, string>;
  headers: Record<string, string>;
  project_id: string;
  enabled: boolean;
  requires_oauth: boolean;
  oauth_provider: string;
  connect_timeout: number;
}

interface CreateMcpServerSave {
  mode: "create";
  draft: McpServerDraft;
}

interface EditMcpServerSave {
  mode: "edit";
  originalName: string;
  originalEnabled: boolean;
  draft: McpServerDraft;
}

export type SaveMcpServerDraftOptions = CreateMcpServerSave | EditMcpServerSave;

function recordOrEmpty(value: Record<string, string> | null | undefined) {
  return value ? { ...value } : {};
}

export function createMcpServerDraft(
  overrides: Partial<McpServerDraft> = {},
): McpServerDraft {
  return {
    name: "",
    description: "",
    transport: "http",
    url: "",
    command: "",
    args: [],
    env: {},
    headers: {},
    project_id: "",
    enabled: true,
    requires_oauth: false,
    oauth_provider: "",
    connect_timeout: 30,
    ...overrides,
  };
}

export function mcpServerToDraft(server: McpServer): McpServerDraft {
  return createMcpServerDraft({
    name: server.name,
    description: server.description ?? "",
    transport: server.transport,
    url: server.url ?? "",
    command: server.command ?? "",
    args: server.args ?? [],
    env: recordOrEmpty(server.env),
    headers: recordOrEmpty(server.headers),
    project_id: server.project_id ?? "",
    enabled: server.enabled !== false,
    requires_oauth: server.requires_oauth ?? false,
    oauth_provider: server.oauth_provider ?? "",
    connect_timeout: server.connect_timeout ?? 30,
  });
}

async function sendMcpServerRequest(
  url: string,
  method: "POST" | "PUT" | "PATCH",
  body: unknown,
): Promise<boolean> {
  const response = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) return false;
  const data = (await response.json().catch(() => ({}))) as { success?: boolean };
  return data.success !== false;
}

function serverUrl(name: string): string {
  return `/api/mcp/servers/${encodeURIComponent(name)}`;
}

export async function saveMcpServerDraft(
  options: SaveMcpServerDraftOptions,
): Promise<boolean> {
  if (options.mode === "create") {
    return sendMcpServerRequest("/api/mcp/servers", "POST", options.draft);
  }

  const putDraft = {
    ...options.draft,
    enabled: options.originalEnabled,
  };
  const updated = await sendMcpServerRequest(
    serverUrl(options.originalName),
    "PUT",
    putDraft,
  );
  if (!updated) return false;

  if (options.draft.enabled === options.originalEnabled) return true;
  return sendMcpServerRequest(
    serverUrl(options.originalName),
    "PATCH",
    { enabled: options.draft.enabled },
  );
}
