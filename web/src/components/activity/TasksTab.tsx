import {
  memo,
  useState,
  useEffect,
  useCallback,
  useRef,
  useMemo,
  type MouseEvent,
} from "react";
import { ResizeHandle } from "../chat/artifacts/ResizeHandle";
import { useWebSocketEvent } from "../../hooks/useWebSocketEvent";
import { useStagesRegistry } from "../../hooks/useStagesRegistry";
import type { DependencyTree, GobbyTask } from "../../hooks/useTasks";
import { PriorityBadge, TaskStateBadges, TypeBadge } from "../tasks/TaskBadges";
import {
  getCanonicalTaskState,
  getTaskDisplayState,
} from "../../lib/taskState";
import {
  normalizeTaskPayloads,
  type RawTaskPayload,
} from "../../lib/taskNormalization";
import {
  buildTree,
  collectExpandableNodeIds,
  collectVisibleTaskRows,
  compareTasksForDisplay,
  DEFAULT_FILTERS,
  filterTreeBySearch,
  matchesTaskFilter,
  RECENT_CLOSED_TASK_LIMIT,
  type TaskFilterKey,
} from "./TasksTabModel";
import { TasksTabFilters } from "./TasksTabFilters";
import {
  TasksTabDetailPanel,
  type GobbyTaskDetail,
  type ParentTaskRef,
} from "./TasksTabDetailPanel";
import { DEFAULT_TOP_PANEL_PERCENT } from "./constants";
import { ActivityPanelEmpty, TasksEmptyIcon } from "./ActivityPanelEmpty";
import { ActivityPanelSearch } from "./ActivityPanelSearch";
import { TaskCloseDialog } from "./TaskCloseDialog";
import {
  type ActiveTaskAction,
  TaskQuickMenu,
  type TaskContextMenu,
  type TaskMenuAction,
} from "./TaskQuickMenu";
import { TaskTreeRow } from "./TaskTreeRow";
import {
  claimTaskForSession,
  postBuildControl,
  postTaskLifecycleAction,
  startBuild,
  startQuickBuild,
} from "./TasksTabActions";
import {
  areSetsEqual,
  extractTaskPayload,
  fetchMissingTaskAncestors,
  getBaseUrl,
  getCurrentStageName,
  mergeTasksById,
  normalizeActivityTask,
} from "./TasksTabData";

interface TasksTabProps {
  projectId?: string | null;
  chatSessionId?: string | null;
}

