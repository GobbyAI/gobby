/**
 * Typed fetchers and defensive normalizers over the daemon wiki envelope
 * (`{ok, command, payload, stderr}` from GwikiGateway). Field access follows
 * the asRecord/fieldText style: every payload field is re-checked at runtime
 * because envelopes come from an external binary whose shape can drift.
 */

import { load as loadYaml } from "js-yaml";

import type { WikiEnvelope, WikiJson } from "../../../hooks/useWiki";
import type {
  WikiGraphEdge,
  WikiGraphInclude,
  WikiGraphNode,
  WikiGraphPayload,
  WikiOutputMeta,
  WikiPageMeta,
} from "./WikiTabModel";

export interface WikiFetchScope {
  projectId?: string | null;
  topic?: string | null;
}

// ── Defensive field helpers ─────────────────────────────────────

export function asRecord(value: unknown): Record<string, unknown> {
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return {};
}

function asList(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

export function fieldText(record: Record<string, unknown>, ...keys: string[]): string | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.length > 0) return value;
  }
  return null;
}

export function fieldNumber(record: Record<string, unknown>, ...keys: string[]): number | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return null;
}

export function fieldStringList(record: Record<string, unknown>, key: string): string[] {
  return asList(record[key]).filter((item): item is string => typeof item === "string");
}

function fieldBoolean(record: Record<string, unknown>, key: string): boolean {
  return record[key] === true;
}

// ── Envelope transport ──────────────────────────────────────────

function wikiQuery(
  scope: WikiFetchScope,
  params: Record<string, string | number | boolean | null | undefined> = {},
): string {
  const search = new URLSearchParams();
  if (scope.projectId) search.set("project", scope.projectId);
  if (scope.topic) search.set("topic", scope.topic);
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined) continue;
    const text = String(value);
    if (text.trim()) search.set(key, text);
  }
  const query = search.toString();
  return query ? `?${query}` : "";
}

async function parseBody(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch (error) {
    throw new Error(`HTTP ${response.status} returned invalid JSON: ${String(error)}`);
  }
}

function errorMessage(body: unknown, status: number): string {
  const detail = asRecord(body).detail;
  if (typeof detail === "string") return humanizeWikiError(detail);
  const detailRecord = asRecord(detail);
  const message = fieldText(asRecord(detailRecord.error), "message") ?? fieldText(detailRecord, "stderr");
  return message ?? `HTTP ${status}`;
}

async function readEnvelope(path: string, init?: RequestInit): Promise<WikiEnvelope> {
  const response = await fetch(path, init);
  const body = await parseBody(response);
  if (!response.ok) {
    throw new Error(errorMessage(body, response.status));
  }
  return body as WikiEnvelope;
}

function envelopePayload(envelope: WikiEnvelope): Record<string, unknown> {
  return asRecord(envelope.payload);
}

// ── Normalizers ─────────────────────────────────────────────────

function normalizeGraphNode(value: unknown): WikiGraphNode | null {
  const record = asRecord(value);
  const id = fieldText(record, "id");
  if (!id) return null;
  return {
    id,
    kind: fieldText(record, "kind") ?? "unknown",
    path: fieldText(record, "path"),
    title: fieldText(record, "title"),
  };
}

function normalizeGraphEdge(value: unknown, kind: string): WikiGraphEdge | null {
  const record = asRecord(value);
  const source = fieldText(record, "source");
  const target = fieldText(record, "target");
  if (!source || !target) return null;
  return {
    source,
    target,
    kind: fieldText(record, "kind") ?? kind,
    rawTarget: fieldText(record, "raw_target"),
  };
}

export function normalizeGraph(payload: unknown): WikiGraphPayload {
  const outer = asRecord(payload);
  // Live gwiki envelopes nest everything under payload.graph (nodes, edges,
  // analytics, degraded flags); flat payloads carry the fields directly.
  const record = outer.graph === undefined ? outer : asRecord(outer.graph);
  const nodes = asList(record.nodes)
    .map(normalizeGraphNode)
    .filter((node): node is WikiGraphNode => node !== null);
  // Edges arrive keyed by kind (links/imports/calls/callers/trust/audit).
  const edges: WikiGraphEdge[] = [];
  for (const [kind, list] of Object.entries(asRecord(record.edges))) {
    for (const value of asList(list)) {
      const edge = normalizeGraphEdge(value, kind);
      if (edge) edges.push(edge);
    }
  }
  const analytics = record.analytics === undefined ? null : asRecord(record.analytics);
  return {
    nodes,
    edges,
    degraded: fieldBoolean(record, "degraded"),
    degradedSources: fieldStringList(record, "degraded_sources"),
    analytics,
  };
}

