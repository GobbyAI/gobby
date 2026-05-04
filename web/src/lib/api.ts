export const RESTART_TIMEOUT_MS = 10000;

export async function requestDaemonRestart(): Promise<Response> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), RESTART_TIMEOUT_MS);
  try {
    return await fetch(`${import.meta.env.VITE_API_BASE_URL || ""}/api/admin/restart`, {
      method: "POST",
      credentials: "include",
      signal: controller.signal,
    });
  } finally {
    window.clearTimeout(timeout);
  }
}
