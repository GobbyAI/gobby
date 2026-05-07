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
import "../tasks/task-execution.css";
import type { DependencyTree, GobbyTask } from "../../hooks/useTasks";
import { PriorityBadge, StatusDot, TaskStateBadges, TypeBadge } from "../tasks/TaskBadges";
import {
  getCanonicalTaskState,
  getTaskDisplayState,
  getTaskStateSummary,
} from "../../lib/taskState";
import {
  normalizeTaskPayload,
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
  getStageStateColor,
  matchesTaskFilter,
  PRIORITY_TEXT_COLORS,
  PRIORITY_TEXT_WEIGHTS,
  RECENT_CLOSED_TASK_LIMIT,
  type TaskFilterKey,
  type VisibleTaskRow,
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

interface TasksTabProps {
  projectId?: string | null;
  chatSessionId?: string | null;
}

interface TaskContextMenu {
  x: number;
  y: number;
  task: GobbyTask;
}

function getBaseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL || "";
}

function extractTaskPayload(data: unknown): RawTaskPayload | null {
  if (!data || typeof data !== "object") return null;
  const record = data as { id?: unknown; task?: unknown };
  if (typeof record.id === "string") return record as RawTaskPayload;
  if (record.task && typeof record.task === "object") {
    return record.task as RawTaskPayload;
  }
  return null;
}