function pageTitleFromPath(path: string): string {
  const segments = path.split("/").filter(Boolean);
  const segment = segments.length > 0 ? segments[segments.length - 1] : path;
  return segment.endsWith(".md") ? segment.slice(0, -3) : segment;
}

function normalizePageMeta(value: unknown): WikiPageMeta | null {
  const record = asRecord(value);
  const path = fieldText(record, "path");
  if (!path) return null;
  return {
    path,
    title: fieldText(record, "title") ?? pageTitleFromPath(path),
    tags: fieldStringList(record, "tags"),
    contentHash: fieldText(record, "content_hash"),
    updatedAt: fieldText(record, "updated_at"),
  };
}

function normalizeOutputMeta(value: unknown): WikiOutputMeta | null {
  const record = asRecord(value);
  const path = fieldText(record, "path");
  if (!path) return null;
  return {
    path,
    size: fieldNumber(record, "size"),
    modified: fieldText(record, "modified"),
  };
}

export interface WikiPagesResult {
  pages: WikiPageMeta[];
  outputs: WikiOutputMeta[];
}

export function normalizePages(payload: unknown): WikiPagesResult {
  const record = asRecord(payload);
  return {
    pages: asList(record.pages)
      .map(normalizePageMeta)
      .filter((page): page is WikiPageMeta => page !== null),
    outputs: asList(record.outputs)
      .map(normalizeOutputMeta)
      .filter((output): output is WikiOutputMeta => output !== null),
  };
}

export interface WikiPageCandidate {
  path: string;
  title: string | null;
}

export interface WikiPageDetail {
  path: string | null;
  title: string | null;
  /** Raw page content including any frontmatter block. */
  content: string;
  /** Content with the frontmatter block stripped. */
  body: string;
  frontmatter: Record<string, unknown>;
  contentHash: string | null;
  status: string | null;
  truncated: boolean;
  /** Populated on `status: "ambiguous"` reads — the match-picker input. */
  candidates: WikiPageCandidate[];
}

const FRONTMATTER_PATTERN = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/;

function splitFrontmatter(content: string): { frontmatter: Record<string, unknown>; body: string } {
  const match = FRONTMATTER_PATTERN.exec(content);
  if (!match) return { frontmatter: {}, body: content };
  const body = content.slice(match[0].length).replace(/^\r?\n/, "");
  try {
    return { frontmatter: asRecord(loadYaml(match[1])), body };
  } catch {
    // A malformed block degrades to {} so one bad page cannot break the reader.
    return { frontmatter: {}, body };
  }
}

function normalizePageCandidate(value: unknown): WikiPageCandidate | null {
  if (typeof value === "string") return value ? { path: value, title: null } : null;
  const record = asRecord(value);
  const path = fieldText(record, "path", "wiki_path");
  if (!path) return null;
  return { path, title: fieldText(record, "title") };
}

export function normalizePage(payload: unknown): WikiPageDetail {
  const record = asRecord(payload);
  const content = fieldText(record, "content") ?? "";
  const { frontmatter, body } = splitFrontmatter(content);
  return {
    path: fieldText(record, "wiki_path", "path"),
    title: fieldText(record, "title"),
    content,
    body,
    frontmatter,
    contentHash: fieldText(record, "content_hash"),
    status: fieldText(record, "status"),
    truncated: fieldBoolean(record, "truncated"),
    candidates: asList(record.candidates ?? record.matches)
      .map(normalizePageCandidate)
      .filter((candidate): candidate is WikiPageCandidate => candidate !== null),
  };
}

export interface WikiBacklink {
  sourcePath: string;
  targetPath: string | null;
  rawTarget: string | null;
}