export const TasksTab = memo(function TasksTab({
  projectId,
  chatSessionId,
}: TasksTabProps) {
  const { registry: stagesRegistry } = useStagesRegistry();
  const [tasks, setTasks] = useState<GobbyTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [selectedStageFilters, setSelectedStageFilters] = useState<Set<string>>(
    () => new Set(),
  );
  const registryStageNames = useMemo(
    () => stagesRegistry.map((stage) => stage.name).sort(),
    [stagesRegistry],
  );
  const defaultStageFilters = useMemo(
    () => new Set(registryStageNames),
    [registryStageNames],
  );
  const [statusFilters, setStatusFilters] = useState<Set<TaskFilterKey>>(
    () => new Set(DEFAULT_FILTERS),
  );
  const [showFilterDropdown, setShowFilterDropdown] = useState(false);
  const previousDefaultStageFiltersRef = useRef<Set<string>>(new Set());
  const stageFiltersInitializedRef = useRef(false);
  useEffect(() => {
    const previousDefaultStageFilters = previousDefaultStageFiltersRef.current;
    setSelectedStageFilters((prev) => {
      const shouldUseDefault =
        !stageFiltersInitializedRef.current ||
        areSetsEqual(prev, previousDefaultStageFilters);
      stageFiltersInitializedRef.current = true;

      if (shouldUseDefault) {
        return areSetsEqual(prev, defaultStageFilters)
          ? prev
          : new Set(defaultStageFilters);
      }

      const next = new Set(
        [...prev].filter((stageName) => defaultStageFilters.has(stageName)),
      );
      return areSetsEqual(prev, next) ? prev : next;
    });
    previousDefaultStageFiltersRef.current = new Set(defaultStageFilters);
  }, [defaultStageFilters]);
  const stageSelectionMatchesDefault = useMemo(
    () => areSetsEqual(selectedStageFilters, defaultStageFilters),
    [defaultStageFilters, selectedStageFilters],
  );
  const selectedRegistryStageNames = useMemo(
    () => registryStageNames.filter((stageName) => selectedStageFilters.has(stageName)),
    [registryStageNames, selectedStageFilters],
  );
  const stageQueryKey = useMemo(
    () =>
      !stageSelectionMatchesDefault && selectedRegistryStageNames.length > 0
        ? selectedRegistryStageNames.join("\u0000")
        : "",
    [selectedRegistryStageNames, stageSelectionMatchesDefault],
  );
  const stageQueryList = useMemo(
    () => (stageQueryKey ? stageQueryKey.split("\u0000") : []),
    [stageQueryKey],
  );
  const includeClosedTasks = statusFilters.has("closed");
  const activeFilterCount = useMemo(() => {
    const symmetricDifference = new Set([...DEFAULT_FILTERS, ...statusFilters]);
    const statusFilterCount = [...symmetricDifference].filter(
      (key) => DEFAULT_FILTERS.has(key) !== statusFilters.has(key),
    ).length;
    const stageFilterCount = registryStageNames.filter(
      (stageName) => !selectedStageFilters.has(stageName),
    ).length;
    return statusFilterCount + (stageSelectionMatchesDefault ? 0 : stageFilterCount);
  }, [
    registryStageNames,
    selectedStageFilters,
    stageSelectionMatchesDefault,
    statusFilters,
  ]);
  const [topHeight, setTopHeight] = useState(DEFAULT_TOP_PANEL_PERCENT);
  const [taskDetail, setTaskDetail] = useState<GobbyTaskDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [taskDependencies, setTaskDependencies] = useState<DependencyTree | null>(null);
  const [taskSubtasks, setTaskSubtasks] = useState<GobbyTask[]>([]);
  const [activeTaskAction, setActiveTaskAction] = useState<ActiveTaskAction | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [taskMenu, setTaskMenu] = useState<TaskContextMenu | null>(null);
  const [closeDialogTask, setCloseDialogTask] = useState<GobbyTask | null>(null);
  const [collapsedTaskIds, setCollapsedTaskIds] = useState<Set<string>>(
    () => new Set(),
  );

  // Fetch tasks, then apply canonical state filters client-side.
  const abortRef = useRef<AbortController | null>(null);
  const debouncedRefetchRef = useRef<number | null>(null);
  const selectedTaskIdRef = useRef<string | null>(null);
  const userSelectedRef = useRef(false);
  // Abort any in-flight WebSocket-triggered detail fetch when a newer one
  // arrives or when the component unmounts.
  const detailFetchControllerRef = useRef<AbortController | null>(null);

  const fetchTasks = useCallback(() => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    const baseUrl = getBaseUrl();

    const buildParams = (closed: boolean, limit: number) => {
      const params = new URLSearchParams();
      if (projectId) params.set("project_id", projectId);
      params.set("closed", closed ? "true" : "false");
      params.set("limit", String(limit));
      params.set("sort_by", "updated_at");
      params.set("sort_order", "desc");
      params.set("include_stages", "1");
      stageQueryList.forEach((stageName) => params.append("stage", stageName));
      return params;
    };

    const fetchTaskList = async (closed: boolean, limit: number) => {
      const params = buildParams(closed, limit);
      const response = await fetch(`${baseUrl}/api/tasks?${params}`, {
        signal: controller.signal,
      });
      if (!response.ok) return [];
      const data = await response.json();
      return normalizeTaskPayloads(data.tasks ?? []) as GobbyTask[];
    };

    void (async () => {
      const activeTasks = await fetchTaskList(false, 500);
      const closedTasks = includeClosedTasks
        ? await fetchTaskList(true, RECENT_CLOSED_TASK_LIMIT)
        : [];
      const taskList = mergeTasksById(activeTasks, closedTasks);
      const tasksWithAncestors = await fetchMissingTaskAncestors(
        baseUrl,
        taskList,
        controller.signal,
      );
      if (!controller.signal.aborted) setTasks(tasksWithAncestors);
    })()
      .catch((err) => {
        if (err.name !== "AbortError") setTasks([]);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
  }, [includeClosedTasks, projectId, stageQueryList]);

  useEffect(() => {
    fetchTasks();
    return () => {
      abortRef.current?.abort();
      detailFetchControllerRef.current?.abort();
      if (debouncedRefetchRef.current)
        window.clearTimeout(debouncedRefetchRef.current);
    };
  }, [fetchTasks]);

  // WebSocket: real-time task event subscription
  const handleTaskEventRef = useRef<
    (event: string, taskData: Record<string, unknown>) => void
  >(() => {});
  const handleTaskEvent = useCallback(
    (event: string, taskData: Record<string, unknown>) => {
      const taskId = taskData.id as string;
      if (!taskId) return;

      // Ignore events for other projects
      const taskProjectId = taskData.project_id as string | undefined;
      if (projectId && taskProjectId && taskProjectId !== projectId) return;

      if (event === "task_deleted") {
        setTasks((prev) => prev.filter((t) => t.id !== taskId));
        if (taskId === selectedTaskId) {
          userSelectedRef.current = false;
          setSelectedTaskId(null);
        }
      } else if (event === "task_created") {
        const newTask = normalizeActivityTask(taskData as RawTaskPayload);
        setTasks((prev) => {
          if (prev.some((t) => t.id === taskId)) return prev;
          return [...prev, newTask];
        });
      } else {
        // task_updated, task_closed, task_reopened, task_de_escalated
        setTasks((prev) =>
          prev.map((t) =>
            t.id === taskId ? normalizeActivityTask(taskData as RawTaskPayload, t) : t,
          ),
        );
      }

      // Re-fetch detail if the affected task is currently selected.
      // Abort any previous in-flight detail fetch so a stale response can't
      // overwrite a newer one (or land after the component unmounts).
      if (taskId === selectedTaskId && event !== "task_deleted") {
        detailFetchControllerRef.current?.abort();
        const controller = new AbortController();
        detailFetchControllerRef.current = controller;
        setDetailLoading(true);
        const baseUrl = getBaseUrl();
        fetch(`${baseUrl}/api/tasks/${taskId}`, { signal: controller.signal })
          .then((res) => (res.ok ? res.json() : null))
          .then((data) => {
            if (controller.signal.aborted) return;
            const raw = extractTaskPayload(data);
            const cached = tasks.find((task) => task.id === taskId) ?? null;
            setTaskDetail(raw ? (normalizeActivityTask(raw, cached) as GobbyTaskDetail) : null);
          })
          .catch((err) => {
            if (err?.name !== "AbortError") {
              // Swallow non-abort errors quietly — the periodic refetch
              // below will retry shortly.
            }
          })
          .finally(() => {
            if (!controller.signal.aborted) setDetailLoading(false);
          });
      }

      // Debounced full refetch to sync server truth
      if (debouncedRefetchRef.current)
        window.clearTimeout(debouncedRefetchRef.current);
      debouncedRefetchRef.current = window.setTimeout(() => fetchTasks(), 500);
    },
    [fetchTasks, projectId, selectedTaskId, tasks],
  );

  useEffect(() => {
    handleTaskEventRef.current = handleTaskEvent;
  }, [handleTaskEvent]);

  useWebSocketEvent(
    "task_event",
    useCallback((data: Record<string, unknown>) => {
      if (data.event && (data.task || data.task_id)) {
        handleTaskEventRef.current(
          data.event as string,
          (data.task || { id: data.task_id }) as Record<string, unknown>,
        );
      }
    }, []),
  );

  // Fetch task detail when selected
  useEffect(() => {
    if (!selectedTaskId) {
      setTaskDetail(null);
      return;
    }
    const controller = new AbortController();
    setDetailLoading(true);
    const baseUrl = getBaseUrl();
    fetch(`${baseUrl}/api/tasks/${selectedTaskId}`, {
      signal: controller.signal,
    })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        const raw = extractTaskPayload(data);
        const cached = tasks.find((task) => task.id === selectedTaskId) ?? null;
        setTaskDetail(raw ? (normalizeActivityTask(raw, cached) as GobbyTaskDetail) : null);
      })
      .catch((err) => {
        if (err.name !== "AbortError") setTaskDetail(null);
      })
      .finally(() => {
        if (!controller.signal.aborted) setDetailLoading(false);
      });
    return () => controller.abort();
  }, [selectedTaskId, tasks]);

  // Fetch dependencies + subtasks alongside the detail. Each call uses its own
  // controller so a stale response from a previous selection can't overwrite
  // the current panel.
  useEffect(() => {
    if (!selectedTaskId) {
      setTaskDependencies(null);
      setTaskSubtasks([]);
      return;
    }
    const controllerDeps = new AbortController();
    const controllerSubtasks = new AbortController();
    const baseUrl = getBaseUrl();
    fetch(
      `${baseUrl}/api/tasks/${selectedTaskId}/dependencies?direction=both`,
      { signal: controllerDeps.signal },
    )
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => setTaskDependencies(data ?? null))
      .catch((err) => {
        if (err.name !== "AbortError") setTaskDependencies(null);
      });
    fetch(
      `${baseUrl}/api/tasks?parent_task_id=${selectedTaskId}&limit=200&include_stages=1`,
      { signal: controllerSubtasks.signal },
    )
      .then((res) => (res.ok ? res.json() : null))
      .then((data) =>
        setTaskSubtasks(normalizeTaskPayloads(data?.tasks ?? []) as GobbyTask[]),
      )
      .catch((err) => {
        if (err.name !== "AbortError") setTaskSubtasks([]);
      });
    return () => {
      controllerDeps.abort();
      controllerSubtasks.abort();
    };
  }, [selectedTaskId]);

  const handleFiltersApply = useCallback(
    (nextFilters: Set<TaskFilterKey>, nextStages: Set<string>) => {
      setStatusFilters(nextFilters);
      setSelectedStageFilters(nextStages);
    },
    [],
  );

  // Client-side filter + display ordering. The activity Tasks tree should read
  // like a prioritized work queue: highest priority first, then oldest first.
  const filtered = useMemo(() => {
    const shouldApplyStageFilter =
      defaultStageFilters.size > 0 && !stageSelectionMatchesDefault;
    const matchingTasks = tasks.filter((task) => {
      if (!matchesTaskFilter(task, statusFilters)) return false;
      if (!shouldApplyStageFilter) return true;
      const stageName = getCurrentStageName(task);
      return stageName !== null && selectedStageFilters.has(stageName);
    });
    const recentClosedIds = new Set(
      matchingTasks
        .filter((task) => getTaskDisplayState(task) === "closed")
        .sort((a, b) => {
          const closedAtA = getCanonicalTaskState(a).closed_at ?? a.updated_at ?? "";
          const closedAtB = getCanonicalTaskState(b).closed_at ?? b.updated_at ?? "";
          return closedAtB.localeCompare(closedAtA);
        })
        .slice(0, RECENT_CLOSED_TASK_LIMIT)
        .map((task) => task.id),
    );

    return matchingTasks
      .filter((task) => {
        if (getTaskDisplayState(task) !== "closed") {
          return true;
        }
        return recentClosedIds.has(task.id);
      })
      .sort(compareTasksForDisplay);
  }, [
    defaultStageFilters,
    selectedStageFilters,
    stageSelectionMatchesDefault,
    statusFilters,
    tasks,
  ]);

  const treeData = useMemo(() => {
    const taskMap = new Map(tasks.map((task) => [task.id, task]));
    const visibleIds = new Set<string>();

    for (const task of filtered) {
      let current: GobbyTask | undefined = task;
      while (current) {
        if (visibleIds.has(current.id)) break;
        visibleIds.add(current.id);
        current = current.parent_task_id
          ? taskMap.get(current.parent_task_id)
          : undefined;
      }
    }

    const visibleTasks = tasks.filter((task) => visibleIds.has(task.id));
    return buildTree(visibleTasks);
  }, [filtered, tasks]);

  const searchableTreeData = useMemo(
    () => filterTreeBySearch(treeData, search),
    [treeData, search],
  );
  const searchableExpandableIds = useMemo(
    () => collectExpandableNodeIds(searchableTreeData),
    [searchableTreeData],
  );

  useEffect(() => {
    setCollapsedTaskIds((prev) => {
      if (prev.size === 0) {
        return prev;
      }

      const next = new Set(
        [...prev].filter((id) => searchableExpandableIds.has(id)),
      );

      return next.size === prev.size ? prev : next;
    });
  }, [searchableExpandableIds]);

  const visibleRows = useMemo(
    () =>
      collectVisibleTaskRows(
        searchableTreeData,
        collapsedTaskIds,
        0,
        search.trim().length > 0,
      ),
    [search, searchableTreeData, collapsedTaskIds],
  );

  const selectedTaskSummary = useMemo(
    () =>
      selectedTaskId
        ? tasks.find((task) => task.id === selectedTaskId) ?? null
        : null,
    [selectedTaskId, tasks],
  );
  const headerRef = taskDetail?.ref ?? selectedTaskSummary?.ref ?? null;
  const headerTitle = taskDetail?.title ?? selectedTaskSummary?.title ?? null;
  let parentTask: ParentTaskRef | null = null;
  if (taskDetail?.parent_task_id) {
    const parent = tasks.find((t) => t.id === taskDetail.parent_task_id);
    if (parent) {
      parentTask = { id: parent.id, ref: parent.ref, title: parent.title };
    }
  }

  useEffect(() => {
    selectedTaskIdRef.current = selectedTaskId;
  }, [selectedTaskId]);

  useEffect(() => {
    if (visibleRows.length === 0) {
      if (selectedTaskIdRef.current !== null) {
        setSelectedTaskId(null);
      }
      userSelectedRef.current = false;
      return;
    }

    const hasVisibleSelection = visibleRows.some(
      (row) => row.node.task.id === selectedTaskIdRef.current,
    );
    if (!hasVisibleSelection) {
      userSelectedRef.current = false;
      if (selectedTaskIdRef.current !== null) {
        setSelectedTaskId(null);
        return;
      }
      setSelectedTaskId(visibleRows[0].node.task.id);
    }
  }, [visibleRows]);

  const closeTaskMenu = useCallback(() => setTaskMenu(null), []);

  const applyRawTaskUpdate = useCallback(
    (taskId: string, rawTask: RawTaskPayload | null) => {
      if (!rawTask) return;
      setTasks((prev) =>
        prev.map((task) =>
          task.id === taskId ? normalizeActivityTask(rawTask, task) : task,
        ),
      );
      if (selectedTaskId === taskId) {
        setTaskDetail((prev) =>
          normalizeActivityTask(rawTask, prev ?? undefined) as GobbyTaskDetail,
        );
      }
    },
    [selectedTaskId],
  );

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
        applyRawTaskUpdate(task.id, rawTask);
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
    [applyRawTaskUpdate, closeTaskMenu, fetchTasks],
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
      const menuWidth = 180;
      const candidateX = rect.left - menuWidth;
      const x = Math.max(0, Math.min(candidateX, window.innerWidth - menuWidth));
      setTaskMenu({
        x,
        y: rect.top,
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
        () => postTaskLifecycleAction(getBaseUrl(), task.id, "close", { reason }),
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

  const toggleTaskOpen = useCallback((taskId: string) => {
    setCollapsedTaskIds((prev) => {
      const next = new Set(prev);
      if (next.has(taskId)) {
        next.delete(taskId);
      } else {
        next.add(taskId);
      }
      return next;
    });
  }, []);

  if (loading) {
    return <ActivityPanelEmpty body="Loading tasks…" />;
  }

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* Toolbar */}
      <div className="activity-panel-toolbar">
        <ActivityPanelSearch
          value={search}
          onChange={setSearch}
          placeholder="Search"
        />
        <button
          type="button"
          className="btn btn-accent btn-sm activity-panel-action-btn activity-filter-button"
          onClick={() => setShowFilterDropdown((v) => !v)}
          title="Filter by task state"
          aria-label="Filter tasks"
          aria-expanded={showFilterDropdown}
        >
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3" />
          </svg>
          <span className="activity-panel-action-btn__label">Filter</span>
          {activeFilterCount > 0 && (
            <span className="activity-filter-badge">{activeFilterCount}</span>
          )}
        </button>
        {showFilterDropdown && (
          <TasksTabFilters
            filters={statusFilters}
            stages={stagesRegistry}
            selectedStages={selectedStageFilters}
            onApply={handleFiltersApply}
            onClose={() => setShowFilterDropdown(false)}
          />
        )}
      </div>
      {actionError && (
        <div
          className="px-2.5 py-1.5 border-b border-border text-xs"
          role="alert"
          style={{ color: "var(--color-error)" }}
        >
          {actionError}
        </div>
      )}

      {/* Tree pane */}
      <div
        className={`activity-tasks-pane min-h-0 overflow-y-auto ${selectedTaskId ? "border-b border-border" : "flex-1"}`}
        style={selectedTaskId ? { height: `${topHeight}%` } : undefined}
        role={filtered.length > 0 ? "tree" : undefined}
        aria-label={filtered.length > 0 ? "Tasks" : undefined}
        data-testid={filtered.length > 0 ? "task-tree" : undefined}
      >
        {filtered.length === 0 ? (
          <ActivityPanelEmpty
            icon={<TasksEmptyIcon />}
            heading="Tasks"
            body={
              tasks.length > 0
                ? "Tasks exist, but none match the current filters"
                : "Tasks appear here as they are created"
            }
          />
        ) : (
          <>
            {visibleRows.map((row) => {
              const task = row.node.task;
              return (
                <TaskTreeRow
                  key={task.id}
                  row={row}
                  isSelected={selectedTaskId === task.id}
                  isBusy={activeTaskAction?.taskId === task.id}
                  onSelect={(taskId) => {
                    userSelectedRef.current = true;
                    setActionError(null);
                    setSelectedTaskId(taskId);
                  }}
                  onToggleOpen={toggleTaskOpen}
                  onMenuButtonClick={handleMenuButtonClick}
                />
              );
            })}
          </>
        )}
      </div>

      {/* Resize handle */}
      {selectedTaskId && (
        <ResizeHandle
          direction="vertical"
          onResize={setTopHeight}
          panelHeight={topHeight}
          minHeight={15}
          maxHeight={80}
        />
      )}

      {/* Detail pane */}
      {selectedTaskId && (
        <div className="activity-task-detail-shell">
          <div className="activity-task-pane-bar activity-task-pane-bar--detail">
            <span className="activity-task-pane-bar__title">
              Task {headerRef ?? "—"}
              {headerTitle ? <> – {headerTitle}</> : null}
            </span>
            {taskDetail && (
              <div className="activity-task-pane-bar__chips">
                <TaskStateBadges task={taskDetail} />
                <PriorityBadge priority={taskDetail.priority ?? 4} />
                <TypeBadge type={taskDetail.task_type} />
              </div>
            )}
          </div>
          {detailLoading ? (
            <p className="activity-task-detail-loading">
              Loading...
            </p>
          ) : taskDetail ? (
            <TasksTabDetailPanel
              task={taskDetail}
              parentTask={parentTask}
              onSelectTask={setSelectedTaskId}
              dependencies={taskDependencies}
              subtasks={taskSubtasks}
            />
          ) : (
            <p className="activity-task-detail-empty">
              Task not found
            </p>
          )}
        </div>
      )}

      {taskMenu && (
        <TaskQuickMenu
          menu={taskMenu}
          chatSessionId={chatSessionId}
          activeAction={activeTaskAction}
          onClose={closeTaskMenu}
          onAssignToMainChat={handleAssignToMainChat}
          onBuild={handleBuild}
          onBuildQuick={handleBuildQuick}
          onStopBuild={handleStopBuild}
          onResumeBuild={handleResumeBuild}
          onReleaseClaim={handleReleaseClaim}
          onCloseTask={handleOpenCloseTaskDialog}
          onReopenTask={handleReopenTask}
        />
      )}
      <TaskCloseDialog
        key={closeDialogTask?.id ?? "none"}
        task={closeDialogTask}
        isSubmitting={activeTaskAction?.action === "close"}
        onCancel={() => setCloseDialogTask(null)}
        onConfirm={handleCloseTask}
      />
    </div>
  );
});
