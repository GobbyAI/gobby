import { useState, useEffect, useCallback, useMemo, useRef } from "react";

import type { GobbySession } from "../types/sessions";
import {
  serializeSessionsFilters,
  type SessionsFilters,
} from "../components/activity/sessionsFilters";
import { useWebSocketEvent } from "./useWebSocketEvent";

const REFETCH_DEBOUNCE_MS = 500;
const FILTER_REFETCH_DEBOUNCE_MS = 250;
const PAGE_SIZE = 100;

interface SessionCursor {
  updated_at: string;
  id: string;
}

function getBaseUrl(): string {
  return "";
}

function buildSessionsUrl(
  baseUrl: string,
  projectId: string,
  filters: SessionsFilters | null,
  cursor: SessionCursor | null,
  now: Date,
): string {
  const params = filters
    ? serializeSessionsFilters(filters, now)
    : new URLSearchParams();
  params.set("project_id", projectId);
  params.set("limit", String(PAGE_SIZE));
  if (cursor) {
    params.set("cursor_updated_at", cursor.updated_at);
    params.set("cursor_id", cursor.id);
  }
  return `${baseUrl}/api/sessions?${params}`;
}

interface FetchPageResult {
  sessions: GobbySession[];
  next_cursor: SessionCursor | null;
}

async function fetchSessionPage(
  projectId: string,
  filters: SessionsFilters | null,
  cursor: SessionCursor | null,
): Promise<FetchPageResult> {
  const baseUrl = getBaseUrl();
  const url = buildSessionsUrl(baseUrl, projectId, filters, cursor, new Date());
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`Failed to fetch sessions: ${response.status}`);
  }
  const data = await response.json();
  const sessions: GobbySession[] = Array.isArray(data.sessions)
    ? data.sessions.filter((session: GobbySession) => session.status !== "deleted")
    : [];
  const next: SessionCursor | null =
    data.next_cursor && typeof data.next_cursor === "object"
      ? {
          updated_at: String(data.next_cursor.updated_at),
          id: String(data.next_cursor.id),
        }
      : null;
  return { sessions, next_cursor: next };
}

