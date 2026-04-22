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
import { Markdown } from "../chat/Markdown";
import { useWebSocketEvent } from "../../hooks/useWebSocketEvent";
import "../tasks/task-execution.css";
import type { GobbyTask } from "../../hooks/useTasks";
import { PriorityBadge, TaskStateBadges, TypeBadge } from "../tasks/TaskBadges";
import {
  getCanonicalTaskState,
  getTaskBucket,
  TASK_BUCKET_COLORS,
  TASK_BUCKET_LABELS,
  type TaskBucket,
} from "../../lib/taskState";

interface TasksTabProps {
  projectId?: string | null;
  chatSessionId?: string | null;
}

interface GobbyTaskDetail extends GobbyTask {
  description: string | null;
  category: string | null;
  validation_criteria: string | null;
  closed_at: string | null;
}

// =============================================================================
// Tree node type (mirrors TaskTree.tsx)
// =============================================================================

interface TreeNode {
  id: string;
  task: GobbyTask;
  children: TreeNode[];
}

interface VisibleTaskRow {
  node: TreeNode;
  depth: number;
  isInternal: boolean;
  isOpen: boolean;
}

type TaskFilterKey = TaskBucket | "escalated";

interface TaskContextMenu {
  x: number;
  y: number;
  task: GobbyTask;
}

// =============================================================================
// Constants
// =============================================================================

const LIFECYCLE_BUCKETS: TaskBucket[] = [
  "ready",
  "in_progress",
  "review",
  "merge_ready",
];
const STATUS_FILTERS: TaskFilterKey[] = ["blocked", "escalated", "closed"];
const DEFAULT_FILTERS = new Set<TaskFilterKey>([
  ...LIFECYCLE_BUCKETS,
  "blocked",
  "escalated",
]);
const RECENT_CLOSED_TASK_LIMIT = 20;

const STATUS_DOT_COLORS = TASK_BUCKET_COLORS;

const PRIORITY_TEXT_COLORS: Record<number, string> = {
  0: "var(--status-escalated, #ef4444)",
  1: "var(--status-escalated, #ef4444)",
  2: "var(--status-progress, #f59e0b)",
  3: "var(--text-secondary, #a3a3a3)",
  4: "var(--text-muted, #737373)",
};

function getBaseUrl(): string {
  return import.meta.env.VITE_API_BASE_URL || "";
}

function compareTasksForDisplay(a: GobbyTask, b: GobbyTask): number {
  const priorityDiff = (a.priority ?? 4) - (b.priority ?? 4);
  if (priorityDiff !== 0) {
    return priorityDiff;
  }

  const seqA = a.seq_num ?? Number.MAX_SAFE_INTEGER;
  const seqB = b.seq_num ?? Number.MAX_SAFE_INTEGER;
  if (seqA !== seqB) {
    return seqA - seqB;
  }

  const createdAtDiff = (a.created_at ?? "").localeCompare(b.created_at ?? "");
  if (createdAtDiff !== 0) {
    return createdAtDiff;
  }

  return (a.updated_at ?? "").localeCompare(b.updated_at ?? "");
}

function formatTaskDetailDate(iso: string | null | undefined): string {
  if (!iso) {
    return "—";
  }

  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) {
    return "—";
  }

  return `${parsed.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  })} ${parsed.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  })}`;
}

// =============================================================================
// Build tree from flat task list (same logic as TaskTree.tsx)
// =============================================================================

function buildTree(tasks: GobbyTask[]): TreeNode[] {
  const nodeMap = new Map<string, TreeNode>();
  const roots: TreeNode[] = [];

  for (const task of tasks) {
    nodeMap.set(task.id, { id: task.id, task, children: [] });
  }

  for (const task of tasks) {
    const node = nodeMap.get(task.id)!;
    if (task.parent_task_id && nodeMap.has(task.parent_task_id)) {
      nodeMap.get(task.parent_task_id)!.children.push(node);
    } else {
      roots.push(node);
    }
  }

  for (const node of nodeMap.values()) {
    node.children.sort((left, right) =>
      compareTasksForDisplay(left.task, right.task),
    );
  }
  roots.sort((left, right) => compareTasksForDisplay(left.task, right.task));

  return roots;
}

