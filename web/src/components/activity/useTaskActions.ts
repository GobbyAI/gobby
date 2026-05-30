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
      const response = await fetch(`${baseUrl}/api/tasks`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const payload = await response.json().catch(() => null);
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
