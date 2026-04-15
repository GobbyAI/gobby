import { memo, useState, useEffect, useCallback, useRef, useMemo } from "react";
import { Tree, type NodeRendererProps, type TreeApi } from "react-arborist";
import { ResizeHandle } from "../chat/artifacts/ResizeHandle";
import { Markdown } from "../chat/Markdown";
import { useNow } from "../../hooks/useNow";
import { useWebSocketEvent } from "../../hooks/useWebSocketEvent";
import "../tasks/task-execution.css";
import type { GobbyTask } from "../../hooks/useTasks";
import {
  getCanonicalTaskState,
  getTaskBucket,
  getTaskStateSummary,
  TASK_BUCKET_COLORS,
  TASK_BUCKET_LABELS,
  type TaskBucket,
} from "../../lib/taskState";

interface TasksTabProps {
  projectId?: string | null;
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

// =============================================================================
// Constants
// =============================================================================

const LIFECYCLE_BUCKETS: TaskBucket[] = [
  "ready",
  "in_progress",
  "review",
  "merge_ready",
];
const STATUS_BUCKETS: TaskBucket[] = ["blocked", "closed"];
const DEFAULT_FILTERS = new Set<TaskBucket>([...LIFECYCLE_BUCKETS, "blocked"]);
const INITIAL_TASK_LIMIT = 10;
const TASK_ROW_HEIGHT = 30;

const STATUS_DOT_COLORS = TASK_BUCKET_COLORS;

const PRIORITY_LABELS: Record<number, string> = {
  0: "Critical",
  1: "High",
  2: "Medium",
  3: "Low",
  4: "Backlog",
};

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

  return roots;
}

function searchMatch(node: { data: TreeNode }, term: string): boolean {
  const task = node.data.task;
  const lower = term.toLowerCase();
  return (
    task.title.toLowerCase().includes(lower) ||
    task.ref.toLowerCase().includes(lower)
  );
}

// =============================================================================
// Lightweight node renderer for panel tree
// =============================================================================

function PanelTaskNode({ node, style }: NodeRendererProps<TreeNode>) {
  const task = node.data.task;
  const dotColor = STATUS_DOT_COLORS[getTaskBucket(task)] ?? "#737373";
  const textColor =
    PRIORITY_TEXT_COLORS[task.priority ?? 3] ?? "var(--text-secondary)";
  const ref = task.seq_num != null ? `#${task.seq_num}` : null;

  return (
    <div
      style={style}
      className={`flex items-center gap-1.5 px-2.5 py-1.5 cursor-pointer text-sm transition-colors border-b border-border/40 hover:bg-muted/50${node.isSelected ? " bg-accent/[0.06]" : ""}${getTaskBucket(task) === "closed" ? " opacity-50" : ""}`}
      onClick={() => node.activate()}
    >
      {node.isInternal ? (
        <button
          className="bg-transparent border-none text-muted-foreground text-xs cursor-pointer p-0 w-4 shrink-0 text-center leading-none"
          onClick={(e) => {
            e.stopPropagation();
            node.toggle();
          }}
        >
          {node.isOpen ? "▾" : "▸"}
        </button>
      ) : (
        <span className="invisible w-4 shrink-0" />
      )}
      <span
        className="w-1.5 h-1.5 rounded-full shrink-0"
        style={{ backgroundColor: dotColor }}
      />
      {ref && (
        <span className="text-sm text-muted-foreground shrink-0">{ref}</span>
      )}
      <span
        className="truncate min-w-0 flex-1 text-sm text-foreground"
        style={{ color: textColor }}
      >
        {task.title}
      </span>
    </div>
  );
}

// =============================================================================
// Filter dropdown
// =============================================================================

function FilterDropdown({
  filters,
  onToggle,
  onClose,
}: {
  filters: Set<TaskBucket>;
  onToggle: (status: TaskBucket) => void;
  onClose: () => void;
}) {
  const filterGroups: Array<{ label: string; buckets: TaskBucket[] }> = [
    { label: "Lifecycle", buckets: LIFECYCLE_BUCKETS },
    { label: "Status", buckets: STATUS_BUCKETS },
  ];

  return (
    <>
      <div className="fixed inset-0 z-[99]" onClick={onClose} />
      <div className="absolute top-full right-2 z-[100] bg-secondary border border-border rounded-md shadow-lg p-1.5 flex flex-col gap-0.5 min-w-[10rem]">
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
                    backgroundColor: STATUS_DOT_COLORS[status] ?? "#737373",
                  }}
                />
                <span>{TASK_BUCKET_LABELS[status]}</span>
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

