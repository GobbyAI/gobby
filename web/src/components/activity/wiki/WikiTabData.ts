import type {
  WikiEnvelope,
  WikiJson,
  WikiSourceRecord,
} from "../../../hooks/useWiki";

export interface WikiLink {
  label: string;
  href: string;
}

export interface WikiFinding {
  label: string;
  path: string | null;
}

export interface WikiMetric {
  label: string;
  value: string;
}

export type WikiSourceFilter = "all" | "linked" | "unlinked";

export const WIKI_SOURCE_FILTERS: Array<{
  value: WikiSourceFilter;
  label: string;
}> = [
  { value: "all", label: "All sources" },
  { value: "linked", label: "Linked page" },
  { value: "unlinked", label: "Needs page" },
];

export interface WikiSummary {
  scopeText: string;
  statusText: string;
  healthText: string;
  metrics: WikiMetric[];
  degradedServices: string[];
  findings: WikiFinding[];
  indexedPaths: string[];
  links: WikiLink[];
  searches: string[];
}

export function asRecord(value: unknown): WikiJson {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as WikiJson)
    : {};
}

export function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

export function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

export function fieldText(record: WikiJson, names: string[]): string | null {
  for (const name of names) {
    const value = stringValue(record[name]);
    if (value) return value;
  }
  return null;
}

export function stringList(record: WikiJson, names: string[]): string[] {
  for (const name of names) {
    const value = record[name];
    if (Array.isArray(value)) {
      return value
        .map((item) =>
          typeof item === "string"
            ? item
            : fieldText(asRecord(item), ["path", "file", "message", "name"]),
        )
        .filter((item): item is string => Boolean(item));
    }
  }
  return [];
}

export function displayValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

export function booleanValue(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

export function timestampValue(value: unknown): string {
  if (typeof value === "number") {
    return new Date(value * 1000).toISOString();
  }
  return stringValue(value) || "never";
}

export function uniqueStrings(items: string[]): string[] {
  return Array.from(new Set(items));
}

function safeHttpUrl(value: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:"
      ? url.toString()
      : null;
  } catch {
    return null;
  }
}

export function watcherStateText(
  active: boolean | null,
  running: boolean | null,
): string {
  if (active === false) return "inactive";
  if (running === true) return "running";
  if (running === false) return "stopped";
  return "unknown";
}

export function sourceLabel(source: WikiSourceRecord): string {
  return source.title || source.path || source.raw_path || source.source_url || source.id;
}

export function sourcePath(source: WikiSourceRecord): string | null {
  return source.path || source.raw_path || source.wiki_path || source.page_path || null;
}

export function sourceLinks(source: WikiSourceRecord): WikiLink[] {
  const links: WikiLink[] = [];
  const pageUrl = safeHttpUrl(stringValue(source.page_url));
  if (pageUrl) {
    links.push({
      label: String(source.title || source.wiki_path || "Wiki page"),
      href: pageUrl,
    });
  }
  const sourceUrl = safeHttpUrl(stringValue(source.source_url || source.url));
  if (sourceUrl) {
    links.push({ label: "Source", href: sourceUrl });
  }
  return links;
}

export function pageLinks(payload: WikiJson): WikiLink[] {
  return asArray(payload.page_links || payload.pages || payload.links)
    .map((item) => {
      const record = asRecord(item);
      const href = safeHttpUrl(stringValue(record.url || record.href || record.page_url));
      if (!href) return null;
      return {
        label: fieldText(record, ["title", "label", "path"]) || href,
        href,
      };
    })
    .filter((item): item is WikiLink => item !== null);
}

export function recentSearches(payload: WikiJson): string[] {
  return asArray(payload.recent_searches || payload.searches)
    .map((item) => {
      if (typeof item === "string") return item;
      const record = asRecord(item);
      const query = fieldText(record, ["query", "text"]);
      if (!query) return null;
      const count = record.result_count ?? record.results;
      return count === undefined ? query : `${query} (${displayValue(count)})`;
    })
    .filter((item): item is string => item !== null);
}

