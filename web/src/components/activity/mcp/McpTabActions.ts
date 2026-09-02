import type {
  McpServer,
  McpTemplate,
  McpTemplateParam,
} from "../../../hooks/useMcp";

export type McpServerScope = "project" | "global";

export interface McpServerDraft {
  name: string;
  description: string;
  transport: string;
  url: string;
  command: string;
  args: string[];
  env: Record<string, string>;
  headers: Record<string, string>;
  template: string;
  values: Record<string, string>;
  scope: McpServerScope;
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

const TEMPLATE_ERROR_KEY = "$template";
const SECRET_REFERENCE_PATTERN = /^\$secret:[A-Za-z_][A-Za-z0-9_.-]*$/;

function templateParamLabel(param: McpTemplateParam): string {
  return param.description?.trim() || param.name;
}

export function validateMcpTemplateSelection(
  template: McpTemplate | undefined,
  selection: Pick<McpServerDraft, "template" | "values">,
): Record<string, string> {
  if (!selection.template || !template) {
    return { [TEMPLATE_ERROR_KEY]: "Choose an MCP template." };
  }

  const errors: Record<string, string> = {};
  for (const param of template.params) {
    const value = selection.values[param.name]?.trim() ?? "";
    const label = templateParamLabel(param);
    if (param.required && !value) {
      errors[param.name] = `${label} is required.`;
      continue;
    }
    if (value && param.secret && !SECRET_REFERENCE_PATTERN.test(value)) {
      errors[param.name] = "Use a $secret:NAME reference.";
      continue;
    }
    if (value && param.choices.length > 0 && !param.choices.includes(value)) {
      errors[param.name] = `Choose a valid ${label.toLowerCase()}.`;
    }
  }
  return errors;
}

function recordOrEmpty(
  value: Record<string, string> | null | undefined,
): Record<string, string> {
  return value ? { ...value } : {};
}

export function createMcpServerDraft(
  overrides: Partial<McpServerDraft> = {},
): McpServerDraft {
  const scope =
    overrides.scope ?? (overrides.project_id ? "project" : "global");
  return {
    name: "",
    description: "",
    transport: "http",
    url: "",
    command: "",
    args: [],
    env: {},
    headers: {},
    template: "",
    values: {},
    scope,
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
  const data = (await response.json().catch(() => ({ success: false }))) as {
    success?: boolean;
  };
  return data.success !== false;
}

function serverUrl(name: string): string {
  return `/api/mcp/servers/${encodeURIComponent(name)}`;
}

export async function saveMcpServerDraft(
  options: SaveMcpServerDraftOptions,
): Promise<boolean> {
  if (options.mode === "create") {
    if (options.draft.template) {
      const { name, template, values, scope, project_id } = options.draft;
      return sendMcpServerRequest("/api/mcp/servers", "POST", {
        name,
        template,
        values,
        scope,
        project_id,
      });
    }
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
  return sendMcpServerRequest(serverUrl(options.originalName), "PATCH", {
    enabled: options.draft.enabled,
  });
}