function taskMatchesSearch(task: GobbyTask, term: string): boolean {
  const lower = term.toLowerCase();
  return (
    task.title.toLowerCase().includes(lower) ||
    task.ref.toLowerCase().includes(lower)
  );
}

function filterTreeBySearch(nodes: TreeNode[], term: string): TreeNode[] {
  const trimmed = term.trim();
  if (!trimmed) {
    return nodes;
  }

  const visit = (node: TreeNode): TreeNode | null => {
    const children = node.children
      .map(visit)
      .filter((child): child is TreeNode => child !== null);

    if (!taskMatchesSearch(node.task, trimmed) && children.length === 0) {
      return null;
    }

    return {
      ...node,
      children,
    };
  };

  return nodes
    .map(visit)
    .filter((node): node is TreeNode => node !== null);
}

function collectExpandableNodeIds(
  nodes: TreeNode[],
  ids: Set<string> = new Set(),
): Set<string> {
  for (const node of nodes) {
    if (node.children.length > 0) {
      ids.add(node.id);
      collectExpandableNodeIds(node.children, ids);
    }
  }
  return ids;
}

function collectVisibleTaskRows(
  nodes: TreeNode[],
  collapsedIds: Set<string>,
  depth = 0,
  forceOpen = false,
): VisibleTaskRow[] {
  return nodes.flatMap((node) => {
    const isInternal = node.children.length > 0;
    const isOpen = forceOpen || !collapsedIds.has(node.id);
    const row: VisibleTaskRow = {
      node,
      depth,
      isInternal,
      isOpen,
    };

    if (!isInternal || !isOpen) {
      return [row];
    }

    return [
      row,
      ...collectVisibleTaskRows(node.children, collapsedIds, depth + 1, forceOpen),
    ];
  });
}

function getTaskFilterLabel(filter: TaskFilterKey): string {
  if (filter === "escalated") {
    return "Escalated";
  }
  return TASK_BUCKET_LABELS[filter];
}

function getTaskFilterColor(filter: TaskFilterKey): string {
  if (filter === "escalated") {
    return "var(--status-escalated, #ef4444)";
  }
  return STATUS_DOT_COLORS[filter] ?? "#737373";
}

function matchesTaskFilter(task: GobbyTask, filters: Set<TaskFilterKey>): boolean {
  const state = getCanonicalTaskState(task);
  if (state.is_closed) return filters.has("closed");
  if (state.is_escalated) return filters.has("escalated");
  const bucket = getTaskBucket(task);
  return filters.has(bucket);
}

// =============================================================================
// Filter dropdown
// =============================================================================

function FilterDropdown({
  filters,
  onToggle,
  onClose,
}: {
  filters: Set<TaskFilterKey>;
  onToggle: (status: TaskFilterKey) => void;
  onClose: () => void;
}) {
  const filterGroups: Array<{ label: string; buckets: TaskFilterKey[] }> = [
    { label: "Lifecycle", buckets: LIFECYCLE_BUCKETS },
    { label: "Status", buckets: STATUS_FILTERS },
  ];

  return (
    <>
      <div className="fixed inset-0 z-[99]" onClick={onClose} />
      <div
        className="absolute top-full right-2 z-[100] border border-border rounded-md shadow-xl p-1.5 flex flex-col gap-0.5 min-w-[10rem]"
        style={{ background: "var(--bg-secondary)" }}
      >
        {filterGroups.map((group) => (
          <div key={group.label} className="flex flex-col gap-0.5 py-0.5">
            <div className="px-2 py-1 text-[10px] font-medium uppercase tracking-wide text-muted-foreground/80">
              {group.label}
            </div>
            {group.buckets.map((status) => (
              <label
                key={status}
                className="flex items-center gap-1.5 px-2 py-1 rounded text-xs text-muted-foreground cursor-pointer hover:bg-muted/50"
              >
                <input
                  type="checkbox"
                  className="w-3 h-3"
                  checked={filters.has(status)}
                  onChange={() => onToggle(status)}
                />
                <span
                  className="w-1.5 h-1.5 rounded-full shrink-0"
                  style={{
                    backgroundColor: getTaskFilterColor(status),
                  }}
                />
                <span>{getTaskFilterLabel(status)}</span>
              </label>
            ))}
          </div>
        ))}
      </div>
    </>
  );
}

