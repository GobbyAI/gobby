import { useEffect, useState } from "react";

import {
  defaultSessionsFilters,
  deserializeFromStorage,
  serializeForStorage,
  type SessionsFilters,
} from "../activity/sessionsFilters";

const SESSIONS_FILTERS_STORAGE_KEY = "gobby-sessions-filters";

export function usePersistedSessionsFilters() {
  const [sessionsFilters, setSessionsFilters] = useState<SessionsFilters>(
    () => {
      try {
        return deserializeFromStorage(
          localStorage.getItem(SESSIONS_FILTERS_STORAGE_KEY),
        );
      } catch {
        return defaultSessionsFilters();
      }
    },
  );

  // Best-effort persistence: disabled storage just means filters are per load.
  useEffect(() => {
    try {
      localStorage.setItem(
        SESSIONS_FILTERS_STORAGE_KEY,
        JSON.stringify(serializeForStorage(sessionsFilters)),
      );
    } catch {
      // Ignore storage failures.
    }
  }, [sessionsFilters]);

  return { sessionsFilters, setSessionsFilters };
}
