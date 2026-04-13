import { useState, useEffect, useCallback, useRef } from "react";
import { useWebSocketEvent } from "./useWebSocketEvent";

export interface AgentRunRecord {
  id: string;
  parent_session_id: string;
  child_session_id: string | null;
  workflow_name: string | null;
  provider: string;
  model: string | null;
  status: "pending" | "running" | "success" | "error" | "timeout" | "cancelled";
  prompt: string;
  result: string | null;
  error: string | null;
  tool_calls_count: number;
  turns_used: number;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
  task_id: string | null;
  mode: string;
  worktree_id: string | null;
  clone_id: string | null;
  // Session enrichment (from API)
  usage_input_tokens?: number;
  usage_output_tokens?: number;
  usage_cache_creation_tokens?: number;
  usage_cache_read_tokens?: number;
  summary_markdown?: string | null;
  git_branch?: string | null;
}

export interface AgentRunDetail extends AgentRunRecord {
  commands?: Array<{
    id: string;
    from_session: string;
    command_text: string;
    allowed_tools: string | null;
    allowed_mcp_tools: string | null;
    exit_condition: string | null;
    status: string;
    created_at: string;
  }>;
}

interface Filters {
  status?: string;
}

export function useAgentRuns(projectId?: string | null) {
  const [runs, setRuns] = useState<AgentRunRecord[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [filters, setFilters] = useState<Filters>({});
  const refetchTimerRef = useRef<number | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const fetchRuns = useCallback(async () => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    const params = new URLSearchParams();
    if (filters.status) params.set("status", filters.status);
    if (projectId) params.set("project_id", projectId);
    params.set("limit", "100");

    try {
      const res = await fetch(`/api/agents/runs?${params}`, {
        signal: controller.signal,
      });
      if (res.ok) {
        const data = await res.json();
        setRuns(data.runs || []);
      } else {
        console.error(
          "Failed to fetch agent runs:",
          res.status,
          res.statusText,
        );
        setRuns([]);
      }
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") return;
      console.error("Failed to fetch agent runs:", e);
    } finally {
      setIsLoading(false);
    }
  }, [filters, projectId]);

  // Initial load + refetch on filter change
  useEffect(() => {
    setIsLoading(true);
    fetchRuns();
  }, [fetchRuns]);

  // Real-time updates via WebSocket
  useWebSocketEvent(
    "agent_event",
    useCallback(() => {
      if (refetchTimerRef.current) clearTimeout(refetchTimerRef.current);
      refetchTimerRef.current = window.setTimeout(() => {
        fetchRuns();
      }, 500);
    }, [fetchRuns]),
  );

  // Clean up on unmount
  useEffect(() => {
    return () => {
      if (refetchTimerRef.current) clearTimeout(refetchTimerRef.current);
      abortRef.current?.abort();
    };
  }, []);

  const cancelRun = useCallback(
    async (runId: string) => {
      const res = await fetch(
        `/api/agents/runs/${encodeURIComponent(runId)}/cancel`,
        {
          method: "POST",
        },
      );
      if (!res.ok) throw new Error(`Failed to cancel: ${res.statusText}`);
      const data = await res.json();
      await fetchRuns();
      return data;
    },
    [fetchRuns],
  );

  const fetchRunDetail = useCallback(
    async (runId: string): Promise<AgentRunDetail | null> => {
      try {
        const res = await fetch(
          `/api/agents/runs/${encodeURIComponent(runId)}`,
        );
        if (!res.ok) return null;
        const data = await res.json();
        return data.run || null;
      } catch {
        return null;
      }
    },
    [],
  );

  return {
    runs,
    isLoading,
    filters,
    setFilters,
    fetchRuns,
    cancelRun,
    fetchRunDetail,
  };
}
