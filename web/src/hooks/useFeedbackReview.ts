import { useEffect, useState } from "react";

export interface FeedbackSchedule {
  enabled: boolean;
  cron_expr: string;
  timezone: string;
  next_run_at: string | null;
  last_status: string | null;
}

export interface FeedbackStatus {
  backlog: number;
  schedule: FeedbackSchedule | null;
}

export interface FeedbackFiledTask {
  task_ref: string;
  title: string;
}

export interface FeedbackRun {
  id: string;
  status: string;
  dry_run: boolean;
  rows_considered: number;
  actions: { filed?: FeedbackFiledTask[]; deduplicated?: number } | null;
  digest_md: string | null;
  error: string | null;
  created_at: string;
}

const RUN_LIMIT = 20;

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const detail =
      body && typeof body === "object" && "detail" in body
        ? String(body.detail)
        : null;
    throw new Error(detail ?? `Request failed (${res.status})`);
  }
  return body as T;
}

/** Backlog, schedule and recent review runs for the session-feedback loop. */
export function useFeedbackReview() {
  const [status, setStatus] = useState<FeedbackStatus | null>(null);
  const [runs, setRuns] = useState<FeedbackRun[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      getJson<FeedbackStatus>("/api/feedback/status"),
      getJson<{ runs: FeedbackRun[] }>(`/api/feedback/runs?limit=${RUN_LIMIT}`),
    ])
      .then(([nextStatus, runPage]) => {
        if (cancelled) return;
        setStatus(nextStatus);
        setRuns(runPage.runs ?? []);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(
          e instanceof Error ? e.message : "Failed to load feedback review",
        );
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return { status, runs, isLoading, error };
}
