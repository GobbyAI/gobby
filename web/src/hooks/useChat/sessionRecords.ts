import type {
  ChatMessage,
  ChatMode,
  SessionInteractionMode,
  SessionObservationMeta,
} from "../../types/chat";
import { AUTO_REASONING_EFFORT } from "../../lib/providerModels";

const CHAT_PROVIDERS = new Set(["claude", "gemini", "qwen", "codex", "droid", "agy", "grok"]);

export function isChatProvider(value: unknown): value is string {
  return typeof value === "string" && CHAT_PROVIDERS.has(value);
}

export function isValidSessionType(value: unknown): value is "terminal" | "web_chat" {
  return value === "terminal" || value === "web_chat";
}

export function normalizeSessionType(value: unknown): "terminal" | "web_chat" | null {
  return isValidSessionType(value) ? value : null;
}

export function normalizeReasoningEffort(value: string | null | undefined): string | null {
  if (!value) {
    return null;
  }
  const normalized = value.trim().toLowerCase();
  if (!normalized || normalized === AUTO_REASONING_EFFORT) {
    return null;
  }
  return normalized;
}

export interface CreatedWebChatSession extends Record<string, unknown> {
  id: string;
  source: string;
  model: string | null;
  chat_mode: string | null;
  seq_num: number | null;
  title: string | null;
  status?: string | null;
  external_id?: string | null;
  git_branch?: string | null;
  context_window?: number | null;
}

export interface ContinuationRollbackSnapshot {
  sourceSessionId: string;
  conversationId: string;
  dbSessionId: string | null;
  mainSessionMeta: SessionObservationMeta | null;
  sessionTitle: string | null;
  sessionRef: string | null;
  selectedProvider: string | null;
  messages: ChatMessage[];
  contextUsage: {
    totalInputTokens: number;
    outputTokens: number;
    contextWindow: number | null;
    uncachedInputTokens: number;
    cacheReadTokens: number;
    cacheCreationTokens: number;
  };
  currentMode: ChatMode;
  currentBranch: string | null;
  worktreePath: string | null;
  viewingSessionId: string | null;
  viewingSessionMeta: SessionObservationMeta | null;
  observedSessionId: string | null;
  observedSessionMeta: SessionObservationMeta | null;
  attachedSessionId: string | null;
  attachedSessionMeta: SessionObservationMeta | null;
  sessionInteractionMode: SessionInteractionMode;
  proxyDeliveryNotice: string | null;
}

export async function createWebChatSession(params?: {
  projectId?: string | null;
  provider?: string | null;
  model?: string | null;
  reasoningEffort?: string | null;
  chatMode?: ChatMode | null;
  title?: string | null;
}): Promise<CreatedWebChatSession> {
  const baseUrl = import.meta.env.VITE_API_BASE_URL || "";
  const reasoningEffort = normalizeReasoningEffort(params?.reasoningEffort ?? null);
  const response = await fetch(`${baseUrl}/api/sessions/web-chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      project_id: params?.projectId ?? null,
      provider: params?.provider ?? null,
      model: params?.model ?? null,
      reasoning_effort: reasoningEffort,
      chat_mode: params?.chatMode ?? null,
      title: params?.title ?? null,
    }),
  });

  if (!response.ok) {
    throw new Error(`Failed to create web chat session: ${response.status}`);
  }

  const data = await response.json();
  return data.session as CreatedWebChatSession;
}

export function isWebChatSessionRecord(
  session: Record<string, unknown> | null | undefined,
): boolean {
  return normalizeSessionType(session?.session_type) === "web_chat";
}

const NON_RESTORABLE_SESSION_STATUSES = new Set([
  "expired",
  "archived",
  "closed",
  "ended",
]);

export function isRestorableSessionRecord(
  session: Record<string, unknown> | null | undefined,
): boolean {
  if (!isWebChatSessionRecord(session)) {
    return false;
  }
  const status =
    typeof session?.status === "string" ? session.status.toLowerCase() : null;
  if (!status) return true;
  return !NON_RESTORABLE_SESSION_STATUSES.has(status);
}

function sessionMetaValue<K extends keyof SessionObservationMeta>(
  overrides: Partial<SessionObservationMeta> | undefined,
  key: K,
  fallback: SessionObservationMeta[K],
): SessionObservationMeta[K] {
  return overrides?.[key] ?? fallback;
}

function sessionString(
  session: Record<string, unknown>,
  key: string,
  fallback: string | null,
): string | null {
  const value = session[key];
  return typeof value === "string" ? value : fallback;
}

function sessionBoolean(
  session: Record<string, unknown>,
  key: string,
): boolean | undefined {
  const value = session[key];
  return typeof value === "boolean" ? value : undefined;
}

function sessionNumber(
  session: Record<string, unknown>,
  key: string,
): number | null {
  const value = session[key];
  return typeof value === "number" ? value : null;
}

export function toSessionObservationMeta(
  session: Record<string, unknown> | null,
  overrides?: Partial<SessionObservationMeta>,
): SessionObservationMeta | null {
  if (!session) return null;
  return {
    ref: sessionMetaValue(
      overrides,
      "ref",
      typeof session.seq_num === "number" ? `#${session.seq_num}` : null,
    ),
    source: sessionMetaValue(overrides, "source", sessionString(session, "source", "unknown")!),
    title: sessionMetaValue(overrides, "title", sessionString(session, "title", null)),
    status: sessionMetaValue(overrides, "status", sessionString(session, "status", "unknown")!),
    canProxyAttach: sessionMetaValue(
      overrides,
      "canProxyAttach",
      sessionBoolean(session, "can_proxy_attach"),
    ),
    model: sessionMetaValue(overrides, "model", sessionString(session, "model", null)),
    reasoningEffort: sessionMetaValue(
      overrides,
      "reasoningEffort",
      sessionString(session, "reasoning_effort", null),
    ),
    externalId: sessionMetaValue(
      overrides,
      "externalId",
      sessionString(session, "external_id", "")!,
    ),
    chatMode: sessionMetaValue(overrides, "chatMode", sessionString(session, "chat_mode", null)),
    gitBranch: sessionMetaValue(
      overrides,
      "gitBranch",
      sessionString(session, "git_branch", null),
    ),
    contextWindow: sessionMetaValue(
      overrides,
      "contextWindow",
      sessionNumber(session, "context_window"),
    ),
    agentRunId: sessionMetaValue(
      overrides,
      "agentRunId",
      sessionString(session, "agent_run_id", null),
    ),
    workflowName: sessionMetaValue(
      overrides,
      "workflowName",
      sessionString(session, "workflow_name", null),
    ),
    agentName: sessionMetaValue(
      overrides,
      "agentName",
      sessionString(session, "agent_name", null),
    ),
    sessionType: sessionMetaValue(
      overrides,
      "sessionType",
      normalizeSessionType(session.session_type),
    ),
  };
}
