import type { GobbySession } from "../../types/sessions";

export interface RunningAgent {
  run_id: string;
  provider: string;
  model?: string | null;
  is_local?: boolean | number | string | null;
  pid?: number;
  mode?: string;
  started_at?: string;
  session_id?: string;
}

export interface WatchingSessionEntry {
  id: string;
  type: "agent" | "session";
  label: string;
  provider: string;
  status: string;
  sessionType?: string;
  externalId?: string;
  agentRunId?: string | null;
  runId?: string;
  startedAt?: string;
  updatedAt?: string;
  seqNum?: number | null;
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  hasTmux: boolean;
  sandboxEnabled: boolean;
  isLocal: boolean;
}

export interface SessionContextMenu {
  x: number;
  y: number;
  entry: WatchingSessionEntry;
}

export interface Badge {
  label: string;
  className: string;
}

export const WATCHING_SESSION_ID_KEY = "gobby-watching-session-id";
export const HIDDEN_SOURCES = new Set(["pipeline", "cron", "system"]);
const LOCAL_LEGACY_PROVIDERS = new Set(["lmstudio", "ollama", "llamacpp", "local"]);

export function getBaseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL || "";
}

function normalizeLocalText(value: unknown): string {
  return typeof value === "string" ? value.trim().toLowerCase() : "";
}

function readExplicitLocalFlag(value: unknown): boolean | null {
  if (typeof value === "boolean") {
    return value;
  }
  if (typeof value === "number") {
    return value !== 0;
  }
  if (typeof value === "string") {
    const normalized = normalizeLocalText(value);
    if (["1", "true", "yes"].includes(normalized)) {
      return true;
    }
    if (["0", "false", "no"].includes(normalized)) {
      return false;
    }
  }
  return null;
}

function isLocalLegacyFallback(provider: unknown, model: unknown): boolean {
  const normalizedProvider = normalizeLocalText(provider);
  const normalizedModel = normalizeLocalText(model);
  return (
    LOCAL_LEGACY_PROVIDERS.has(normalizedProvider) ||
    /^gpt-oss(?:[-_:./]|$)/.test(normalizedModel)
  );
}

export function resolveLocalFlag(
  flag: unknown,
  provider: unknown,
  model: unknown,
): boolean {
  return readExplicitLocalFlag(flag) ?? isLocalLegacyFallback(provider, model);
}

function getSessionTypeBadge(sessionType: string | undefined): Badge {
  if (sessionType === "web_chat") {
    return { label: "web", className: "chip chip--web" };
  }
  return { label: "tmux", className: "chip chip--tmux" };
}

function getAgentBadge(agentRunId: string | null | undefined): Badge | null {
  if (!agentRunId) return null;
  return { label: "auto", className: "chip chip--auto" };
}

function getSandboxBadge(sandboxEnabled: boolean): Badge | null {
  if (!sandboxEnabled) return null;
  return { label: "SB", className: "chip chip--sandbox" };
}

function getLocalBadge(entry: WatchingSessionEntry): Badge | null {
  if (!entry.isLocal) return null;
  return { label: "LOCAL", className: "chip chip--local" };
}

export function renderBadges(entry: WatchingSessionEntry) {
  const badges = [
    getSessionTypeBadge(entry.sessionType),
    getLocalBadge(entry),
    getSandboxBadge(entry.sandboxEnabled),
    getAgentBadge(entry.agentRunId),
  ]
    .filter((badge): badge is Badge => Boolean(badge))
    .sort((left, right) =>
      left.label.localeCompare(right.label, undefined, { sensitivity: "base" }),
    );

  return (
    <>
      {badges.map((badge) => (
        <span
          key={`${badge.className}:${badge.label}`}
          className={badge.className}
        >
          {badge.label}
        </span>
      ))}
    </>
  );
}

export function matchesSearch(session: GobbySession, search: string): boolean {
  if (!search.trim()) {
    return true;
  }
  const query = search.trim().toLowerCase();
  return (
    (session.title && session.title.toLowerCase().includes(query)) ||
    session.ref.toLowerCase().includes(query) ||
    (session.external_id?.toLowerCase().includes(query) ?? false)
  );
}

export function entryTimestamp(entry: WatchingSessionEntry): number {
  const raw = entry.updatedAt ?? entry.startedAt ?? null;
  return parseTimestamp(raw);
}

export function parseTimestamp(value: string | null | undefined): number {
  if (!value) return 0;
  const parsed = new Date(value).getTime();
  return Number.isFinite(parsed) ? parsed : 0;
}
