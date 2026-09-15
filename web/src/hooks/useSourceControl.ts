import { useState, useEffect, useCallback, useRef } from "react";

// =============================================================================
// Types
// =============================================================================

export interface SourceControlStatus {
  github_repo: string | null;
  current_branch: string | null;
  branch_count: number;
  worktree_count: number;
  clone_count: number;
}

export interface GitBranch {
  name: string;
  is_current: boolean;
  is_remote: boolean;
  ahead: number;
  behind: number;
  last_commit_date: string;
  worktree_id: string | null;
}

export interface GitCommit {
  sha: string;
  short_sha: string;
  message: string;
  author: string;
  date: string;
}

export interface WorktreeInfo {
  id: string;
  branch_name: string | null;
  worktree_path: string;
  status: string;
  task_id: string | null;
  agent_session_id: string | null;
  project_id: string;
  base_branch: string;
  created_at: string;
  updated_at: string;
  merged_at: string | null;
}

export interface CloneInfo {
  id: string;
  branch_name: string | null;
  clone_path: string;
  remote_url: string | null;
  status: string;
  task_id: string | null;
  project_id: string;
  base_branch: string;
  created_at: string;
  updated_at: string;
}

export interface DiffResult {
  diff_stat: string;
  files: { status: string; path: string }[];
  patch: string;
}

// =============================================================================
// Constants
// =============================================================================

const LOCAL_POLL_MS = 5000;

function getBaseUrl(): string {
  return "";
}

function isAbortError(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    error.name === "AbortError"
  );
}

// =============================================================================
// Hook
// =============================================================================

