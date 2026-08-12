export const CRON_RUN_SUCCESS_STATUSES = new Set(["success", "completed"]);
export const CRON_RUN_FAILURE_STATUSES = new Set(["failed", "error"]);
export const CRON_RUN_ACTIVE_STATUSES = new Set(["running"]);
export const CRON_RUN_DISPATCHED_STATUSES = new Set(["dispatched"]);
export const CRON_RUN_SKIPPED_STATUSES = new Set(["skipped"]);

export type CronRunStatusKind =
  "success" | "failure" | "running" | "dispatched" | "skipped" | "pending";

export function cronRunStatusKind(status: string): CronRunStatusKind {
  if (CRON_RUN_SUCCESS_STATUSES.has(status)) return "success";
  if (CRON_RUN_FAILURE_STATUSES.has(status)) return "failure";
  if (CRON_RUN_ACTIVE_STATUSES.has(status)) return "running";
  if (CRON_RUN_DISPATCHED_STATUSES.has(status)) return "dispatched";
  if (CRON_RUN_SKIPPED_STATUSES.has(status)) return "skipped";
  return "pending";
}
