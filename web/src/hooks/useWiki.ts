import { useCallback, useEffect, useMemo, useState } from "react";

export type WikiJson = Record<string, unknown>;

export interface WikiEnvelope<TPayload = WikiJson> {
  ok?: boolean;
  command?: string;
  payload?: TPayload;
  stderr?: string;
  index_handoff?: unknown;
}

export interface WikiSourceRecord {
  id: string;
  title?: string;
  path?: string;
  raw_path?: string;
  wiki_path?: string;
  page_path?: string;
  url?: string;
  source_url?: string;
  page_url?: string;
  [key: string]: unknown;
}

export interface WikiRemoveSourceRequest {
  id: string;
  dry_run?: boolean;
  yes?: boolean;
  keep_asset?: boolean;
}

interface UseWikiOptions {
  projectId?: string | null;
  topic?: string | null;
}

function getBaseUrl(): string {
  return "";
}

function scopeQuery({ projectId, topic }: UseWikiOptions): string {
  const params = new URLSearchParams();
  if (projectId) params.set("project", projectId);
  if (topic) params.set("topic", topic);
  const query = params.toString();
  return query ? `?${query}` : "";
}

async function readWikiEnvelope<TPayload = WikiJson>(
  path: string,
): Promise<WikiEnvelope<TPayload>> {
  const response = await fetch(`${getBaseUrl()}${path}`);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(typeof data.detail === "string" ? data.detail : `HTTP ${response.status}`);
  }
  return data;
}

async function postWikiEnvelope<TPayload = WikiJson>(
  path: string,
  body: WikiRemoveSourceRequest,
): Promise<WikiEnvelope<TPayload>> {
  const response = await fetch(`${getBaseUrl()}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail;
    if (detail && typeof detail === "object" && "stderr" in detail) {
      throw new Error(String((detail as { stderr?: unknown }).stderr));
    }
    throw new Error(typeof detail === "string" ? detail : `HTTP ${response.status}`);
  }
  return data;
}

function sourceRecordsFromEnvelope(envelope: WikiEnvelope): WikiSourceRecord[] {
  const sources = envelope.payload?.sources;
  if (!Array.isArray(sources)) return [];
  return sources.filter(
    (source): source is WikiSourceRecord =>
      typeof source === "object" &&
      source !== null &&
      typeof (source as { id?: unknown }).id === "string",
  );
}

export function useWiki(options: UseWikiOptions = {}) {
  const { projectId = null, topic = null } = options;
  const query = useMemo(() => scopeQuery({ projectId, topic }), [projectId, topic]);
  const [status, setStatus] = useState<WikiEnvelope | null>(null);
  const [health, setHealth] = useState<WikiEnvelope | null>(null);
  const [sourcesEnvelope, setSourcesEnvelope] = useState<WikiEnvelope | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const [nextStatus, nextHealth, nextSources] = await Promise.all([
        readWikiEnvelope(`/api/wiki/status${query}`),
        readWikiEnvelope(`/api/wiki/health${query}`),
        readWikiEnvelope(`/api/wiki/sources${query}`),
      ]);
      setStatus(nextStatus);
      setHealth(nextHealth);
      setSourcesEnvelope(nextSources);
    } catch (nextError) {
      setError(String(nextError));
    } finally {
      setIsLoading(false);
    }
  }, [query]);

  const removeSource = useCallback(
    (request: WikiRemoveSourceRequest) =>
      postWikiEnvelope(`/api/wiki/remove-source${query}`, request),
    [query],
  );

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void refresh();
    }, 0);
    return () => window.clearTimeout(timer);
  }, [refresh]);

  return {
    status,
    health,
    sources: sourceRecordsFromEnvelope(sourcesEnvelope ?? {}),
    sourcesEnvelope,
    isLoading,
    error,
    refresh,
    removeSource,
  };
}
