export const RESTART_TIMEOUT_MS = 10000;

export interface ProtectedCronRun {
  run_id: string;
  job_id: string;
  job_name: string;
  started_at: string;
  elapsed_seconds: number;
  remaining_seconds: number;
}

export interface DaemonRestartFailure {
  message: string;
  protectedRuns: ProtectedCronRun[];
}

function isProtectedCronRun(value: unknown): value is ProtectedCronRun {
  if (typeof value !== "object" || value === null) return false;
  const run = value as Record<string, unknown>;
  return (
    typeof run.run_id === "string" &&
    typeof run.job_id === "string" &&
    typeof run.job_name === "string" &&
    typeof run.started_at === "string" &&
    typeof run.elapsed_seconds === "number" &&
    typeof run.remaining_seconds === "number"
  );
}

function formatDuration(totalSeconds: number): string {
  let remaining = Math.max(0, Math.floor(totalSeconds));
  const hours = Math.floor(remaining / 3600);
  remaining %= 3600;
  const minutes = Math.floor(remaining / 60);
  const seconds = remaining % 60;
  const parts: string[] = [];
  if (hours > 0) parts.push(`${hours}h`);
  if (minutes > 0) parts.push(`${minutes}m`);
  if (seconds > 0 || parts.length === 0) parts.push(`${seconds}s`);
  return parts.join(" ");
}

export function describeProtectedCronRuns(runs: ProtectedCronRun[]): string {
  return runs
    .map(
      (run) =>
        `${run.job_name} (running ${formatDuration(run.elapsed_seconds)}, ` +
        `at most ${formatDuration(run.remaining_seconds)} left)`,
    )
    .join(", ");
}

export async function readDaemonRestartFailure(
  response: Response,
): Promise<DaemonRestartFailure | null> {
  if (response.ok) return null;

  let payload: Record<string, unknown> | null = null;
  try {
    const value: unknown = await response.json();
    if (typeof value === "object" && value !== null) {
      payload = value as Record<string, unknown>;
    }
  } catch {
    // A non-JSON failure still has an actionable HTTP status below.
  }

  const rawRuns = payload?.protected_runs;
  const protectedRuns = Array.isArray(rawRuns)
    ? rawRuns.filter(isProtectedCronRun)
    : [];
  const baseMessage =
    typeof payload?.message === "string"
      ? payload.message
      : `Restart failed: ${response.status}`;
  const details = describeProtectedCronRuns(protectedRuns);

  return {
    message: details ? `${baseMessage}: ${details}` : baseMessage,
    protectedRuns,
  };
}

export async function requestDaemonRestart(force = false): Promise<Response> {
  const controller = new AbortController();
  const timeout = window.setTimeout(
    () => controller.abort(),
    RESTART_TIMEOUT_MS,
  );
  try {
    return await fetch(
      `${import.meta.env.VITE_API_BASE_URL || ""}/api/admin/restart${force ? "?force=true" : ""}`,
      {
        method: "POST",
        credentials: "include",
        signal: controller.signal,
      },
    );
  } catch (error) {
    if (
      (error instanceof DOMException && error.name === "AbortError") ||
      error instanceof TypeError
    ) {
      return new Response(null, {
        status: 202,
        statusText: "Accepted (daemon restarting)",
      });
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}
