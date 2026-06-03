import type { WikiJson, WikiSourceRecord } from "../../hooks/useWiki";

export interface WikiLink {
  label: string;
  href: string;
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
  if (typeof value === "number" || typeof value === "boolean") return String(value);
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
    return url.protocol === "http:" || url.protocol === "https:" ? url.toString() : null;
  } catch {
    return null;
  }
}

export function watcherStateText(active: boolean | null, running: boolean | null): string {
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
    links.push({ label: String(source.title || source.wiki_path || "Wiki page"), href: pageUrl });
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

export function healthFindings(payload: WikiJson): { label: string; path: string | null }[] {
  return asArray(payload.findings || payload.issues || payload.errors)
    .map((item) => {
      const record = asRecord(item);
      const message = fieldText(record, ["message", "detail", "code", "severity"]);
      if (!message) return null;
      return { label: message, path: fieldText(record, ["path", "file", "source_path"]) };
    })
    .filter((item): item is { label: string; path: string | null } => item !== null);
}
