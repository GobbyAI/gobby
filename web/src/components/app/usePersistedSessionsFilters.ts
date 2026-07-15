import { useEffect, useRef, useState } from "react";

import {
  defaultSessionsFilters,
  deserializeFromStorage,
  serializeForStorage,
  type SessionsFilters,
} from "../activity/sessionsFilters";

const SESSIONS_FILTERS_STORAGE_KEY = "gobby-sessions-filters";

function storageKey(projectId: string | null): string {
  return `${SESSIONS_FILTERS_STORAGE_KEY}:${projectId ?? "all"}`;
}

function loadSessionsFilters(projectId: string | null): SessionsFilters {
  try {
    return deserializeFromStorage(localStorage.getItem(storageKey(projectId)));
  } catch {
    return defaultSessionsFilters();
  }
}

export function usePersistedSessionsFilters(projectId: string | null) {
  const [sessionsFilters, setSessionsFilters] = useState<SessionsFilters>(
    () => loadSessionsFilters(projectId),
  );
  const activeProjectIdRef = useRef(projectId);
  const skipNextPersistenceRef = useRef(false);

  useEffect(() => {
    if (activeProjectIdRef.current === projectId) return;
    activeProjectIdRef.current = projectId;
    skipNextPersistenceRef.current = true;
    setSessionsFilters(loadSessionsFilters(projectId));
  }, [projectId]);

  // Best-effort persistence: disabled storage just means filters are per load.
  useEffect(() => {
    if (skipNextPersistenceRef.current) {
      skipNextPersistenceRef.current = false;
      return;
    }
    try {
      localStorage.setItem(
        storageKey(projectId),
        JSON.stringify(serializeForStorage(sessionsFilters)),
      );
    } catch {
      // Ignore storage failures.
    }
  }, [projectId, sessionsFilters]);

  return { sessionsFilters, setSessionsFilters };
}
