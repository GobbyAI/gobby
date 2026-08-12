import {
  useCallback,
  useEffect,
  useState,
  type Dispatch,
  type MouseEvent,
  type SetStateAction,
} from "react";
import type { GobbyTask } from "../../types/tasks";
import type { RawTaskPayload } from "../../lib/taskNormalization";
import {
  claimTaskForSession,
  postBuildControl,
  postTaskLifecycleAction,
  startBuild,
  startQuickBuild,
} from "./TasksTabActions";
import type { GobbyTaskDetail } from "./TasksTabDetailPanel";
import {
  type ActiveTaskAction,
  type TaskContextMenu,
  type TaskMenuAction,
} from "./TaskQuickMenu";
import { getBaseUrl } from "./TasksTabData";

interface UseTasksTabMenuActionsProps {
  applyRawTaskUpdate: (taskId: string, rawTask: RawTaskPayload | null) => void;
  chatSessionId?: string | null;
  fetchTasks: () => void;
  setActionError: Dispatch<SetStateAction<string | null>>;
  taskDetail: GobbyTaskDetail | null;
}

export function useTasksTabMenuActions({
  applyRawTaskUpdate,
  chatSessionId,
  fetchTasks,
  setActionError,
  taskDetail,
}: UseTasksTabMenuActionsProps) {
  const [activeTaskAction, setActiveTaskAction] =
    useState<ActiveTaskAction | null>(null);
  const [taskMenu, setTaskMenu] = useState<TaskContextMenu | null>(null);
  const [closeDialogTask, setCloseDialogTask] = useState<GobbyTask | null>(
    null,
  );

  const closeTaskMenu = useCallback(() => setTaskMenu(null), []);

  const runMenuAction = useCallback(
    async (
      task: GobbyTask,
      action: TaskMenuAction,
      operation: () => Promise<RawTaskPayload | null>,
      errorPrefix: string,
      refetchAfter = false,
    ) => {
      closeTaskMenu();
      setActiveTaskAction({ taskId: task.id, action });
      setActionError(null);
      try {
        const rawTask = await operation();
        if (rawTask !== null) applyRawTaskUpdate(task.id, rawTask);
        if (refetchAfter) fetchTasks();
      } catch (error) {
        setActionError(
          error instanceof Error
            ? `${errorPrefix}: ${error.message}`
            : `${errorPrefix}.`,
        );
      } finally {
        setActiveTaskAction(null);
      }
    },
    [applyRawTaskUpdate, closeTaskMenu, fetchTasks, setActionError],
  );

  useEffect(() => {
    if (!taskMenu) return;
    const handleWindowClick = () => setTaskMenu(null);
    window.addEventListener("click", handleWindowClick);
    return () => window.removeEventListener("click", handleWindowClick);
  }, [taskMenu]);

  const handleMenuButtonClick = useCallback(
    (event: MouseEvent<HTMLButtonElement>, task: GobbyTask) => {
      event.stopPropagation();
      const rect = event.currentTarget.getBoundingClientRect();
      setTaskMenu({
        x: rect.left,
        y: rect.top,
        width: rect.width,
        height: rect.height,
        task,
      });
    },
    [],
  );

  const handleAssignToMainChat = useCallback(() => {
    if (!taskMenu?.task.id || !chatSessionId) {
      return;
    }
    const task = taskMenu.task;
    void runMenuAction(
      task,
      "assign",
      () => claimTaskForSession(getBaseUrl(), task.id, chatSessionId),
      "Failed to assign task to main chat",
    );
  }, [chatSessionId, runMenuAction, taskMenu]);

  const handleBuild = useCallback(() => {
    if (!taskMenu?.task) return;
    const task = taskMenu.task;
    void runMenuAction(
      task,
      "build",
      async () => {
        await startBuild(getBaseUrl(), task);
        return null;
      },
      "Failed to start build",
      true,
    );
  }, [runMenuAction, taskMenu]);

  const handleBuildQuick = useCallback(() => {
    if (!taskMenu?.task) return;
    const task = taskMenu.task;
    void runMenuAction(
      task,
      "buildQuick",
      async () => {
        await startQuickBuild(getBaseUrl(), task);
        return null;
      },
      "Failed to start quick build",
      true,
    );
  }, [runMenuAction, taskMenu]);

  const handleStopBuild = useCallback(() => {
    if (!taskMenu?.task) return;
    const task = taskMenu.task;
    void runMenuAction(
      task,
      "stopBuild",
      async () => {
        await postBuildControl(getBaseUrl(), "stop", task);
        return null;
      },
      "Failed to stop build",
      true,
    );
  }, [runMenuAction, taskMenu]);

  const handleResumeBuild = useCallback(() => {
    if (!taskMenu?.task) return;
    const task = taskMenu.task;
    void runMenuAction(
      task,
      "resumeBuild",
      async () => {
        await postBuildControl(getBaseUrl(), "resume", task);
        return null;
      },
      "Failed to resume build",
      true,
    );
  }, [runMenuAction, taskMenu]);

  const handleReleaseClaim = useCallback(() => {
    if (!taskMenu?.task) return;
    const task = taskMenu.task;
    void runMenuAction(
      task,
      "releaseClaim",
      () => postTaskLifecycleAction(getBaseUrl(), task.id, "release-claim"),
      "Failed to release task claim",
    );
  }, [runMenuAction, taskMenu]);

  const handleDetailClaim = useCallback(() => {
    if (!taskDetail || !chatSessionId) return;
    const detail = taskDetail;
    void runMenuAction(
      detail,
      "assign",
      () => claimTaskForSession(getBaseUrl(), detail.id, chatSessionId),
      "Failed to claim task",
    );
  }, [chatSessionId, runMenuAction, taskDetail]);

  const handleDetailRelease = useCallback(() => {
    if (!taskDetail) return;
    const detail = taskDetail;
    void runMenuAction(
      detail,
      "releaseClaim",
      () => postTaskLifecycleAction(getBaseUrl(), detail.id, "release-claim"),
      "Failed to release task claim",
    );
  }, [runMenuAction, taskDetail]);

  const handleOpenCloseTaskDialog = useCallback(() => {
    if (!taskMenu?.task) return;
    setCloseDialogTask(taskMenu.task);
    closeTaskMenu();
  }, [closeTaskMenu, taskMenu]);

  const handleCloseTask = useCallback(
    (reason: string) => {
      if (!closeDialogTask) return;
      const task = closeDialogTask;
      void runMenuAction(
        task,
        "close",
        () =>
          postTaskLifecycleAction(getBaseUrl(), task.id, "close", { reason }),
        "Failed to close task",
      );
      setCloseDialogTask(null);
    },
    [closeDialogTask, runMenuAction],
  );

  const handleReopenTask = useCallback(() => {
    if (!taskMenu?.task) return;
    const task = taskMenu.task;
    void runMenuAction(
      task,
      "reopen",
      () => postTaskLifecycleAction(getBaseUrl(), task.id, "reopen"),
      "Failed to reopen task",
    );
  }, [runMenuAction, taskMenu]);

  return {
    activeTaskAction,
    closeDialogTask,
    closeTaskMenu,
    handleAssignToMainChat,
    handleBuild,
    handleBuildQuick,
    handleCloseTask,
    handleDetailClaim,
    handleDetailRelease,
    handleMenuButtonClick,
    handleOpenCloseTaskDialog,
    handleReleaseClaim,
    handleReopenTask,
    handleResumeBuild,
    handleStopBuild,
    setCloseDialogTask,
    taskMenu,
  };
}