export function normalizeBacklinks(payload: unknown): WikiBacklink[] {
  const record = asRecord(payload);
  const backlinks: WikiBacklink[] = [];
  for (const value of asList(record.backlinks)) {
    const row = asRecord(value);
    const sourcePath = fieldText(row, "source_path");
    if (!sourcePath) continue;
    backlinks.push({
      sourcePath,
      targetPath: fieldText(row, "target_path"),
      rawTarget: fieldText(row, "raw_target"),
    });
  }
  return backlinks;
}

export interface WikiSearchHit {
  title: string | null;
  wikiPage: string | null;
  sourcePath: string | null;
  resultType: string | null;
  snippet: string;
  score: number | null;
  sources: string[];
}

function normalizeSearchHit(value: unknown): WikiSearchHit {
  const record = asRecord(value);
  return {
    title: fieldText(record, "title"),
    wikiPage: fieldText(record, "wiki_page"),
    sourcePath: fieldText(record, "source_path"),
    resultType: fieldText(record, "result_type"),
    snippet: fieldText(record, "snippet") ?? "",
    score: fieldNumber(record, "score"),
    sources: fieldStringList(record, "sources"),
  };
}

export interface WikiSearchResult {
  query: string | null;
  hits: WikiSearchHit[];
  warnings: string[];
  hint: string | null;
}

export function normalizeSearch(payload: unknown): WikiSearchResult {
  const record = asRecord(payload);
  return {
    query: fieldText(record, "query"),
    hits: asList(record.hits).map(normalizeSearchHit),
    warnings: fieldStringList(record, "warnings"),
    hint: fieldText(record, "hint"),
  };
}

export interface WikiServiceState {
  name: string;
  configured: boolean;
}

export interface WikiStatusSummary {
  state: "loading" | "ready" | "degraded" | "unavailable";
  message: string | null;
  services: WikiServiceState[];
  degradedServices: string[];
  brokenLinks: number;
  stalePages: number;
  uncompiledSources: number;
}

/** Gateway failures arrive as raw bodies like `Error: {"code":…,"message":…}`;
 * banner copy wants the human `message` field, not the machine envelope. */
function humanizeWikiError(text: string): string {
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start === -1 || end <= start) return text;
  try {
    const parsed: unknown = JSON.parse(text.slice(start, end + 1));
    if (parsed && typeof parsed === "object") {
      const message = (parsed as Record<string, unknown>).message;
      if (typeof message === "string" && message.trim()) return message;
    }
  } catch {
    // Not an embedded JSON envelope — keep the original text.
  }
  return text;
}

/**
 * Successor to the deleted buildWikiSummary: condenses the useWiki
 * status/health envelopes into the shape the §2.2 degraded banner renders.
 */
export function summarizeWikiStatus(
  status: WikiEnvelope | null,
  health: WikiEnvelope | null,
  error?: string | null,
  isLoading = false,
): WikiStatusSummary {
  if (error || !status) {
    // No envelope and no failure yet means the first fetch is still in
    // flight — that's "loading", not an outage banner. A real error wins
    // even while a retry is in flight.
    const loading = isLoading && !error;
    return {
      state: loading ? "loading" : "unavailable",
      message: loading
        ? "Checking wiki status…"
        : error
          ? humanizeWikiError(error)
          : "Wiki status unavailable",
      services: [],
      degradedServices: [],
      brokenLinks: 0,
      stalePages: 0,
      uncompiledSources: 0,
    };
  }
  const statusPayload = asRecord(status.payload);
  const services: WikiServiceState[] = Object.entries(asRecord(statusPayload.services)).map(
    ([name, value]) => ({ name, configured: fieldBoolean(asRecord(value), "configured") }),
  );
  const degradedServices = services
    .filter((service) => !service.configured)
    .map((service) => service.name);
  const healthPayload = asRecord(health?.payload);
  return {
    state: degradedServices.length > 0 ? "degraded" : "ready",
    message: fieldText(statusPayload, "status"),
    services,
    degradedServices,
    brokenLinks: asList(healthPayload.broken_links).length,
    stalePages: asList(healthPayload.stale_pages).length,
    uncompiledSources: asList(healthPayload.uncompiled_sources).length,
  };
}

// ── Fetchers ────────────────────────────────────────────────────

