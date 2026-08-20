import type { AcpSessionInfo, GobbySession } from "../../types/sessions";
import { Chip } from "../ui/Chip";
import { chipIdentityClasses } from "../ui/chipVariants";

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
  blockedCount?: number;
  attentionReasons?: readonly string[];
  // Present only on ACP-backed rows; drives the leading "ACP" kind chip and
  // (in later tasks) the capability-gated row actions. Detect via Boolean(acp).
  acp?: AcpSessionInfo | null;
}

export interface SessionContextMenu {
  x: number;
  y: number;
  width: number;
  height: number;
  entry: WatchingSessionEntry;
  trigger: HTMLButtonElement;
}

export interface Badge {
  label: string;
}

export const WATCHING_SESSION_ID_KEY = "gobby-watching-session-id";
export const HIDDEN_SOURCES = new Set(["pipeline", "cron", "system"]);
const LOCAL_LEGACY_PROVIDERS = new Set([
  "lmstudio",
  "ollama",
  "vllm",
  "llamacpp",
  "local",
]);

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
    return { label: "web" };
  }
  return { label: "tmux" };
}

// The session's canonical kind. ACP rows replace the web/tmux chip with an
// "ACP" chip so ACP reads as a first-class kind alongside WEB and TMUX.
function getKindBadge(entry: WatchingSessionEntry): Badge {
  if (entry.acp) {
    return { label: "ACP" };
  }
  return getSessionTypeBadge(entry.sessionType);
}

function getAgentBadge(agentRunId: string | null | undefined): Badge | null {
  if (!agentRunId) return null;
  return { label: "auto" };
}

function getSandboxBadge(sandboxEnabled: boolean): Badge | null {
  if (!sandboxEnabled) return null;
  return { label: "SB" };
}

function getLocalBadge(entry: WatchingSessionEntry): Badge | null {
  if (!entry.isLocal) return null;
  return { label: "LOCAL" };
}

export function renderBadges(entry: WatchingSessionEntry) {
  // The kind chip (web/tmux/ACP) leads, outside the alphabetical sort, so the
  // session's identity stays in a stable first position. Mode chips follow,
  // sorted, since their presence varies per row.
  const kindBadge = getKindBadge(entry);
  const modeBadges = [
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
      <Chip tone="accent" uppercase className={chipIdentityClasses}>
        {kindBadge.label}
      </Chip>
      {(entry.blockedCount ?? 0) > 0 && (
        <Chip
          tone="warning"
          uppercase
          className="gap-1 border border-[color-mix(in_srgb,var(--color-warning-foreground)_35%,transparent)] bg-[var(--color-warning-soft)]"
          aria-label={[
            `Blocked attention: ${entry.blockedCount}`,
            ...(entry.attentionReasons ?? []),
          ].join("; ")}
          title={entry.attentionReasons?.join("; ")}
        >
          <span aria-hidden="true">!</span>
          blocked {entry.blockedCount}
        </Chip>
      )}
      {modeBadges.map((badge) => (
        <Chip
          key={badge.label}
          tone="accent"
          uppercase
          className={chipIdentityClasses}
        >
          {badge.label}
        </Chip>
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