export function healthFindings(payload: WikiJson): WikiFinding[] {
  return asArray(payload.findings || payload.issues || payload.errors)
    .map((item) => {
      const record = asRecord(item);
      const message = fieldText(record, ["message", "detail", "code", "severity"]);
      if (!message) return null;
      return {
        label: message,
        path: fieldText(record, ["path", "file", "source_path"]),
      };
    })
    .filter((item): item is WikiFinding => item !== null);
}

export function buildWikiSummary({
  status,
  health,
  isLoading,
  projectId,
}: {
  status: WikiEnvelope | null;
  health: WikiEnvelope | null;
  isLoading: boolean;
  projectId?: string | null;
}): WikiSummary {
  const statusPayload = asRecord(status?.payload);
  const healthPayload = asRecord(health?.payload);
  const maintenancePayload = asRecord(statusPayload.maintenance);
  const watcherPayload = asRecord(maintenancePayload.watcher);
  const gatewayPayload = asRecord(maintenancePayload.gateway);
  const statusText =
    fieldText(statusPayload, ["status", "state", "mode"]) || "unknown";
  const healthText =
    fieldText(healthPayload, ["status", "state", "health"]) || "unknown";
  const scopeText = (() => {
    const scope = statusPayload.scope;
    if (typeof scope === "string") return scope;
    const scopeRecord = asRecord(scope);
    return fieldText(scopeRecord, ["project", "topic", "name"]) || projectId || "default";
  })();
  const indexedPaths = stringList(statusPayload, [
    "indexed_paths",
    "paths",
    "changed_paths",
  ]);
  const degradedServices = uniqueStrings([
    ...stringList(healthPayload, ["degraded_services", "degraded", "services"]),
    ...stringList(gatewayPayload, ["degraded_services", "degraded", "services"]),
  ]);
  const watcherActive = booleanValue(watcherPayload.active);
  const watcherRunning = booleanValue(watcherPayload.running);
  const pendingDebounce = booleanValue(watcherPayload.pending_debounce);
  const pendingDebounceText =
    pendingDebounce === null ? "unknown" : pendingDebounce ? "pending" : "clear";
  const pendingChangesText =
    watcherPayload.pending_changes === undefined
      ? "unknown"
      : displayValue(watcherPayload.pending_changes);
  const gatewayAvailable = booleanValue(gatewayPayload.available);
  const gatewayAvailableText =
    gatewayAvailable === null
      ? "unknown"
      : gatewayAvailable
        ? "available"
        : "unavailable";
  const gatewayStatusText =
    fieldText(gatewayPayload, ["status", "state"]) || "unknown";

  return {
    scopeText,
    statusText,
    healthText,
    metrics: [
      { label: "Scope", value: scopeText },
      { label: "Status", value: isLoading ? "loading" : statusText },
      { label: "Health", value: isLoading ? "loading" : healthText },
      {
        label: "Watcher",
        value: isLoading ? "loading" : watcherStateText(watcherActive, watcherRunning),
      },
      {
        label: "Pending Debounce",
        value: isLoading ? "loading" : pendingDebounceText,
      },
      {
        label: "Pending Changes",
        value: isLoading ? "loading" : pendingChangesText,
      },
      {
        label: "Last Index",
        value: isLoading ? "loading" : timestampValue(watcherPayload.last_index_time),
      },
      {
        label: "Gateway",
        value: isLoading ? "loading" : gatewayAvailableText,
      },
      {
        label: "Gateway Status",
        value: isLoading ? "loading" : gatewayStatusText,
      },
    ],
    degradedServices,
    findings: healthFindings(healthPayload),
    indexedPaths,
    links: pageLinks(statusPayload),
    searches: recentSearches(statusPayload),
  };
}

export function filterWikiSources(
  sources: WikiSourceRecord[],
  search: string,
  filter: WikiSourceFilter,
): WikiSourceRecord[] {
  const query = search.trim().toLowerCase();
  return sources.filter((source) => {
    const hasLink = sourceLinks(source).length > 0;
    if (filter === "linked" && !hasLink) return false;
    if (filter === "unlinked" && hasLink) return false;
    if (!query) return true;
    return [
      sourceLabel(source),
      sourcePath(source),
      source.wiki_path,
      source.page_path,
      source.id,
    ]
      .filter((value): value is string => Boolean(value))
      .some((value) => value.toLowerCase().includes(query));
  });
}