// =============================================================================
// TasksTab
// =============================================================================

export const TasksTab = memo(function TasksTab({
  projectId,
  chatSessionId,
}: TasksTabProps) {
  const [tasks, setTasks] = useState<GobbyTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [statusFilters, setStatusFilters] = useState<Set<TaskFilterKey>>(
    () => new Set(DEFAULT_FILTERS),
  );
  const [showFilterDropdown, setShowFilterDropdown] = useState(false);
  const [topHeight, setTopHeight] = useState(50);
  const [taskDetail, setTaskDetail] = useState<GobbyTaskDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [assigningTaskId, setAssigningTaskId] = useState<string | null>(null);
  const [claimError, setClaimError] = useState<string | null>(null);
  const [taskMenu, setTaskMenu] = useState<TaskContextMenu | null>(null);
  const [collapsedTaskIds, setCollapsedTaskIds] = useState<Set<string>>(
    () => new Set(),
  );

  // Fetch tasks, then apply canonical bucket filters client-side.
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
    fetch(`${baseUrl}/api/tasks?${params}`, { signal: controller.signal })
      .then((res) => (res.ok ? res.json() : { tasks: [] }))
      .then((data) => setTasks(data.tasks ?? []))
      .catch((err) => {
        if (err.name !== "AbortError") setTasks([]);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
  }, [projectId]);

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
        const newTask = taskData as unknown as GobbyTask;
        setTasks((prev) => {
          if (prev.some((t) => t.id === taskId)) return prev;
          return [...prev, newTask];
        });
      } else {
        // task_updated, task_closed, task_reopened, task_de_escalated
        const updated = taskData as unknown as GobbyTask;
        setTasks((prev) =>
          prev.map((t) => (t.id === taskId ? { ...t, ...updated } : t)),
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
            setTaskDetail(data?.id ? data : (data?.task ?? null));
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
    [fetchTasks, projectId, selectedTaskId],
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
      .then((data) => setTaskDetail(data?.id ? data : (data?.task ?? null)))
      .catch((err) => {
        if (err.name !== "AbortError") setTaskDetail(null);
      })
      .finally(() => {
        if (!controller.signal.aborted) setDetailLoading(false);
      });
    return () => controller.abort();
  }, [selectedTaskId]);

  const toggleFilter = useCallback((status: TaskFilterKey) => {
    setStatusFilters((prev) => {
      const next = new Set(prev);
      if (next.has(status)) next.delete(status);
      else next.add(status);
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
        .filter((task) => getTaskBucket(task) === "closed")
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
        if (getTaskBucket(task) !== "closed") {
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
      setTasks((prev) =>
        prev.map((task) =>
          task.id === taskId ? { ...task, ...(claimedTask?.task ?? claimedTask) } : task,
        ),
      );
      if (selectedTaskId === taskId) {
        setTaskDetail((claimedTask?.task ?? claimedTask) as GobbyTaskDetail);
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
      const dotColor = taskState.is_escalated
        ? getTaskFilterColor("escalated")
        : STATUS_DOT_COLORS[getTaskBucket(task)] ?? "#737373";
      const textColor =
        PRIORITY_TEXT_COLORS[task.priority ?? 3] ?? "var(--text-secondary)";
      const ref = task.seq_num != null ? `#${task.seq_num}` : null;
      const isAssigning = assigningTaskId === task.id;
      const isSelected = selectedTaskId === task.id;

      const taskRowClass = [
        "activity-task-row",
        isSelected && "activity-task-row--selected",
        getTaskBucket(task) === "closed" && "activity-task-row--closed",
      ]
        .filter(Boolean)
        .join(" ");

      return (
        <div
          key={task.id}
          style={{ paddingLeft: `${row.depth * 1.25 + 0.75}rem` }}
          className={taskRowClass}
          role="treeitem"
          aria-level={row.depth + 1}
          aria-expanded={row.isInternal ? row.isOpen : undefined}
          onClick={() => {
            userSelectedRef.current = true;
            setClaimError(null);
            setSelectedTaskId(task.id);
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
          <span
            className="activity-task-row-dot"
            style={{ backgroundColor: dotColor }}
          />
          {ref && (
            <span className="activity-task-row-ref">{ref}</span>
          )}
          <span
            className="activity-task-row-title"
            style={{ color: textColor }}
          >
            {task.title}
          </span>
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
    return (
      <div className="activity-tab-empty">
        <p>Loading tasks...</p>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* Toolbar */}
      <div className="activity-task-pane-bar activity-task-pane-bar--toolbar relative">
        <input
          type="text"
          className="activity-task-search"
          placeholder="Search..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <button
          type="button"
          className="activity-task-filter-button"
          onClick={() => setShowFilterDropdown((v) => !v)}
          title="Filter by task state"
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
          >
            <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3" />
          </svg>
        </button>
        {showFilterDropdown && (
          <FilterDropdown
            filters={statusFilters}
            onToggle={toggleFilter}
            onClose={() => setShowFilterDropdown(false)}
          />
        )}
      </div>
      {claimError && (
        <div
          className="px-2.5 py-1.5 border-b border-border text-xs"
          role="alert"
          style={{ color: "var(--status-escalated, #ef4444)" }}
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
          <div className="activity-tab-empty">
            <p>No tasks match filters</p>
            {tasks.length > 0 && (
              <p className="text-xs text-muted-foreground mt-1">
                Tasks exist, but none match the current task-state filters.
              </p>
            )}
          </div>
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
            <TaskDetail task={taskDetail} />
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

// =============================================================================
// Task detail panel (extracted from former accordion)
// =============================================================================

function TaskDetail({ task }: { task: GobbyTaskDetail }) {
  const taskState = getCanonicalTaskState(task);
  const ownerLabel = task.agent_name ?? taskState.owner_session_id ?? "Unassigned";
  const ownerMono = !task.agent_name && Boolean(taskState.owner_session_id);
  const stateLabel = TASK_BUCKET_LABELS[getTaskBucket(task)];
  const categoryLabel = task.category ?? task.task_type;

  return (
    <div className="activity-task-detail-card">
      <div className="activity-task-detail-meta">
        <TaskDetailMetaRow
          label="Claimed by"
          value={ownerLabel}
          mono={ownerMono}
          title="Agent or session currently holding this task's claim"
        />
        <TaskDetailMetaRow label="State" value={stateLabel} />
        <TaskDetailMetaRow label="Created" value={formatTaskDetailDate(task.created_at)} />
        <TaskDetailMetaRow label="Updated" value={formatTaskDetailDate(task.updated_at)} />
        <TaskDetailMetaRow label="Category" value={categoryLabel} />
        {task.path_cache && <TaskDetailMetaRow label="Path" value={task.path_cache} mono />}
        {task.closed_at && (
          <TaskDetailMetaRow
            label="Closed"
            value={formatTaskDetailDate(task.closed_at)}
          />
        )}
      </div>

      {task.description && (
        <div className="activity-task-detail-section">
          <div className="activity-task-detail-section-title">Description</div>
          <div className="activity-task-detail-markdown message-content">
            <Markdown content={task.description} id={`task-desc-${task.id}`} />
          </div>
        </div>
      )}

      {task.validation_criteria && (
        <div className="activity-task-detail-section">
          <div className="activity-task-detail-section-title">Validation</div>
          <div className="activity-task-detail-markdown message-content">
            <Markdown
              content={task.validation_criteria}
              id={`task-vc-${task.id}`}
            />
          </div>
        </div>
      )}
    </div>
  );
}

function TaskDetailMetaRow({
  label,
  value,
  mono = false,
  title,
}: {
  label: string;
  value: string;
  mono?: boolean;
  title?: string;
}) {
  return (
    <div className="activity-task-detail-meta-row" title={title}>
      <span className="activity-task-detail-meta-label">{label}</span>
      <span
        className={`activity-task-detail-meta-value${
          mono ? " activity-task-detail-meta-value--mono" : ""
        }`}
      >
        {value}
      </span>
    </div>
  );
}
