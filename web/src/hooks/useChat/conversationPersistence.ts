import type { SessionInteractionMode } from "../../types/chat";
import {
  loadPersistedViewingSessionId,
  loadPersistedViewingSessionMode,
  savePersistedViewingSessionId,
  savePersistedViewingSessionMode,
} from "../../lib/sessionPersistence";

const CONVERSATION_ID_KEY = "gobby-conversation-id";
const DB_SESSION_ID_KEY = "gobby-db-session-id";

function readLocalStorage(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeLocalStorage(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Best-effort persistence only.
  }
}

function removeLocalStorage(key: string): void {
  try {
    localStorage.removeItem(key);
  } catch {
    // Best-effort persistence only.
  }
}

/** crypto.randomUUID() requires a secure context (HTTPS/localhost). Fall back for HTTP access (e.g. Tailscale IP). */
export function uuid(): string {
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
  ) {
    try {
      return crypto.randomUUID();
    } catch {
      /* non-secure context */
    }
  }
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join(
    "",
  );
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function loadConversationId(): string {
  return readLocalStorage(CONVERSATION_ID_KEY) || "";
}

export function saveConversationId(id: string): void {
  if (!id) {
    removeLocalStorage(CONVERSATION_ID_KEY);
    return;
  }
  writeLocalStorage(CONVERSATION_ID_KEY, id);
}

export function loadDbSessionId(): string | null {
  return readLocalStorage(DB_SESSION_ID_KEY);
}

export function saveDbSessionId(id: string | null): void {
  if (id) {
    writeLocalStorage(DB_SESSION_ID_KEY, id);
  } else {
    removeLocalStorage(DB_SESSION_ID_KEY);
  }
}

export function loadViewingSessionId(): string | null {
  return loadPersistedViewingSessionId();
}

export function saveViewingSessionId(id: string | null): void {
  savePersistedViewingSessionId(id);
}

export function loadViewingSessionMode(): SessionInteractionMode {
  return loadPersistedViewingSessionMode();
}

export function saveViewingSessionMode(mode: SessionInteractionMode): void {
  savePersistedViewingSessionMode(mode);
}
