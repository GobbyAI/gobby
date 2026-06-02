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

export interface WikiSearchRequest {
  query: string;
  limit?: number;
}

export interface WikiReadRequest {
  path?: string;
  title?: string;
}

export interface WikiIngestRequest {
  path?: string;
  paths?: string[];
  urls?: string[];
}

export interface WikiCompileRequest {
  output?: string;
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
  body?: unknown,
): Promise<WikiEnvelope<TPayload>> {
  const isFormData = typeof FormData !== "undefined" && body instanceof FormData;
  const response = await fetch(`${getBaseUrl()}${path}`, {
    method: "POST",
    headers: isFormData ? undefined : { "Content-Type": "application/json" },
    body: isFormData ? body : JSON.stringify(body ?? {}),
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

  const withQuery = useCallback(
    (path: string, params: Record<string, string | number | null | undefined> = {}) => {
      const nextParams = new URLSearchParams(query.startsWith("?") ? query.slice(1) : query);
      for (const [key, value] of Object.entries(params)) {
        if (value !== null && value !== undefined && String(value).trim()) {
          nextParams.set(key, String(value));
        }
      }
      const nextQuery = nextParams.toString();
      return nextQuery ? `${path}?${nextQuery}` : path;
    },
    [query],
  );

  const search = useCallback(
    (request: WikiSearchRequest) =>
      readWikiEnvelope(withQuery("/api/wiki/search", {
        query: request.query,
        limit: request.limit,
      })),
    [withQuery],
  );

  const read = useCallback(
    (request: WikiReadRequest) =>
      readWikiEnvelope(withQuery("/api/wiki/read", {
        path: request.path,
        title: request.title,
      })),
    [withQuery],
  );

  const attach = useCallback(
    (file: File) => {
      const body = new FormData();
      body.set("file", file);
      return postWikiEnvelope(withQuery("/api/wiki/attach"), body);
    },
    [withQuery],
  );

  const ingest = useCallback(
    (request: WikiIngestRequest) => postWikiEnvelope(withQuery("/api/wiki/ingest"), request),
    [withQuery],
  );

  const compileWiki = useCallback(
    (request: WikiCompileRequest = {}) => postWikiEnvelope(withQuery("/api/wiki/compile"), request),
    [withQuery],
  );

  const audit = useCallback(
    () => postWikiEnvelope(withQuery("/api/wiki/audit")),
    [withQuery],
  );

  const checkHealth = useCallback(
    () => readWikiEnvelope(withQuery("/api/wiki/health")),
    [withQuery],
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
    search,
    read,
    attach,
    ingest,
    compileWiki,
    audit,
    checkHealth,
    removeSource,
  };
}