export async function fetchGraph(
  scope: WikiFetchScope,
  include: WikiGraphInclude = "all",
): Promise<WikiGraphPayload> {
  const envelope = await readEnvelope(`/api/wiki/graph${wikiQuery(scope, { include })}`);
  return normalizeGraph(envelopePayload(envelope));
}

export async function fetchPages(
  scope: WikiFetchScope,
  prefix?: string,
): Promise<WikiPagesResult> {
  const envelope = await readEnvelope(`/api/wiki/pages${wikiQuery(scope, { prefix })}`);
  return normalizePages(envelopePayload(envelope));
}

export async function fetchPage(
  scope: WikiFetchScope,
  selector: { path?: string; title?: string },
): Promise<WikiPageDetail> {
  const envelope = await readEnvelope(
    `/api/wiki/read${wikiQuery(scope, { path: selector.path, title: selector.title })}`,
  );
  return normalizePage(envelopePayload(envelope));
}

export async function fetchBacklinks(
  scope: WikiFetchScope,
  target: string,
): Promise<WikiBacklink[]> {
  const envelope = await readEnvelope(`/api/wiki/backlinks${wikiQuery(scope, { target })}`);
  return normalizeBacklinks(envelopePayload(envelope));
}

export async function fetchSearch(
  scope: WikiFetchScope,
  query: string,
  limit?: number,
): Promise<WikiSearchResult> {
  const envelope = await readEnvelope(`/api/wiki/search${wikiQuery(scope, { query, limit })}`);
  return normalizeSearch(envelopePayload(envelope));
}

export type WikiPageWriteMode = "upsert" | "create" | "replace";

export interface WikiSaveRequest {
  path: string;
  content: string;
  mode?: WikiPageWriteMode;
  expectedHash?: string | null;
}

export type WikiSaveResult =
  | { ok: true; path: string | null; created: boolean; contentHash: string | null }
  | { ok: false; conflict: true; code: "precondition_failed" | "already_exists"; message: string };

const CONFLICT_STATUS_CODES: Record<number, "precondition_failed" | "already_exists"> = {
  412: "precondition_failed",
  409: "already_exists",
};

export async function savePage(
  scope: WikiFetchScope,
  request: WikiSaveRequest,
): Promise<WikiSaveResult> {
  const response = await fetch(`/api/wiki/write${wikiQuery(scope)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      path: request.path,
      content: request.content,
      mode: request.mode ?? "upsert",
      expected_hash: request.expectedHash ?? undefined,
    }),
  });
  const body = await parseBody(response);
  const conflictCode = CONFLICT_STATUS_CODES[response.status];
  if (conflictCode) {
    const detail = asRecord(asRecord(body).detail);
    const payloadCode = fieldText(asRecord(detail.payload), "code");
    return {
      ok: false,
      conflict: true,
      code: payloadCode === "already_exists" || payloadCode === "precondition_failed"
        ? payloadCode
        : conflictCode,
      message: errorMessage(body, response.status),
    };
  }
  if (!response.ok) {
    throw new Error(errorMessage(body, response.status));
  }
  const payload = asRecord(asRecord(body).payload);
  return {
    ok: true,
    path: fieldText(payload, "path"),
    created: fieldBoolean(payload, "created"),
    contentHash: fieldText(payload, "content_hash"),
  };
}

export async function deletePage(scope: WikiFetchScope, path: string): Promise<WikiJson> {
  const envelope = await readEnvelope(`/api/wiki/delete${wikiQuery(scope)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  return envelopePayload(envelope);
}

export interface CodewikiStatus {
  enabled: boolean;
  state: string;
  reason: string;
}

/** Dormant wiki-owned status response — plain JSON, not an envelope. */
function normalizeCodewikiStatus(body: unknown): CodewikiStatus {
  const record = asRecord(body);
  return {
    enabled: fieldBoolean(record, "enabled"),
    state: fieldText(record, "state") ?? "unknown",
    reason: fieldText(record, "reason") ?? "unknown",
  };
}

export async function fetchCodewikiStatus(): Promise<CodewikiStatus> {
  const response = await fetch("/api/wiki/code/status");
  const body = await parseBody(response);
  if (!response.ok) {
    throw new Error(errorMessage(body, response.status));
  }
  return normalizeCodewikiStatus(body);
}
