import { useState, useEffect, useCallback, useMemo, useRef } from "react";

import type { GobbySession } from "../types/sessions";
import { useWebSocketEvent } from "./useWebSocketEvent";

const REFETCH_DEBOUNCE_MS = 500;

function getBaseUrl(): string {
  return "";
}

export function useSessionCatalog(projectId: string | null) {
  const [sessions, setSessions] = useState<GobbySession[]>([]);
  const [deletingIds, setDeletingIds] = useState<Set<string>>(new Set());
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const debouncedRefetchRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (debouncedRefetchRef.current) {
        window.clearTimeout(debouncedRefetchRef.current);
      }
    };
  }, []);

  const fetchSessions = useCallback(async () => {
    if (!projectId) {
      setSessions([]);
      setError(null);
      setIsLoading(false);
      return;
    }

    setIsLoading(true);
    setError(null);
    try {
      const baseUrl = getBaseUrl();
      const params = new URLSearchParams({ limit: "200" });
      params.set("project_id", projectId);

      const response = await fetch(`${baseUrl}/api/sessions?${params}`);
      if (!response.ok) {
        throw new Error(`Failed to fetch sessions: ${response.status}`);
      }

      const data = await response.json();
      const fetched: GobbySession[] = Array.isArray(data.sessions)
        ? data.sessions
        : [];
      setSessions(fetched.filter((session) => session.status !== "deleted"));
    } catch (e) {
      console.error("Failed to fetch sessions:", e);
      setError(e instanceof Error ? e : new Error(String(e)));
    } finally {
      setIsLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    const timeoutId = window.setTimeout(() => {
      void fetchSessions();
    }, 0);
    return () => window.clearTimeout(timeoutId);
  }, [fetchSessions]);

  const previousProjectIdRef = useRef(projectId);
  useEffect(() => {
    if (projectId !== previousProjectIdRef.current) {
      previousProjectIdRef.current = projectId;
      setSessions([]);
    }
  }, [projectId]);

  useWebSocketEvent(
    "session_event",
    useCallback(() => {
      if (debouncedRefetchRef.current) {
        window.clearTimeout(debouncedRefetchRef.current);
      }
      debouncedRefetchRef.current = window.setTimeout(
        () => void fetchSessions(),
        REFETCH_DEBOUNCE_MS,
      );
    }, [fetchSessions]),
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

  const sortedSessions = useMemo(
    () =>
      [...sessions].sort(
        (a, b) =>
          new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
      ),
    [sessions],
  );

  const refresh = useCallback(() => {
    setIsLoading(true);
    void fetchSessions();
  }, [fetchSessions]);

  const removeSession = useCallback((id: string) => {
    setSessions((prev) => prev.filter((session) => session.id !== id));
    setDeletingIds((prev) => {
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
  }, []);

  const markSessionDeleting = useCallback((id: string) => {
    setDeletingIds((prev) => new Set(prev).add(id));
  }, []);

  const confirmSessionDeleted = useCallback((sessionId: string) => {
    setSessions((prev) => prev.filter((session) => session.id !== sessionId));
    setDeletingIds((prev) => {
      const next = new Set(prev);
      next.delete(sessionId);
      return next;
    });
  }, []);

  const restoreSession = useCallback((id: string) => {
    setDeletingIds((prev) => {
      const next = new Set(prev);
      next.delete(id);
      return next;
    });
  }, []);

  const renameSession = useCallback(
    async (id: string, title: string) => {
      const previousSession = sessions.find((session) => session.id === id);
      setSessions((prev) =>
        prev.map((session) => {
          if (session.id !== id) {
            return session;
          }
          return { ...session, title };
        }),
      );

      const restorePreviousTitle = () => {
        if (!previousSession) {
          return;
        }
        setSessions((prev) =>
          prev.map((session) =>
            session.id === id ? { ...session, title: previousSession.title } : session,
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
          await fetchSessions();
        }
      } catch (e) {
        console.error("Failed to rename session:", e);
        restorePreviousTitle();
        await fetchSessions();
      }
    },
    [fetchSessions, sessions],
  );

  return {
    sessions: sortedSessions,
    isLoading,
    error,
    refresh,
    removeSession,
    markSessionDeleting,
    confirmSessionDeleted,
    restoreSession,
    deletingIds,
    renameSession,
  };
}
