export const CONVERSATION_ID_STORAGE_KEY = "gobby-conversation-id";
export const DB_SESSION_ID_STORAGE_KEY = "gobby-db-session-id";
export const FRESH_CHAT_DRAFT_STORAGE_KEY = "gobby-fresh-chat-draft";
export const REASONING_PREFERENCES_STORAGE_KEY = "gobby-reasoning-preferences";
export const VIEWING_SESSION_ID_STORAGE_KEY = "gobby-viewing-session-id";
export const VIEWING_SESSION_MODE_STORAGE_KEY = "gobby-viewing-session-mode";

export type PersistedViewingSessionMode = "none" | "observe" | "proxy";

export function loadPersistedConversationId(): string | null {
  try {
    return (
      localStorage.getItem(DB_SESSION_ID_STORAGE_KEY) ||
      localStorage.getItem(CONVERSATION_ID_STORAGE_KEY)
    );
  } catch {
    return null;
  }
}

export function loadPersistedDbSessionId(): string | null {
  try {
    return localStorage.getItem(DB_SESSION_ID_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function hasFreshChatDraft(): boolean {
  try {
    return localStorage.getItem(FRESH_CHAT_DRAFT_STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

export function markFreshChatDraft(): void {
  try {
    localStorage.setItem(FRESH_CHAT_DRAFT_STORAGE_KEY, "1");
  } catch {
    /* ignore */
  }
}

export function clearFreshChatDraft(): void {
  try {
    localStorage.removeItem(FRESH_CHAT_DRAFT_STORAGE_KEY);
  } catch {
    /* ignore */
  }
}

export function loadPersistedViewingSessionId(): string | null {
  try {
    return localStorage.getItem(VIEWING_SESSION_ID_STORAGE_KEY);
  } catch {
    return null;
  }
}

export function savePersistedViewingSessionId(id: string | null): void {
  try {
    if (id) {
      localStorage.setItem(VIEWING_SESSION_ID_STORAGE_KEY, id);
    } else {
      localStorage.removeItem(VIEWING_SESSION_ID_STORAGE_KEY);
    }
  } catch {
    /* ignore */
  }
}

export function loadPersistedViewingSessionMode(): PersistedViewingSessionMode {
  try {
    const persisted = localStorage.getItem(VIEWING_SESSION_MODE_STORAGE_KEY);
    return persisted === "observe" || persisted === "proxy"
      ? persisted
      : "none";
  } catch {
    return "none";
  }
}

export function savePersistedViewingSessionMode(
  mode: PersistedViewingSessionMode,
): void {
  try {
    if (mode === "observe" || mode === "proxy") {
      localStorage.setItem(VIEWING_SESSION_MODE_STORAGE_KEY, mode);
    } else {
      localStorage.removeItem(VIEWING_SESSION_MODE_STORAGE_KEY);
    }
  } catch {
    /* ignore */
  }
}

export function loadReasoningPreferences(): Record<string, string> {
  try {
    const raw = localStorage.getItem(REASONING_PREFERENCES_STORAGE_KEY);
    if (!raw) {
      return {};
    }
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return {};
    }
    return Object.fromEntries(
      Object.entries(parsed).filter((entry): entry is [string, string] => {
        return typeof entry[1] === "string";
      }),
    );
  } catch {
    return {};
  }
}
