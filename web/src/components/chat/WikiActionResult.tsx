import type { WikiEnvelope, WikiJson } from "../../hooks/useWiki";

export type WikiActionKind =
  | "search"
  | "read"
  | "attach"
  | "ingest"
  | "compile"
  | "audit"
  | "health";

export interface WikiActionResultState {
  kind: WikiActionKind;
  title: string;
  envelope: WikiEnvelope;
}

interface WikiActionResultProps {
  result: WikiActionResultState;
}

interface LabelValue {
  label: string;
  value: string;
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

function collectPaths(payload: WikiJson, names: string[], label: string): LabelValue[] {
  const paths: LabelValue[] = [];
  for (const name of names) {
    const value = payload[name];
    if (typeof value === "string" && value.trim()) {
      paths.push({ label, value });
    } else if (Array.isArray(value)) {
      value.forEach((item) => {
        const path = typeof item === "string" ? item : fieldText(asRecord(item), names);
        if (path) paths.push({ label, value: path });
      });
    }
  }
  return paths;
}

function citations(payload: WikiJson): LabelValue[] {
  return asArray(payload.citations || payload.results || payload.matches)
    .map((item) => {
      const record = asRecord(item);
      const value = fieldText(record, ["path", "wiki_path", "page_path", "source_path"]);
      if (!value) return null;
      return { label: fieldText(record, ["title", "label"]) || "Citation", value };
    })
    .filter((item): item is LabelValue => item !== null);
}

function sourcePaths(payload: WikiJson): LabelValue[] {
  const direct = collectPaths(
    payload,
    ["source_path", "source_paths", "raw_path", "raw_paths", "changed_paths"],
    "Source",
  );
  const nested = asArray(payload.citations || payload.sources || payload.accepted)
    .map((item) => {
      const record = asRecord(item);
      const value = fieldText(record, ["source_path", "raw_path", "path"]);
      return value ? { label: fieldText(record, ["title", "requested_url", "url"]) || "Source", value } : null;
    })
    .filter((item): item is LabelValue => item !== null);
  return [...direct, ...nested];
}

function wikiPaths(payload: WikiJson): LabelValue[] {
  const direct = collectPaths(
    payload,
    ["wiki_path", "wiki_paths", "page_path", "page_paths", "path", "changed_paths"],
    "Wiki path",
  );
  const nested = asArray(payload.citations || payload.pages || payload.accepted)
    .map((item) => {
      const record = asRecord(item);
      const value = fieldText(record, ["wiki_path", "page_path", "path"]);
      return value ? { label: fieldText(record, ["title", "requested_url", "url"]) || "Wiki path", value } : null;
    })
    .filter((item): item is LabelValue => item !== null);
  return [...direct, ...nested];
}

function degradedMessages(payload: WikiJson): string[] {
  return asArray(payload.degraded_services || payload.degraded || payload.warnings)
    .map((item) => {
      if (typeof item === "string") return item;
      return fieldText(asRecord(item), ["message", "service", "detail", "code"]);
    })
    .filter((item): item is string => Boolean(item));
}

function acceptedEntries(payload: WikiJson): LabelValue[] {
  return asArray(payload.accepted)
    .map((item) => {
      const record = asRecord(item);
      const label = fieldText(record, ["requested_url", "url", "path"]) || "accepted";
      const value = fieldText(record, ["raw_path", "wiki_path", "path"]) || label;
      return { label, value };
    });
}

function failedEntries(payload: WikiJson): LabelValue[] {
  return asArray(payload.failed)
    .map((item) => {
      const record = asRecord(item);
      const label = fieldText(record, ["url", "requested_url", "path"]) || "failed";
      const value = fieldText(record, ["message", "detail", "code"]) || label;
      return { label, value };
    });
}

function contentPreview(payload: WikiJson): string | null {
  return stringValue(payload.content);
}

function ResultSection({ title, items }: { title: string; items: LabelValue[] }) {
  if (!items.length) return null;
  return (
    <section className="space-y-1">
      <div className="text-xs font-medium text-muted-foreground">{title}</div>
      <ul className="space-y-1">
        {items.map((item, index) => (
          <li key={`${title}-${item.label}-${item.value}-${index}`} className="rounded-md bg-muted/30 px-2 py-1 text-xs">
            <span className="font-medium text-foreground">{item.label}</span>
            <span className="ml-2 break-all text-muted-foreground">{item.value}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function TextSection({ title, items }: { title: string; items: string[] }) {
  if (!items.length) return null;
  return (
    <section className="space-y-1">
      <div className="text-xs font-medium text-muted-foreground">{title}</div>
      <ul className="space-y-1">
        {items.map((item, index) => (
          <li key={`${title}-${item}-${index}`} className="break-all rounded-md bg-muted/30 px-2 py-1 text-xs">
            {item}
          </li>
        ))}
      </ul>
    </section>
  );
}

export function WikiActionResult({ result }: WikiActionResultProps) {
  const payload = asRecord(result.envelope.payload);
  const preview = contentPreview(payload);

  return (
    <div className="mt-2 space-y-3 rounded-md border border-border bg-background p-3 text-sm shadow-sm">
      <div className="flex items-center justify-between gap-2">
        <div className="font-medium text-foreground">{result.title}</div>
        <div className="text-xs text-muted-foreground">{result.envelope.ok === false ? "Failed" : "Done"}</div>
      </div>
      {result.envelope.stderr ? (
        <div className="rounded-md bg-muted/30 px-2 py-1 text-xs text-muted-foreground">
          {result.envelope.stderr}
        </div>
      ) : null}
      {preview ? (
        <section className="space-y-1">
          <div className="text-xs font-medium text-muted-foreground">Content</div>
          <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded-md bg-muted/30 p-2 text-xs text-foreground">
            {preview}
          </pre>
        </section>
      ) : null}
      <ResultSection title="Citations" items={citations(payload)} />
      <ResultSection title="Wiki Paths" items={wikiPaths(payload)} />
      <ResultSection title="Source Paths" items={sourcePaths(payload)} />
      <ResultSection title="Accepted" items={acceptedEntries(payload)} />
      <ResultSection title="Failed" items={failedEntries(payload)} />
      <TextSection title="Degraded" items={degradedMessages(payload)} />
    </div>
  );
}