export function useSessionCatalog(
  projectId: string | null,
  filters: SessionsFilters | null = null,
) {
  const [sessions, setSessions] = useState<GobbySession[]>([]);
  const [deletingIds, setDeletingIds] = useState<Set<string>>(new Set());
  const [isLoading, setIsLoading] = useState(true);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const [cursor, setCursor] = useState<SessionCursor | null>(null);
  const debouncedRefetchRef = useRef<number | null>(null);
  const filterDebounceRef = useRef<number | null>(null);

  // Latest filters + projectId in refs so callbacks captured by websocket
  // listeners always see current values without requiring re-subscription.
  const filtersRef = useRef<SessionsFilters | null>(filters);
  const projectIdRef = useRef<string | null>(projectId);
  useEffect(() => {
    filtersRef.current = filters;
  }, [filters]);
  useEffect(() => {
    projectIdRef.current = projectId;
  }, [projectId]);

  useEffect(() => {
    return () => {
      if (debouncedRefetchRef.current) {
        window.clearTimeout(debouncedRefetchRef.current);
      }
      if (filterDebounceRef.current) {
        window.clearTimeout(filterDebounceRef.current);
      }
    };
  }, []);

  // Reset and fetch page 1. Used on initial load, project change, and
  // filter change. Discards any older pages — when the filter window shifts,
  // older pages no longer match and would create stale results.
  const resetAndFetch = useCallback(async () => {
    const currentProjectId = projectIdRef.current;
    if (!currentProjectId) {
      setSessions([]);
      setCursor(null);
      setError(null);
      setIsLoading(false);
      return;
    }
    setIsLoading(true);
    setError(null);
    try {
      const result = await fetchSessionPage(currentProjectId, filtersRef.current, null);
      setSessions(result.sessions);
      setCursor(result.next_cursor);
    } catch (e) {
      console.error("Failed to fetch sessions:", e);
      setError(e instanceof Error ? e : new Error(String(e)));
    } finally {
      setIsLoading(false);
    }
  }, []);

  // Re-fetch page 1 only and merge by id. Keeps any older loaded pages in
  // place (their rows are replaced if they show up on page 1, otherwise
  // preserved). Used for websocket-driven refresh and the public refresh()
  // method — we don't want a websocket tick to nuke the user's scroll
  // history.
  const refreshPageOne = useCallback(async () => {
    const currentProjectId = projectIdRef.current;
    if (!currentProjectId) return;
    try {
      const result = await fetchSessionPage(currentProjectId, filtersRef.current, null);
      setSessions((prev) => {
        const merged = new Map<string, GobbySession>();
        for (const s of prev) merged.set(s.id, s);
        for (const s of result.sessions) merged.set(s.id, s);
        return Array.from(merged.values());
      });
      // next_cursor only resets if page 1 already covers everything we had.
      // Otherwise we keep our deeper cursor so loadMore continues from there.
      setCursor((prev) => {
        if (result.next_cursor === null) return prev;
        return prev ?? result.next_cursor;
      });
    } catch (e) {
      console.error("Failed to refresh sessions page 1:", e);
    }
  }, []);

  // Initial load + project changes
  const previousProjectIdRef = useRef(projectId);
  useEffect(() => {
    if (projectId !== previousProjectIdRef.current) {
      previousProjectIdRef.current = projectId;
      setSessions([]);
      setCursor(null);
    }
    void resetAndFetch();
  }, [projectId, resetAndFetch]);

  // Filter changes — debounced reset + refetch. Skip the very first render
  // (initial load is owned by the project effect above).
  const initialFilterMountRef = useRef(true);
  useEffect(() => {
    if (initialFilterMountRef.current) {
      initialFilterMountRef.current = false;
      return;
    }
    if (filterDebounceRef.current) {
      window.clearTimeout(filterDebounceRef.current);
    }
    filterDebounceRef.current = window.setTimeout(() => {
      void resetAndFetch();
    }, FILTER_REFETCH_DEBOUNCE_MS);
  }, [filters, resetAndFetch]);

  useWebSocketEvent(
    "session_event",
    useCallback((data: Record<string, unknown>) => {
      // Patch the local catalog optimistically for status-changing events.
      // refreshPageOne fetches with the active filter (e.g. Live = active+paused),
      // so an expired/deleted session isn't returned and the merge preserves
      // its stale prev row — Live view keeps showing the row with status:active
      // until the user reloads. Patch in place so client-side filters drop it.
      const event = typeof data.event === "string" ? data.event : null;
      const sessionId = typeof data.session_id === "string" ? data.session_id : null;
      if (sessionId) {
        if (event === "session_expired") {
          setSessions((prev) =>
            prev.map((session) =>
              session.id === sessionId
                ? { ...session, status: "expired" }
                : session,
            ),
          );
        } else if (event === "session_deleted") {
          setSessions((prev) => prev.filter((session) => session.id !== sessionId));
        }
      }
      if (debouncedRefetchRef.current) {
        window.clearTimeout(debouncedRefetchRef.current);
      }
      debouncedRefetchRef.current = window.setTimeout(
        () => void refreshPageOne(),
        REFETCH_DEBOUNCE_MS,
      );
    }, [refreshPageOne]),
  );

  useWebSocketEvent(
    "session_usage_updated",
    useCallback((data: Record<string, unknown>) => {
      const sessionId = typeof data.session_id === "string" ? data.session_id : null;
      if (!sessionId) {
        return;
      }
      setSessions((prev) =>
        prev.map((session) =>
          session.id === sessionId
            ? {
                ...session,
                usage_input_tokens:
                  typeof data.usage_input_tokens === "number"
                    ? data.usage_input_tokens
                    : session.usage_input_tokens,
                usage_output_tokens:
                  typeof data.usage_output_tokens === "number"
                    ? data.usage_output_tokens
                    : session.usage_output_tokens,
                usage_cache_creation_tokens:
                  typeof data.usage_cache_creation_tokens === "number"
                    ? data.usage_cache_creation_tokens
                    : session.usage_cache_creation_tokens,
                usage_cache_read_tokens:
                  typeof data.usage_cache_read_tokens === "number"
                    ? data.usage_cache_read_tokens
                    : session.usage_cache_read_tokens,
                context_window:
                  typeof data.context_window === "number"
                    ? data.context_window
                    : session.context_window,
                model:
                  typeof data.model === "string" ? data.model : session.model,
              }
            : session,
        ),
      );
    }, []),
  );

  // Sort by seq_num DESC for stability across token-usage updates. The
  // backend's processor.py touches updated_at every ~2s during agent runs,
  // which made the previous updated_at sort visibly leapfrog rows. seq_num
  // is monotonic at session creation and never changes. Falls back to
  // created_at when seq_num is null (older daemons that don't emit it).
  const sortedSessions = useMemo(
    () =>
      [...sessions].sort((a, b) => {
        const aSeq = a.seq_num ?? -Infinity;
        const bSeq = b.seq_num ?? -Infinity;
        if (aSeq !== bSeq) return bSeq - aSeq;
        return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
      }),
    [sessions],
  );

  const refresh = useCallback(() => {
    void refreshPageOne();
  }, [refreshPageOne]);

  const loadMore = useCallback(async () => {
    if (!cursor || isLoadingMore) return;
    const currentProjectId = projectIdRef.current;
    if (!currentProjectId) return;
    setIsLoadingMore(true);
    try {
      const result = await fetchSessionPage(currentProjectId, filtersRef.current, cursor);
      setSessions((prev) => {
        // Append, but de-dupe by id in case a session moved between pages
        // due to an updated_at change between page-1 fetch and this fetch.
        const seen = new Set(prev.map((s) => s.id));
        return [...prev, ...result.sessions.filter((s) => !seen.has(s.id))];
      });
      setCursor(result.next_cursor);
    } catch (e) {
      console.error("Failed to load more sessions:", e);
      setError(e instanceof Error ? e : new Error(String(e)));
    } finally {
      setIsLoadingMore(false);
    }
  }, [cursor, isLoadingMore]);

  const removeSessionById = useCallback((id: string) => {
    setSessions((prev) => prev.filter((session) => session.id !== id));
    setDeletingIds((prev) => {
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
  }, []);

  const removeSession = useCallback(
    (id: string) => {
      removeSessionById(id);
    },
    [removeSessionById],
  );

  const markSessionDeleting = useCallback((id: string) => {
    setDeletingIds((prev) => new Set(prev).add(id));
  }, []);

  const confirmSessionDeleted = useCallback(
    (sessionId: string) => {
      removeSessionById(sessionId);
    },
    [removeSessionById],
  );

  const restoreSession = useCallback((id: string) => {
    setDeletingIds((prev) => {
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
  }, []);

  const renameSession = useCallback(
    async (id: string, title: string) => {
      const previousSession = sessions.find((s) => s.id === id);
      setSessions((prev) =>
        prev.map((session) =>
          session.id === id ? { ...session, title } : session,
        ),
      );

      const restorePreviousTitle = () => {
        if (!previousSession) {
          return;
        }
        const previousTitle = previousSession.title;
        setSessions((prev) =>
          prev.map((session) =>
            session.id === id ? { ...session, title: previousTitle } : session,
          ),
        );
      };

      try {
        const baseUrl = getBaseUrl();
        const response = await fetch(`${baseUrl}/api/sessions/${id}/rename`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title }),
        });
        if (!response.ok) {
          console.error(`Rename failed: ${response.status}`);
          restorePreviousTitle();
          await refreshPageOne();
        }
      } catch (e) {
        console.error("Failed to rename session:", e);
        restorePreviousTitle();
        await refreshPageOne();
      }
    },
    [refreshPageOne, sessions],
  );

  return {
    sessions: sortedSessions,
    isLoading,
    isLoadingMore,
    error,
    refresh,
    loadMore,
    hasMore: cursor !== null,
    removeSession,
    markSessionDeleting,
    confirmSessionDeleted,
    restoreSession,
    deletingIds,
    renameSession,
  };
}