export const TasksTab = memo(function TasksTab({ projectId }: TasksTabProps) {
  const [tasks, setTasks] = useState<GobbyTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [statusFilters, setStatusFilters] = useState<Set<TaskBucket>>(
    () => new Set(DEFAULT_FILTERS),
  );
  const [visibleCount, setVisibleCount] = useState(INITIAL_TASK_LIMIT);
  const [showFilterDropdown, setShowFilterDropdown] = useState(false);
  const [topHeight, setTopHeight] = useState(50);
  const [taskDetail, setTaskDetail] = useState<GobbyTaskDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [treeHeight, setTreeHeight] = useState(300);
  const treeRef = useRef<TreeApi<TreeNode> | null>(null);
  // Visible row count, sourced from react-arborist's TreeApi.visibleNodes
  // so it tracks user collapse/expand state. Initialized lazily once the
  // tree mounts; updated on every onToggle.
  const [arboristVisibleCount, setArboristVisibleCount] = useState<
    number | null
  >(null);
  const syncVisibleCount = useCallback(() => {
    const n = treeRef.current?.visibleNodes?.length;
    setArboristVisibleCount(typeof n === "number" ? n : null);
  }, []);

  // Fetch tasks, then apply canonical bucket filters client-side.
  const abortRef = useRef<AbortController | null>(null);
  const debouncedRefetchRef = useRef<number | null>(null);
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
        if (taskId === selectedTaskId) setSelectedTaskId(null);
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

  // ResizeObserver for tree height
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const observer = new ResizeObserver(([entry]) => {
      const available = entry.contentRect.height - 40;
      if (available > 100) setTreeHeight(Math.round(available));
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  const toggleFilter = useCallback((status: TaskBucket) => {
    setStatusFilters((prev) => {
      const next = new Set(prev);
      if (next.has(status)) next.delete(status);
      else next.add(status);
      return next;
    });
  }, []);

  useEffect(() => {
    setVisibleCount(INITIAL_TASK_LIMIT);
  }, [projectId, search, statusFilters]);

  // Client-side filtering
  const now = useNow();
  const DAY_MS = 24 * 60 * 60 * 1000;
  const filtered = useMemo(() => {
    return tasks
      .filter((t) => {
        const bucket = getTaskBucket(t);
        if (!statusFilters.has(bucket)) return false;
        if (bucket !== "closed") return true;
        const closedAt = getCanonicalTaskState(t).closed_at;
        if (!closedAt) return false;
        return now - new Date(closedAt).getTime() < DAY_MS;
      })
      .sort((a, b) => {
        const pa = a.priority ?? 3;
        const pb = b.priority ?? 3;
        if (pa !== pb) return pa - pb;
        return (b.created_at ?? "").localeCompare(a.created_at ?? "");
      });
  }, [tasks, statusFilters, now, DAY_MS]);

  const treeData = useMemo(() => {
    const taskMap = new Map(tasks.map((task) => [task.id, task]));
    const visibleIds = new Set<string>();

    for (const task of filtered.slice(0, visibleCount)) {
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
  }, [filtered, tasks, visibleCount]);

  // Resync the visible row count whenever the tree's data shape changes —
  // this catches mount and any case where filters/search add or remove rows
  // before the user touches the tree manually.
  useEffect(() => {
    setArboristVisibleCount(null);
    const frame = window.requestAnimationFrame(() => {
      syncVisibleCount();
    });

    return () => {
      window.cancelAnimationFrame(frame);
    };
  }, [search, treeData, syncVisibleCount]);

  const hasMore = filtered.length > visibleCount;
  const fallbackVisibleCount = useMemo(
    () => countVisibleNodes(treeData),
    [treeData],
  );
  // Prefer the authoritative count from the TreeApi when the tree has
  // mounted; otherwise use the recursive fallback so initial render still
  // gets a sensible viewport height.
  const visibleTreeRowCount = arboristVisibleCount ?? fallbackVisibleCount;
  const unconstrainedTreeHeight = visibleTreeRowCount * TASK_ROW_HEIGHT;
  const treeViewportHeight = selectedTaskId
    ? undefined
    : Math.max(TASK_ROW_HEIGHT, Math.min(treeHeight, unconstrainedTreeHeight));

  if (loading) {
    return (
      <div className="activity-tab-empty">
        <p>Loading tasks...</p>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      {/* Toolbar */}
      <div className="flex items-center gap-1.5 px-2 py-1.5 border-b border-border bg-secondary relative">
        <input
          type="text"
          className="flex-1 min-w-0 px-2 py-0.5 border border-border rounded bg-background text-foreground text-xs outline-none focus:border-accent transition-colors placeholder:text-muted-foreground"
          placeholder="Search..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <button
          type="button"
          className="flex items-center justify-center bg-transparent border border-border rounded text-muted-foreground cursor-pointer px-1.5 py-0.5 shrink-0 hover:text-foreground hover:border-accent transition-colors"
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

      {/* Tree pane */}
      <div
        ref={containerRef}
        className={`overflow-y-auto ${selectedTaskId ? "border-b border-border" : "flex-1"}`}
        style={selectedTaskId ? { height: `${topHeight}%` } : undefined}
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
            <Tree<TreeNode>
              ref={treeRef}
              data={treeData}
              openByDefault={true}
              width="100%"
              height={treeViewportHeight}
              rowHeight={TASK_ROW_HEIGHT}
              indent={16}
              searchTerm={search}
              searchMatch={searchMatch}
              onActivate={(node) => setSelectedTaskId(node.data.task.id)}
              onToggle={syncVisibleCount}
              disableDrag
              disableDrop
            >
              {PanelTaskNode}
            </Tree>
            {hasMore && (
              <button
                className="w-full py-2 text-xs text-muted-foreground hover:text-foreground hover:bg-muted/30 transition-colors"
                onClick={() =>
                  setVisibleCount((prev) => prev + INITIAL_TASK_LIMIT)
                }
              >
                Load more
              </button>
            )}
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
        <div className="flex-1 flex flex-col min-h-0 overflow-y-auto">
          <div className="flex items-center gap-2 px-2.5 py-1.5 border-b border-border bg-secondary">
            <span className="flex-1 min-w-0 truncate text-sm font-medium">
              {taskDetail ? taskDetail.title : "Loading..."}
            </span>
            <button
              type="button"
              className="bg-transparent border-none text-muted-foreground cursor-pointer text-sm px-1 shrink-0 hover:text-foreground transition-colors"
              onClick={() => setSelectedTaskId(null)}
              title="Close detail"
            >
              ✕
            </button>
          </div>
          {detailLoading ? (
            <p className="text-xs text-muted-foreground px-3 py-2">
              Loading...
            </p>
          ) : taskDetail ? (
            <TaskDetail task={taskDetail} />
          ) : (
            <p className="text-xs text-muted-foreground px-3 py-2">
              Task not found
            </p>
          )}
        </div>
      )}
    </div>
  );
});

// =============================================================================
// Task detail panel (extracted from former accordion)
// =============================================================================

function TaskDetail({ task }: { task: GobbyTaskDetail }) {
  const priorityLabel = PRIORITY_LABELS[task.priority ?? 4] ?? "Backlog";

  return (
    <div className="px-3 py-2 flex flex-col gap-2">
      <div className="flex items-center gap-1 text-xs text-muted-foreground flex-wrap">
        <span className="capitalize">{getTaskStateSummary(task)}</span>
        <span className="opacity-40">{"\u00B7"}</span>
        <span>{priorityLabel}</span>
        {task.task_type !== "task" && (
          <>
            <span className="opacity-40">{"\u00B7"}</span>
            <span>{task.task_type}</span>
          </>
        )}
        {getCanonicalTaskState(task).owner_session_id && (
          <>
            <span className="opacity-40">{"\u00B7"}</span>
            <span>{getCanonicalTaskState(task).owner_session_id}</span>
          </>
        )}
      </div>

      {task.description && (
        <div className="border-t border-border pt-1.5">
          <div className="message-content text-xs">
            <Markdown content={task.description} id={`task-desc-${task.id}`} />
          </div>
        </div>
      )}

      {task.validation_criteria && (
        <div className="border-t border-border pt-1.5">
          <div className="text-[10px] text-muted-foreground uppercase tracking-wide mb-1">
            Validation
          </div>
          <div className="message-content text-xs">
            <Markdown
              content={task.validation_criteria}
              id={`task-vc-${task.id}`}
            />
          </div>
        </div>
      )}

      <div className="text-[10px] text-muted-foreground border-t border-border pt-1.5">
        <span>Created {new Date(task.created_at).toLocaleDateString()}</span>
        {task.closed_at && (
          <span>
            {" "}
            {"\u00B7"} Closed {new Date(task.closed_at).toLocaleDateString()}
          </span>
        )}
      </div>
    </div>
  );
}

function countVisibleNodes(nodes: TreeNode[]): number {
  return nodes.reduce(
    (count, node) => count + 1 + countVisibleNodes(node.children),
    0,
  );
}
