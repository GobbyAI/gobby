import type { PipelineExecutionRecord } from "../../hooks/usePipelineExecutions";
import type { AgentRunRecord } from "../../hooks/useAgentRuns";

export type SubTab = "pipelines" | "agents";
export type StatusFilter =
  | "all"
  | "running"
  | "waiting"
  | "completed"
  | "failed";
export type PipelineSortColumn = "name" | "time" | "duration" | "status";
export type AgentSortColumn =
  | "name"
  | "provider"
  | "time"
  | "duration"
  | "turns"
  | "status";
export type SortDirection = "asc" | "desc";
export type GroupBy = "none" | "name" | "provider";

export const STATUS_OPTIONS: { value: StatusFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "running", label: "Running" },
  { value: "waiting", label: "Waiting" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
];

export function statusMatchesFilter(
  status: string,
  filter: StatusFilter,
): boolean {
  if (filter === "all") return true;
  if (filter === "running") return status === "running" || status === "pending";
  if (filter === "waiting") return status === "waiting_approval";
  if (filter === "completed")
    return status === "completed" || status === "success";
  if (filter === "failed")
    return (
      status === "failed" ||
      status === "error" ||
      status === "timeout" ||
      status === "cancelled" ||
      status === "interrupted"
    );
  return true;
}

export function normalizeStatus(status: string): string {
  return status.replace(/_/g, " ");
}

export function formatDateTime(iso: string): string {
  const d = new Date(iso);
  return (
    d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) +
    " " +
    d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
  );
}

export function comparePipelines(
  a: PipelineExecutionRecord,
  b: PipelineExecutionRecord,
  col: PipelineSortColumn,
  dir: SortDirection,
): number {
  let cmp = 0;
  switch (col) {
    case "name":
      cmp = a.pipeline_name.localeCompare(b.pipeline_name);
      break;
    case "time":
      cmp = a.created_at.localeCompare(b.created_at);
      break;
    case "duration": {
      const da = a.completed_at
        ? new Date(a.completed_at).getTime() - new Date(a.created_at).getTime()
        : 0;
      const db = b.completed_at
        ? new Date(b.completed_at).getTime() - new Date(b.created_at).getTime()
        : 0;
      cmp = da - db;
      break;
    }
    case "status":
      cmp = a.status.localeCompare(b.status);
      break;
  }
  return dir === "asc" ? cmp : -cmp;
}

export function compareAgents(
  a: AgentRunRecord,
  b: AgentRunRecord,
  col: AgentSortColumn,
  dir: SortDirection,
): number {
  let cmp = 0;
  switch (col) {
    case "name":
      cmp = (a.workflow_name || "").localeCompare(b.workflow_name || "");
      break;
    case "provider":
      cmp = (a.provider || "").localeCompare(b.provider || "");
      break;
    case "time":
      cmp = a.created_at.localeCompare(b.created_at);
      break;
    case "duration": {
      const da =
        a.started_at && a.completed_at
          ? new Date(a.completed_at).getTime() -
            new Date(a.started_at).getTime()
          : 0;
      const db =
        b.started_at && b.completed_at
          ? new Date(b.completed_at).getTime() -
            new Date(b.started_at).getTime()
          : 0;
      cmp = da - db;
      break;
    }
    case "turns":
      cmp = (a.turns_used || 0) - (b.turns_used || 0);
      break;
    case "status":
      cmp = a.status.localeCompare(b.status);
      break;
  }
  return dir === "asc" ? cmp : -cmp;
}

export function groupBy<T>(
  items: T[],
  keyFn: (item: T) => string,
): Map<string, T[]> {
  const groups = new Map<string, T[]>();
  for (const item of items) {
    const key = keyFn(item) || "Unknown";
    const arr = groups.get(key) || [];
    arr.push(item);
    groups.set(key, arr);
  }
  return groups;
}
