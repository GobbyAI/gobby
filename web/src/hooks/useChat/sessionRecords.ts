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

export function toSessionObservationMeta(
  session: Record<string, unknown> | null,
  overrides?: Partial<SessionObservationMeta>,
): SessionObservationMeta | null {
  if (!session) return null;
  return {
    ref:
      overrides?.ref ??
      (typeof session.seq_num === "number" ? `#${session.seq_num}` : null),
    source:
      overrides?.source ??
      (typeof session.source === "string" ? session.source : "unknown"),
    title:
      overrides?.title ??
      (typeof session.title === "string" ? session.title : null),
    status:
      overrides?.status ??
      (typeof session.status === "string" ? session.status : "unknown"),
    canProxyAttach:
      overrides?.canProxyAttach ??
      (typeof session.can_proxy_attach === "boolean"
        ? session.can_proxy_attach
        : undefined),
    model:
      overrides?.model ??
      (typeof session.model === "string" ? session.model : null),
    reasoningEffort:
      overrides?.reasoningEffort ??
      (typeof session.reasoning_effort === "string"
        ? session.reasoning_effort
        : null),
    externalId:
      overrides?.externalId ??
      (typeof session.external_id === "string" ? session.external_id : ""),
    chatMode:
      overrides?.chatMode ??
      (typeof session.chat_mode === "string" ? session.chat_mode : null),
    gitBranch:
      overrides?.gitBranch ??
      (typeof session.git_branch === "string" ? session.git_branch : null),
    contextWindow:
      overrides?.contextWindow ??
      (typeof session.context_window === "number"
        ? session.context_window
        : null),
    agentRunId:
      overrides?.agentRunId ??
      (typeof session.agent_run_id === "string" ? session.agent_run_id : null),
    workflowName:
      overrides?.workflowName ??
      (typeof session.workflow_name === "string"
        ? session.workflow_name
        : null),
    agentName:
      overrides?.agentName ??
      (typeof session.agent_name === "string" ? session.agent_name : null),
    sessionType:
      overrides?.sessionType ?? normalizeSessionType(session.session_type),
  };
}
