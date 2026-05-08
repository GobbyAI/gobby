import { useState, useEffect, useCallback, useRef } from "react";
import { useWebSocketEvent } from "./useWebSocketEvent";

export interface PipelineStepExecution {
  id: number;
  step_id: string;
  status:
    | "pending"
    | "running"
    | "completed"
    | "skipped"
    | "failed"
    | "waiting_approval";
  started_at: string | null;
  completed_at: string | null;
  output_json: string | null;
  error: string | null;
  approval_token: string | null;
}

export interface PipelineExecutionRecord {
  id: string;
  pipeline_name: string;
  project_id: string;
  status:
    | "pending"
    | "running"
    | "completed"
    | "failed"
    | "waiting_approval"
    | "cancelled"
    | "interrupted";
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  inputs_json: string | null;
  outputs_json: string | null;
  trace_id?: string;
  steps: PipelineStepExecution[];
  cron_job_name?: string | null;
  cron_expr?: string | null;
  definition_json?: string | null;
  parent_execution_id?: string | null;
}

interface Filters {
  status?: string;
  pipeline_name?: string;
}

export interface UsePipelineExecutionsOptions {
  projectId?: string;
  limit?: number;
  offset?: number;
}

const DEFAULT_LIMIT = 50;

export function usePipelineExecutions(
  options?: UsePipelineExecutionsOptions | string,
) {
  // Backward-compat: callers used to pass a bare projectId string.
  const opts: UsePipelineExecutionsOptions =
    typeof options === "string" ? { projectId: options } : (options ?? {});
  const { projectId, limit = DEFAULT_LIMIT, offset = 0 } = opts;

  const [executions, setExecutions] = useState<PipelineExecutionRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [statusSummary, setStatusSummary] = useState<Record<string, number>>(
    {},
  );
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filters, setFilters] = useState<Filters>({});
  const refetchTimerRef = useRef<number | null>(null);

  const fetchExecutions = useCallback(async () => {
    const params = new URLSearchParams();
    if (projectId) params.set("project_id", projectId);
    if (filters.status) params.set("status", filters.status);
    if (filters.pipeline_name)
      params.set("pipeline_name", filters.pipeline_name);
    params.set("limit", String(limit));
    params.set("offset", String(offset));

    try {
      const res = await fetch(`/api/pipelines/executions?${params}`);
      if (res.ok) {
        const data = await res.json();
        setExecutions(data.executions || []);
        setTotal(typeof data.total === "number" ? data.total : 0);
        setStatusSummary(
          (data.status_summary as Record<string, number> | undefined) || {},
        );
        setError(null);
      } else {
        console.error(
          "Failed to fetch pipeline executions:",
          res.status,
          res.statusText,
        );
        setExecutions([]);
        setTotal(0);
        setStatusSummary({});
        setError(`HTTP ${res.status}: ${res.statusText}`);
      }
    } catch (e) {
      console.error("Failed to fetch pipeline executions:", e);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setIsLoading(false);
    }
  }, [projectId, filters, limit, offset]);

  // Initial load + refetch on filter / pagination change
  useEffect(() => {
    setIsLoading(true);
    fetchExecutions();
  }, [fetchExecutions]);

  // Real-time updates via singleton WebSocket. WS lifecycle events update
  // existing rows in place by execution_id, so a *new* execution arriving
  // while the user is on page 2 won't migrate to page 1 until the next
  // refetch — which we trigger on a short debounce so the UI stays fresh.
  useWebSocketEvent(
    "pipeline_event",
    useCallback(() => {
      if (refetchTimerRef.current) clearTimeout(refetchTimerRef.current);
      refetchTimerRef.current = window.setTimeout(() => {
        fetchExecutions();
      }, 500);
    }, [fetchExecutions]),
  );

  // Clean up debounce timer on unmount
  useEffect(() => {
    return () => {
      if (refetchTimerRef.current) clearTimeout(refetchTimerRef.current);
    };
  }, []);

  const approvePipeline = useCallback(
    async (token: string) => {
      const res = await fetch(
        `/api/pipelines/approve/${encodeURIComponent(token)}`,
        {
          method: "POST",
        },
      );
      if (!res.ok) throw new Error(`Failed to approve: ${res.statusText}`);
      const data = await res.json();
      fetchExecutions();
      return data;
    },
    [fetchExecutions],
  );

  const rejectPipeline = useCallback(
    async (token: string) => {
      const res = await fetch(
        `/api/pipelines/reject/${encodeURIComponent(token)}`,
        {
          method: "POST",
        },
      );
      if (!res.ok) throw new Error(`Failed to reject: ${res.statusText}`);
      const data = await res.json();
      fetchExecutions();
      return data;
    },
    [fetchExecutions],
  );

  return {
    executions,
    total,
    limit,
    offset,
    statusSummary,
    isLoading,
    error,
    filters,
    setFilters,
    fetchExecutions,
    refetch: fetchExecutions,
    approvePipeline,
    rejectPipeline,
  };
}
