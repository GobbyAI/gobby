import { useState, type ReactNode } from "react";

import {
  useWiki,
  type WikiEnvelope,
  type WikiJson,
  type WikiSourceRecord,
} from "../../hooks/useWiki";
import { WikiSourceRemovalDialog } from "./WikiSourceRemovalDialog";

interface WikiTabProps {
  projectId?: string | null;
}

interface WikiLink {
  label: string;
  href: string;
}

function asRecord(value: unknown): WikiJson {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as WikiJson)
    : {};
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function fieldText(record: WikiJson, names: string[]): string | null {
  for (const name of names) {
    const value = stringValue(record[name]);
    if (value) return value;
  }
  return null;
}

function stringList(record: WikiJson, names: string[]): string[] {
  for (const name of names) {
    const value = record[name];
    if (Array.isArray(value)) {
      return value
        .map((item) => (typeof item === "string" ? item : fieldText(asRecord(item), ["path", "file", "message", "name"])))
        .filter((item): item is string => Boolean(item));
    }
  }
  return [];
}

function displayValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function booleanValue(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

function timestampValue(value: unknown): string {
  if (typeof value === "number") {
    return new Date(value * 1000).toISOString();
  }
  return stringValue(value) || "never";
}

function uniqueStrings(items: string[]): string[] {
  return Array.from(new Set(items));
}

function watcherStateText(active: boolean | null, running: boolean | null): string {
  if (active === false) return "inactive";
  if (running === true) return "running";
  if (running === false) return "stopped";
  return "unknown";
}

function sourceLabel(source: WikiSourceRecord): string {
  return source.title || source.path || source.raw_path || source.source_url || source.id;
}

function sourcePath(source: WikiSourceRecord): string | null {
  return source.path || source.raw_path || source.wiki_path || source.page_path || null;
}

function sourceLinks(source: WikiSourceRecord): WikiLink[] {
  const links: WikiLink[] = [];
  const pageUrl = stringValue(source.page_url);
  if (pageUrl) {
    links.push({ label: String(source.title || source.wiki_path || "Wiki page"), href: pageUrl });
  }
  const sourceUrl = stringValue(source.source_url || source.url);
  if (sourceUrl) {
    links.push({ label: "Source", href: sourceUrl });
  }
  return links;
}

function pageLinks(payload: WikiJson): WikiLink[] {
  return asArray(payload.page_links || payload.pages || payload.links)
    .map((item) => {
      const record = asRecord(item);
      const href = stringValue(record.url || record.href || record.page_url);
      if (!href) return null;
      return {
        label: fieldText(record, ["title", "label", "path"]) || href,
        href,
      };
    })
    .filter((item): item is WikiLink => item !== null);
}

function recentSearches(payload: WikiJson): string[] {
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

function healthFindings(payload: WikiJson): { label: string; path: string | null }[] {
  return asArray(payload.findings || payload.issues || payload.errors)
    .map((item) => {
      const record = asRecord(item);
      const message = fieldText(record, ["message", "detail", "code", "severity"]);
      if (!message) return null;
      return { label: message, path: fieldText(record, ["path", "file", "source_path"]) };
    })
    .filter((item): item is { label: string; path: string | null } => item !== null);
}

function StatBlock({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-border bg-muted/20 p-3">
      <div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="mt-1 text-sm font-medium text-foreground">{value}</div>
    </div>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <section className="space-y-2">
      <h3 className="text-sm font-semibold text-foreground">{title}</h3>
      {children}
    </section>
  );
}

function EmptyLine({ text }: { text: string }) {
  return <div className="text-sm text-muted-foreground">{text}</div>;
}

function LinesList({ items }: { items: string[] }) {
  if (!items.length) return <EmptyLine text="None" />;
  return (
    <ul className="space-y-1">
      {items.map((item, index) => (
        <li key={`${item}-${index}`} className="break-all rounded-md bg-muted/20 px-2 py-1 text-sm">
          {item}
        </li>
      ))}
    </ul>
  );
}

function LinksList({ links }: { links: WikiLink[] }) {
  if (!links.length) return <EmptyLine text="None" />;
  return (
    <ul className="space-y-1">
      {links.map((link) => (
        <li key={`${link.href}-${link.label}`}>
          <a className="text-sm text-accent hover:underline" href={link.href}>
            {link.label}
          </a>
        </li>
      ))}
    </ul>
  );
}

export function WikiTab({ projectId }: WikiTabProps) {
  const { status, health, sources, isLoading, error, refresh, removeSource } = useWiki({ projectId });
  const [removingSource, setRemovingSource] = useState<WikiSourceRecord | null>(null);
  const [preview, setPreview] = useState<WikiEnvelope | null>(null);
  const [removalError, setRemovalError] = useState<string | null>(null);
  const [isPreviewLoading, setIsPreviewLoading] = useState(false);
  const [isConfirming, setIsConfirming] = useState(false);

  const statusPayload = asRecord(status?.payload);
  const healthPayload = asRecord(health?.payload);
  const maintenancePayload = asRecord(statusPayload.maintenance);
  const watcherPayload = asRecord(maintenancePayload.watcher);
  const gatewayPayload = asRecord(maintenancePayload.gateway);
  const statusText = fieldText(statusPayload, ["status", "state", "mode"]) || "unknown";
  const healthText = fieldText(healthPayload, ["status", "state", "health"]) || "unknown";
  const scopeText = (() => {
    const scope = statusPayload.scope;
    if (typeof scope === "string") return scope;
    const scopeRecord = asRecord(scope);
    return fieldText(scopeRecord, ["project", "topic", "name"]) || projectId || "default";
  })();
  const indexedPaths = stringList(statusPayload, ["indexed_paths", "paths", "changed_paths"]);
  const degradedServices = uniqueStrings([
    ...stringList(healthPayload, ["degraded_services", "degraded", "services"]),
    ...stringList(gatewayPayload, ["degraded_services", "degraded", "services"]),
  ]);
  const findings = healthFindings(healthPayload);
  const links = pageLinks(statusPayload);
  const searches = recentSearches(statusPayload);
  const watcherActive = booleanValue(watcherPayload.active);
  const watcherRunning = booleanValue(watcherPayload.running);
  const watcherText = watcherStateText(watcherActive, watcherRunning);
  const pendingDebounce = booleanValue(watcherPayload.pending_debounce);
  const pendingDebounceText =
    pendingDebounce === null ? "unknown" : pendingDebounce ? "pending" : "clear";
  const pendingChangesText =
    watcherPayload.pending_changes === undefined
      ? "unknown"
      : displayValue(watcherPayload.pending_changes);
  const lastIndexText = timestampValue(watcherPayload.last_index_time);
  const gatewayAvailable = booleanValue(gatewayPayload.available);
  const gatewayAvailableText =
    gatewayAvailable === null ? "unknown" : gatewayAvailable ? "available" : "unavailable";
  const gatewayStatusText = fieldText(gatewayPayload, ["status", "state"]) || "unknown";

  const openRemoval = async (source: WikiSourceRecord) => {
    setRemovingSource(source);
    setPreview(null);
    setRemovalError(null);
    setIsPreviewLoading(true);
    try {
      const nextPreview = await removeSource({ id: source.id, dry_run: true });
      setPreview(nextPreview);
    } catch (nextError) {
      setRemovalError(String(nextError));
    } finally {
      setIsPreviewLoading(false);
    }
  };

  const confirmRemoval = async ({ keep_asset }: { keep_asset: boolean }) => {
    if (!removingSource) return;
    setIsConfirming(true);
    setRemovalError(null);
    try {
      await removeSource({ id: removingSource.id, yes: true, keep_asset });
      setRemovingSource(null);
      setPreview(null);
      await refresh();
    } catch (nextError) {
      setRemovalError(String(nextError));
    } finally {
      setIsConfirming(false);
    }
  };

  return (
    <div className="flex h-full flex-col gap-4 overflow-auto p-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-foreground">Wiki</h2>
          <p className="text-xs text-muted-foreground">{scopeText}</p>
        </div>
        <button
          type="button"
          onClick={() => void refresh()}
          className="rounded-md border border-border px-3 py-1.5 text-sm text-foreground hover:bg-muted"
        >
          Refresh
        </button>
      </div>

      {error ? (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive">
          {error}
        </div>
      ) : null}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <StatBlock label="Scope" value={scopeText} />
        <StatBlock label="Status" value={isLoading ? "loading" : statusText} />
        <StatBlock label="Health" value={isLoading ? "loading" : healthText} />
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <StatBlock label="Watcher" value={isLoading ? "loading" : watcherText} />
        <StatBlock label="Pending Debounce" value={isLoading ? "loading" : pendingDebounceText} />
        <StatBlock label="Pending Changes" value={isLoading ? "loading" : pendingChangesText} />
        <StatBlock label="Last Index" value={isLoading ? "loading" : lastIndexText} />
        <StatBlock label="Gateway" value={isLoading ? "loading" : gatewayAvailableText} />
        <StatBlock label="Gateway Status" value={isLoading ? "loading" : gatewayStatusText} />
      </div>

      <Section title="Degraded Services">
        <LinesList items={degradedServices} />
      </Section>

      <Section title="Health Findings">
        {findings.length ? (
          <ul className="space-y-1">
            {findings.map((finding, index) => (
              <li key={`${finding.label}-${index}`} className="rounded-md bg-muted/20 px-2 py-1 text-sm">
                <span>{finding.label}</span>
                {finding.path ? <span className="ml-2 break-all text-muted-foreground">{finding.path}</span> : null}
              </li>
            ))}
          </ul>
        ) : (
          <EmptyLine text="None" />
        )}
      </Section>

      <Section title="Recent Searches">
        <LinesList items={searches} />
      </Section>

      <Section title="Indexed Paths">
        <LinesList items={indexedPaths} />
      </Section>

      <Section title="Wiki Page Links">
        <LinksList links={links} />
      </Section>

      <Section title="Source Records">
        {sources.length ? (
          <ul className="space-y-2">
            {sources.map((source) => (
              <li key={source.id} className="rounded-md border border-border bg-background p-3">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="break-words text-sm font-medium text-foreground">{sourceLabel(source)}</div>
                    <div className="break-all text-xs text-muted-foreground">{sourcePath(source) || source.id}</div>
                    <div className="mt-2 flex flex-wrap gap-2">
                      {sourceLinks(source).map((link) => (
                        <a key={`${source.id}-${link.href}`} href={link.href} className="text-xs text-accent hover:underline">
                          {link.label}
                        </a>
                      ))}
                    </div>
                  </div>
                  <button
                    type="button"
                    aria-label={`Remove ${sourceLabel(source)}`}
                    onClick={() => void openRemoval(source)}
                    className="shrink-0 rounded-md border border-destructive/50 px-2 py-1 text-xs text-destructive hover:bg-destructive/10"
                  >
                    Remove
                  </button>
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <EmptyLine text={isLoading ? "Loading" : "None"} />
        )}
      </Section>

      <WikiSourceRemovalDialog
        source={removingSource}
        preview={preview}
        isPreviewLoading={isPreviewLoading}
        isConfirming={isConfirming}
        error={removalError}
        onCancel={() => setRemovingSource(null)}
        onConfirm={(options) => void confirmRemoval(options)}
      />
    </div>
  );
}
