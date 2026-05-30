import { useCallback, type Dispatch, type SetStateAction } from "react";

import type { CreateTaskParams } from "../tasks/TaskCreateForm";
import { useRegisterActivityActions } from "./activityActions";
import { getBaseUrl } from "./TasksTabData";

interface UseTaskActionsOptions {
  projectId?: string | null;
  fetchTasks: () => void;
  loading: boolean;
  setShowCreateTask: Dispatch<SetStateAction<boolean>>;
  setActionError: Dispatch<SetStateAction<string | null>>;
}

interface UseTaskActionsResult {
  handleCreateTask: (params: CreateTaskParams) => Promise<unknown>;
}

function describeCaughtError(err: unknown): string {
  if (!(err instanceof Error)) return `message: ${String(err)}`;
  const parts = [`message: ${err.message}`];
  if (err.stack) parts.push(`stack: ${err.stack}`);
  if ("cause" in err && err.cause !== undefined) {
    parts.push(`cause: ${String(err.cause)}`);
  }
  return parts.join("\n");
}

function throwCreateTaskError(
  message: string,
  err: unknown,
  setActionError: Dispatch<SetStateAction<string | null>>,
): never {
  const detailed = `${message}\n${describeCaughtError(err)}`;
  setActionError(detailed);
  const error = new Error(detailed) as Error & { cause?: unknown };
  error.cause = err;
  throw error;
}

export function useTaskActions({
  projectId,
  fetchTasks,
  loading,
  setShowCreateTask,
  setActionError,
}: UseTaskActionsOptions): UseTaskActionsResult {
  const handleCreateTask = useCallback(
    async (params: CreateTaskParams) => {
      const baseUrl = getBaseUrl();
      const body = projectId ? { ...params, project_id: projectId } : params;
      let response: Response;
      try {
        response = await fetch(`${baseUrl}/api/tasks`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
      } catch (err) {
        const message = "Failed to create task: request failed";
        throwCreateTaskError(message, err, setActionError);
      }

      let payload: unknown;
      try {
        payload = await response.json();
      } catch (err) {
        const message = response.ok
          ? "Failed to create task: invalid response"
          : `Failed to create task (${response.status})`;
        throwCreateTaskError(message, err, setActionError);
      }

      if (!response.ok) {
        const record = payload as { detail?: unknown; error?: unknown } | null;
        const detail = record?.detail ?? record?.error;
        const detailMessage =
          typeof detail === "string"
            ? detail
            : `Failed to create task (${response.status})`;
        const message = detailMessage || `Failed to create task (${response.status})`;
        setActionError(message);
        throw new Error(message);
      }
      fetchTasks();
      return payload;
    },
    [projectId, fetchTasks, setActionError],
  );

  const handleOpenCreateTask = useCallback(
    () => setShowCreateTask(true),
    [setShowCreateTask],
  );

  useRegisterActivityActions(
    {
      onAdd: handleOpenCreateTask,
      addLabel: "Add",
      addAriaLabel: "New task",
      onRefresh: fetchTasks,
      refreshing: loading,
      refreshLabel: "Refresh",
      refreshAriaLabel: "Refresh tasks",
    },
    [handleOpenCreateTask, fetchTasks, loading],
  );

  return { handleCreateTask };
}
