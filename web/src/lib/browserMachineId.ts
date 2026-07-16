const STORAGE_KEY = "gobby-browser-machine-id";

let volatileMachineId: string | null = null;

function createMachineId(): string {
  if (
    typeof globalThis.crypto !== "undefined" &&
    typeof globalThis.crypto.randomUUID === "function"
  ) {
    return `web:${globalThis.crypto.randomUUID()}`;
  }

  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `web:${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function getBrowserMachineId(): string {
  try {
    const stored = globalThis.localStorage.getItem(STORAGE_KEY)?.trim();
    if (stored) {
      volatileMachineId = stored;
      return stored;
    }
  } catch {
    // Storage can be unavailable in privacy-restricted browser contexts.
  }

  const machineId = volatileMachineId ?? createMachineId();
  volatileMachineId = machineId;
  try {
    globalThis.localStorage.setItem(STORAGE_KEY, machineId);
  } catch {
    // The module-scoped value keeps identity stable for this page lifetime.
  }
  return machineId;
}
