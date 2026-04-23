export const CONVERSATION_ID_STORAGE_KEY = "gobby-conversation-id";
export const DB_SESSION_ID_STORAGE_KEY = "gobby-db-session-id";
export const REASONING_PREFERENCES_STORAGE_KEY = "gobby-reasoning-preferences";

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

export function loadReasoningPreferences(): Record<string, string> {
  try {
    const raw = localStorage.getItem(REASONING_PREFERENCES_STORAGE_KEY);
    if (!raw) {
      return {};
    }
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? (parsed as Record<string, string>) : {};
  } catch {
    return {};
  }
}