export function useSourceControl(projectId: string | null = null) {
  const [status, setStatus] = useState<SourceControlStatus | null>(null);
  const [branches, setBranches] = useState<GitBranch[]>([]);
  const [worktrees, setWorktrees] = useState<WorktreeInfo[]>([]);
  const [clones, setClones] = useState<CloneInfo[]>([]);

  const [isLoading, setIsLoading] = useState(true);
  const [errors, setErrors] = useState<Record<string, string>>({});

  const setFetcherError = useCallback((key: string, message: string | null) => {
    setErrors((prev) => {
      if (message === null) {
        if (!(key in prev)) return prev;
        const next = { ...prev };
        delete next[key];
        return next;
      }
      return { ...prev, [key]: message };
    });
  }, []);

  const error =
    Object.values(errors).length > 0 ? Object.values(errors).join("; ") : null;

  const localPollRef = useRef<number | null>(null);
  const requestIdsRef = useRef<Record<string, number>>({});
  const fetchLocalRef = useRef<(signal: AbortSignal) => Promise<void>>(() =>
    Promise.resolve(),
  );

  const beginRequest = useCallback((key: string) => {
    const requestId = (requestIdsRef.current[key] ?? 0) + 1;
    requestIdsRef.current[key] = requestId;
    return requestId;
  }, []);

  const isCurrentRequest = useCallback(
    (key: string, requestId: number, signal?: AbortSignal) =>
      !signal?.aborted && requestIdsRef.current[key] === requestId,
    [],
  );

  const buildParams = useCallback(
    (extra?: Record<string, string>) => {
      const params = new URLSearchParams();
      if (projectId) params.set("project_id", projectId);
      if (extra) {
        for (const [k, v] of Object.entries(extra)) params.set(k, v);
      }
      return params.toString();
    },
    [projectId],
  );

  // --- Fetch functions ---

  const fetchStatus = useCallback(
    async (signal?: AbortSignal) => {
      const requestId = beginRequest("status");
      try {
        const r = await fetch(
          `${getBaseUrl()}/api/source-control/status?${buildParams()}`,
          { signal },
        );
        if (r.ok) {
          const data = await r.json();
          if (!isCurrentRequest("status", requestId, signal)) return;
          setStatus(data);
          setFetcherError("status", null);
        } else if (isCurrentRequest("status", requestId, signal)) {
          setFetcherError("status", `HTTP ${r.status}: ${r.statusText}`);
        }
      } catch (e) {
        if (isAbortError(e) || !isCurrentRequest("status", requestId, signal))
          return;
        const message = e instanceof Error ? e.message : String(e);
        setFetcherError("status", message);
        console.error("Failed to fetch source control status:", e);
      }
    },
    [beginRequest, buildParams, isCurrentRequest, setFetcherError],
  );

  const fetchBranches = useCallback(
    async (signal?: AbortSignal) => {
      const requestId = beginRequest("branches");
      try {
        const r = await fetch(
          `${getBaseUrl()}/api/source-control/branches?${buildParams()}`,
          { signal },
        );
        if (r.ok) {
          const data = await r.json();
          if (!isCurrentRequest("branches", requestId, signal)) return;
          setBranches(data.branches || []);
          setFetcherError("branches", null);
        } else if (isCurrentRequest("branches", requestId, signal)) {
          setFetcherError("branches", `Branches: HTTP ${r.status}`);
        }
      } catch (e) {
        if (isAbortError(e) || !isCurrentRequest("branches", requestId, signal))
          return;
        setFetcherError(
          "branches",
          e instanceof Error ? e.message : "Failed to fetch branches",
        );
        console.error("Failed to fetch branches:", e);
      }
    },
    [beginRequest, buildParams, isCurrentRequest, setFetcherError],
  );

  const fetchWorktrees = useCallback(
    async (signal?: AbortSignal) => {
      const requestId = beginRequest("worktrees");
      try {
        const r = await fetch(
          `${getBaseUrl()}/api/source-control/worktrees?${buildParams()}`,
          { signal },
        );
        if (r.ok) {
          const data = await r.json();
          if (!isCurrentRequest("worktrees", requestId, signal)) return;
          setWorktrees(data.worktrees || []);
          setFetcherError("worktrees", null);
        } else if (isCurrentRequest("worktrees", requestId, signal)) {
          setFetcherError("worktrees", `Worktrees: HTTP ${r.status}`);
        }
      } catch (e) {
        if (
          isAbortError(e) ||
          !isCurrentRequest("worktrees", requestId, signal)
        )
          return;
        setFetcherError(
          "worktrees",
          e instanceof Error ? e.message : "Failed to fetch worktrees",
        );
        console.error("Failed to fetch worktrees:", e);
      }
    },
    [beginRequest, buildParams, isCurrentRequest, setFetcherError],
  );

  const fetchClones = useCallback(
    async (signal?: AbortSignal) => {
      const requestId = beginRequest("clones");
      try {
        const r = await fetch(
          `${getBaseUrl()}/api/source-control/clones?${buildParams()}`,
          { signal },
        );
        if (r.ok) {
          const data = await r.json();
          if (!isCurrentRequest("clones", requestId, signal)) return;
          setClones(data.clones || []);
          setFetcherError("clones", null);
        } else if (isCurrentRequest("clones", requestId, signal)) {
          setFetcherError("clones", `Clones: HTTP ${r.status}`);
        }
      } catch (e) {
        if (isAbortError(e) || !isCurrentRequest("clones", requestId, signal))
          return;
        setFetcherError(
          "clones",
          e instanceof Error ? e.message : "Failed to fetch clones",
        );
        console.error("Failed to fetch clones:", e);
      }
    },
    [beginRequest, buildParams, isCurrentRequest, setFetcherError],
  );

  // --- On-demand fetchers ---

  const fetchCommits = useCallback(
    async (branchName: string, limit = 20): Promise<GitCommit[]> => {
      try {
        const r = await fetch(
          `${getBaseUrl()}/api/source-control/branches/${encodeURIComponent(branchName)}/commits?${buildParams({ limit: String(limit) })}`,
        );
        if (r.ok) {
          const data = await r.json();
          return data.commits || [];
        }
      } catch (e) {
        console.error("Failed to fetch commits:", e);
      }
      return [];
    },
    [buildParams],
  );

  const fetchDiff = useCallback(
    async (base: string, head: string): Promise<DiffResult | null> => {
      try {
        const r = await fetch(
          `${getBaseUrl()}/api/source-control/diff?${buildParams({ base, head })}`,
        );
        if (r.ok) return await r.json();
      } catch (e) {
        console.error("Failed to fetch diff:", e);
      }
      return null;
    },
    [buildParams],
  );

  // --- Actions ---

  const deleteWorktree = useCallback(
    async (id: string): Promise<boolean> => {
      try {
        const r = await fetch(
          `${getBaseUrl()}/api/source-control/worktrees/${id}`,
          {
            method: "DELETE",
          },
        );
        if (r.ok) {
          await fetchWorktrees();
          await fetchStatus();
          return true;
        }
      } catch (e) {
        console.error("Failed to delete worktree:", e);
      }
      return false;
    },
    [fetchWorktrees, fetchStatus],
  );

  const cleanupWorktrees = useCallback(
    async (hours = 24, dryRun = false): Promise<WorktreeInfo[]> => {
      try {
        const r = await fetch(
          `${getBaseUrl()}/api/source-control/worktrees/cleanup?${buildParams({
            hours: String(hours),
            dry_run: String(dryRun),
          })}`,
          { method: "POST" },
        );
        if (r.ok) {
          const data = await r.json();
          if (!dryRun) {
            await fetchWorktrees();
            await fetchStatus();
          }
          return data.candidates || [];
        }
      } catch (e) {
        console.error("Failed to cleanup worktrees:", e);
      }
      return [];
    },
    [buildParams, fetchWorktrees, fetchStatus],
  );

  const syncWorktree = useCallback(
    async (id: string): Promise<boolean> => {
      try {
        const r = await fetch(
          `${getBaseUrl()}/api/source-control/worktrees/${id}/sync`,
          {
            method: "POST",
          },
        );
        if (r.ok) {
          await fetchWorktrees();
          return true;
        }
      } catch (e) {
        console.error("Failed to sync worktree:", e);
      }
      return false;
    },
    [fetchWorktrees],
  );

  const deleteClone = useCallback(
    async (id: string): Promise<boolean> => {
      try {
        const r = await fetch(
          `${getBaseUrl()}/api/source-control/clones/${id}`,
          {
            method: "DELETE",
          },
        );
        if (r.ok) {
          await fetchClones();
          await fetchStatus();
          return true;
        }
      } catch (e) {
        console.error("Failed to delete clone:", e);
      }
      return false;
    },
    [fetchClones, fetchStatus],
  );

  const syncClone = useCallback(
    async (id: string): Promise<boolean> => {
      try {
        const r = await fetch(
          `${getBaseUrl()}/api/source-control/clones/${id}/sync`,
          {
            method: "POST",
          },
        );
        if (r.ok) {
          await fetchClones();
          return true;
        }
      } catch (e) {
        console.error("Failed to sync clone:", e);
      }
      return false;
    },
    [fetchClones],
  );

  // --- Fetch all local data ---

  const fetchLocal = useCallback(
    async (signal: AbortSignal) => {
      await Promise.all([
        fetchStatus(signal),
        fetchBranches(signal),
        fetchWorktrees(signal),
        fetchClones(signal),
      ]);
      if (!signal.aborted) setIsLoading(false);
    },
    [fetchStatus, fetchBranches, fetchWorktrees, fetchClones],
  );

  // --- Refresh all ---

  const refresh = useCallback(async () => {
    setIsLoading(true);
    const controller = new AbortController();
    await fetchLocal(controller.signal);
  }, [fetchLocal]);

  // Keep refs updated with latest fetch functions
  useEffect(() => {
    fetchLocalRef.current = fetchLocal;
  }, [fetchLocal]);

  // --- Effects ---

  // Local data: initial fetch + polling (5s) — restarts on projectId change
  useEffect(() => {
    const controller = new AbortController();
    setIsLoading(true);
    setErrors({});
    fetchLocalRef.current(controller.signal);
    localPollRef.current = window.setInterval(() => {
      fetchLocalRef.current(controller.signal);
    }, LOCAL_POLL_MS);
    return () => {
      controller.abort();
      if (localPollRef.current) window.clearInterval(localPollRef.current);
    };
  }, [projectId]);

  return {
    // Data
    status,
    branches,
    worktrees,
    clones,

    // State
    isLoading,
    error,

    // On-demand
    fetchCommits,
    fetchDiff,

    // Actions
    deleteWorktree,
    cleanupWorktrees,
    syncWorktree,
    deleteClone,
    syncClone,
    refresh,
  };
}