function normalizeActivityTask(raw: RawTaskPayload, fallback?: GobbyTask | null): GobbyTask {
  return normalizeTaskPayload({
    ...fallback,
    ...raw,
    stages: raw.stages ?? fallback?.stages ?? [],
    current_stage:
      raw.current_stage ??
      raw.state?.current_stage ??
      fallback?.current_stage ??
      fallback?.state?.current_stage ??
      null,
  }) as GobbyTask;
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
  const [statusFilters, setStatusFilters] = useState<Set<TaskFilterKey>>(
    () => new Set(DEFAULT_FILTERS),
  );
  const [showFilterDropdown, setShowFilterDropdown] = useState(false);
  const selectedStageSet = useMemo(
    () => new Set(selectedStageFilters),
    [selectedStageFilters],
  );
  const stageQueryKey = useMemo(
    () =>
      selectedStageFilters.size > 0
        ? [...selectedStageFilters].sort().join("\u0000")
        : "",
    [selectedStageFilters],
  );
  const stageQueryList = useMemo(
    () => (stageQueryKey ? stageQueryKey.split("\u0000") : []),
    [stageQueryKey],
  );
  const activeFilterCount = useMemo(() => {
    const symmetricDifference = new Set([...DEFAULT_FILTERS, ...statusFilters]);
    const statusFilterCount = [...symmetricDifference].filter(
      (key) => DEFAULT_FILTERS.has(key) !== statusFilters.has(key),
    ).length;
    return statusFilterCount + selectedStageFilters.size;
  }, [selectedStageFilters, statusFilters]);
  const [topHeight, setTopHeight] = useState(DEFAULT_TOP_PANEL_PERCENT);
  const [taskDetail, setTaskDetail] = useState<GobbyTaskDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [taskDependencies, setTaskDependencies] = useState<DependencyTree | null>(null);
  const [taskSubtasks, setTaskSubtasks] = useState<GobbyTask[]>([]);
  const [assigningTaskId, setAssigningTaskId] = useState<string | null>(null);
  const [claimError, setClaimError] = useState<string | null>(null);
  const [taskMenu, setTaskMenu] = useState<TaskContextMenu | null>(null);
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
    const params = new URLSearchParams();
    if (projectId) params.set("project_id", projectId);
    params.set("limit", "500");
    params.set("sort_by", "updated_at");
    params.set("sort_order", "desc");
    params.set("include_stages", "1");
    if (stageQueryList.length > 0) {
      stageQueryList.forEach((stageName) => params.append("stage", stageName));
    }
    fetch(`${baseUrl}/api/tasks?${params}`, { signal: controller.signal })
      .then((res) => (res.ok ? res.json() : { tasks: [] }))
      .then((data) => setTasks(normalizeTaskPayloads(data.tasks ?? []) as GobbyTask[]))
      .catch((err) => {
        if (err.name !== "AbortError") setTasks([]);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
  }, [projectId, stageQueryList]);

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

  const toggleFilter = useCallback((status: TaskFilterKey) => {
    setStatusFilters((prev) => {
      const next = new Set(prev);
      if (next.has(status)) next.delete(status);
      else next.add(status);
      return next;
    });
  }, []);

  const toggleStageFilter = useCallback((stageName: string) => {
    setSelectedStageFilters((prev) => {
      const next = new Set(prev);
      if (next.has(stageName)) next.delete(stageName);
      else next.add(stageName);
      return next;
    });
  }, []);

  // Client-side filter + display ordering. The activity Tasks tree should read
  // like a prioritized work queue: highest priority first, then oldest first.
  const filtered = useMemo(() => {
    const matchingTasks = tasks.filter((task) =>
      matchesTaskFilter(task, statusFilters),
    );
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
  }, [tasks, statusFilters]);

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

    const visibleTasks = filtered.filter((task) => visibleIds.has(task.id));
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

  const handleAssignToMainChat = useCallback(async () => {
    if (!taskMenu?.task.id || !chatSessionId) {
      return;
    }
    const taskId = taskMenu.task.id;
    closeTaskMenu();
    setAssigningTaskId(taskId);
    setClaimError(null);
    try {
      const response = await fetch(
        `${getBaseUrl()}/api/tasks/${encodeURIComponent(taskId)}/claim`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: chatSessionId, force: true }),
        },
      );
      if (!response.ok) {
        throw new Error(`Failed to claim task (${response.status})`);
      }
      const claimedTask = await response.json();
      const rawClaimedTask = extractTaskPayload(claimedTask);
      setTasks((prev) =>
        prev.map((task) =>
          task.id === taskId && rawClaimedTask
            ? normalizeActivityTask(rawClaimedTask, task)
            : task,
        ),
      );
      if (selectedTaskId === taskId && rawClaimedTask) {
        setTaskDetail((prev) =>
          normalizeActivityTask(rawClaimedTask, prev ?? undefined) as GobbyTaskDetail,
        );
      }
    } catch (error) {
      setClaimError(
        error instanceof Error
          ? `Failed to assign task to main chat: ${error.message}`
          : "Failed to assign task to main chat.",
      );
    } finally {
      setAssigningTaskId(null);
    }
  }, [chatSessionId, closeTaskMenu, selectedTaskId, taskMenu]);

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

  const renderTaskRow = useCallback(
    (row: VisibleTaskRow) => {
      const task = row.node.task;
      const taskState = getCanonicalTaskState(task);
      const currentStage = taskState.current_stage;
      const stateSummary = getTaskStateSummary(task);
      const textColor =
        PRIORITY_TEXT_COLORS[task.priority ?? 3] ?? "var(--text-secondary)";
      const textWeight =
        PRIORITY_TEXT_WEIGHTS[task.priority ?? 3] ?? "var(--font-weight-normal)";
      const ref = task.seq_num != null ? `#${task.seq_num}` : null;
      const isAssigning = assigningTaskId === task.id;
      const isSelected = selectedTaskId === task.id;

      const taskRowClass = [
        "activity-task-row",
        isSelected && "activity-task-row--selected",
        getTaskDisplayState(task) === "closed" && "activity-task-row--closed",
      ]
        .filter(Boolean)
        .join(" ");

      return (
        <div
          key={task.id}
          style={{ paddingLeft: `${row.depth * 1.25 + 0.75}rem` }}
          className={taskRowClass}
          role="treeitem"
          tabIndex={0}
          aria-level={row.depth + 1}
          aria-expanded={row.isInternal ? row.isOpen : undefined}
          aria-label={`${ref ?? task.ref} ${task.title}: ${stateSummary}`}
          title={stateSummary}
          onClick={() => {
            userSelectedRef.current = true;
            setClaimError(null);
            setSelectedTaskId(task.id);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              userSelectedRef.current = true;
              setClaimError(null);
              setSelectedTaskId(task.id);
            }
          }}
        >
          {row.isInternal ? (
            <button
              className="activity-task-row-toggle"
              onClick={(e) => {
                e.stopPropagation();
                toggleTaskOpen(task.id);
              }}
              aria-label={`${
                row.isOpen ? "Collapse" : "Expand"
              } subtasks for ${task.title}`}
              title={row.isOpen ? "Collapse subtasks" : "Expand subtasks"}
            >
              <span
                className={`activity-task-row-toggle-icon${
                  row.isOpen ? " activity-task-row-toggle-icon--open" : ""
                }`}
                aria-hidden="true"
              >
                <svg viewBox="0 0 12 12" fill="none">
                  <path
                    d="M4 2.5L8 6L4 9.5"
                    stroke="currentColor"
                    strokeWidth="1.9"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </span>
            </button>
          ) : (
            <span className="activity-task-row-toggle-spacer" aria-hidden="true" />
          )}
          <StatusDot task={task} />
          {ref && (
            <span className="activity-task-row-ref">{ref}</span>
          )}
          <span
            className="activity-task-row-title"
            style={{ color: textColor, fontWeight: textWeight }}
          >
            {task.title}
          </span>
          {currentStage && (
            <span className="activity-task-row-stage" title={stateSummary}>
              <span
                className="activity-task-row-stage-pip"
                style={{ backgroundColor: getStageStateColor(currentStage.state) }}
                aria-hidden="true"
              />
              <span className="activity-task-row-stage-label">
                {currentStage.display_name}
              </span>
            </span>
          )}
          <button
            type="button"
            className="session-more-btn"
            onClick={(event) => handleMenuButtonClick(event, task)}
            title="Task actions"
            aria-label="Task actions"
            disabled={isAssigning}
          >
            <svg
              width="12"
              height="12"
              viewBox="0 0 24 24"
              fill="currentColor"
            >
              <circle cx="12" cy="5" r="2" />
              <circle cx="12" cy="12" r="2" />
              <circle cx="12" cy="19" r="2" />
            </svg>
          </button>
        </div>
      );
    },
    [
      assigningTaskId,
      handleMenuButtonClick,
      selectedTaskId,
      toggleTaskOpen,
    ],
  );

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
          className="activity-filter-button"
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
          {activeFilterCount > 0 && (
            <span className="activity-filter-badge">{activeFilterCount}</span>
          )}
        </button>
        {showFilterDropdown && (
          <TasksTabFilters
            filters={statusFilters}
            stages={stagesRegistry}
            selectedStages={selectedStageSet}
            onToggle={toggleFilter}
            onToggleStage={toggleStageFilter}
            onClose={() => setShowFilterDropdown(false)}
          />
        )}
      </div>
      {claimError && (
        <div
          className="px-2.5 py-1.5 border-b border-border text-xs"
          role="alert"
          style={{ color: "var(--color-error)" }}
        >
          {claimError}
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
            {visibleRows.map((row) => renderTaskRow(row))}
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
        <>
          <div className="session-ctx-backdrop" onClick={closeTaskMenu} />
          <div
            className="session-ctx-menu"
            style={{ position: "fixed", left: taskMenu.x, top: taskMenu.y }}
          >
            <button
              className="session-ctx-item"
              onClick={() => {
                void handleAssignToMainChat();
              }}
              disabled={!chatSessionId || assigningTaskId === taskMenu.task.id}
            >
              Assign to Main Chat
            </button>
          </div>
        </>
      )}
    </div>
  );
});
