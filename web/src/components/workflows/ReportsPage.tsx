import { useState, useMemo, useCallback, useEffect, useRef } from "react";
import { usePipelineExecutions } from "../../hooks/usePipelineExecutions";
import type { PipelineExecutionRecord } from "../../hooks/usePipelineExecutions";
import { useAgentRuns } from "../../hooks/useAgentRuns";
import type { AgentRunRecord, AgentRunDetail } from "../../hooks/useAgentRuns";
import {
  StepDisplay,
  ChevronIcon,
  AlertIcon,
  PipelineStatusDot as StatusDot,
} from "./execution-utils";
import { formatTime, formatDuration, formatJson } from "./executionFormatters";
import { SegmentedControl } from "../ui/SegmentedControl";
import "./reports-page.css";

// =============================================================================
// Types
// =============================================================================

type SubTab = "pipelines" | "agents";
type StatusFilter = "all" | "running" | "waiting" | "completed" | "failed";
type PipelineSortColumn = "name" | "time" | "duration" | "status";
type AgentSortColumn =
  | "name"
  | "provider"
  | "time"
  | "duration"
  | "turns"
  | "status";
type SortDirection = "asc" | "desc";
type GroupBy = "none" | "name" | "provider";

// =============================================================================
// Class constants — Tailwind migration of reports-page.css
// =============================================================================

const PAGE_CLS = "reports-page flex flex-1 flex-col overflow-hidden px-6 py-4 max-md:p-3";

const TOOLBAR_CLS =
  "flex flex-wrap items-center justify-between gap-4 border-b border-[var(--border)] pb-3 mb-2 max-md:flex-col max-md:items-stretch max-md:gap-2";

const TOOLBAR_LEFT_CLS =
  "flex flex-wrap items-center gap-2 max-md:justify-between max-sm:flex-col max-sm:items-stretch";

const TOOLBAR_RIGHT_CLS = "flex items-center gap-2 max-md:flex-col max-md:gap-2";

const TITLE_CLS = "text-[length:calc(var(--font-size-base)*1.1)] font-semibold mr-1";

const SEARCH_CLS =
  "w-[180px] rounded-md border border-[var(--border)] bg-[var(--bg-tertiary)] px-2.5 py-1.5 font-[inherit] text-[length:calc(var(--font-size-base)*0.8)] text-[var(--text-primary)] placeholder:text-[var(--text-muted)] focus:border-[var(--accent)] focus:outline-none max-md:w-full";

const FILTER_BAR_CLS =
  "flex flex-wrap items-center justify-between gap-3 border-b border-[var(--border)] py-2 mb-2 max-md:flex-col max-md:items-stretch";

const FILTER_CHIPS_CLS =
  "flex flex-wrap gap-1.5 max-md:flex-nowrap max-md:overflow-x-auto max-md:pb-0.5";

const STAT_CHIP_BASE_CLS =
  "inline-flex cursor-pointer items-center gap-1.5 rounded-full border border-[var(--border)] bg-transparent px-2.5 py-0.5 font-[inherit] text-[length:var(--text-sm)] text-[var(--text-secondary)] transition-colors duration-150 hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11";

const STAT_CHIP_ACTIVE_CLS =
  "bg-[var(--bg-tertiary)] border-[var(--accent)] text-[var(--text-primary)]";

const TABLE_CONTAINER_CLS = "flex-1 overflow-y-auto max-sm:overflow-x-visible";

const TABLE_CLS =
  "reports-table w-full border-collapse text-[length:calc(var(--font-size-base)*0.85)]";

const TH_BASE_CLS =
  "reports-th sticky top-0 z-[1] border-b border-[var(--border)] bg-[var(--bg-primary)] px-2.5 py-2 text-left text-[length:calc(var(--font-size-base)*0.7)] font-medium uppercase tracking-[0.05em] text-[var(--text-muted)]";

const TH_SORTABLE_CLS =
  "cursor-pointer select-none whitespace-nowrap hover:text-[var(--text-primary)]";

const TH_ID_CLS = "whitespace-nowrap max-md:hidden";

const ROW_BASE_CLS =
  "cursor-pointer transition-colors duration-100 hover:bg-[var(--bg-tertiary)]";

const ROW_SELECTED_CLS =
  "bg-[color-mix(in_srgb,var(--color-agent)_8%,transparent)] hover:bg-[color-mix(in_srgb,var(--color-agent)_8%,transparent)]";

const CELL_BASE_CLS =
  "reports-cell border-b border-[var(--border)] px-2.5 py-2 whitespace-nowrap";

const CELL_NAME_CLS = "whitespace-normal break-words";

const CELL_ID_CLS =
  "reports-cell--id font-[inherit] text-[length:var(--text-sm)] text-[var(--text-muted)] max-md:hidden";

const CELL_STATUS_CLS =
  "capitalize text-[length:calc(var(--font-size-base)*0.8)] text-[var(--text-secondary)]";

const CELL_DURATION_CLS =
  "reports-cell--duration font-[inherit] text-[length:var(--text-sm)] text-[var(--text-muted)] max-md:hidden";

const CELL_TIME_CLS =
  "text-[length:calc(var(--font-size-base)*0.8)] text-[var(--text-secondary)] max-md:text-[length:calc(var(--font-size-base)*0.7)]";

const STATUS_TEXT_CLS = CELL_STATUS_CLS;

const LOADING_EMPTY_CLS =
  "flex flex-1 items-center justify-center text-[length:calc(var(--font-size-base)*0.9)] text-[var(--text-muted)]";

const TYPE_BADGE_BASE_CLS =
  "reports-type-badge inline-block rounded px-1.5 py-0.5 text-[length:calc(var(--font-size-base)*0.7)] font-medium";

const TYPE_BADGE_AGENT_CLS =
  "reports-type-badge--agent bg-[var(--color-agent-soft)] text-[var(--color-agent)]";

const DETAIL_BACKDROP_CLS =
  "fixed inset-0 z-[90] bg-[var(--surface-scrim)]";

const DETAIL_PANEL_BASE_CLS =
  "fixed top-0 right-0 z-[100] flex h-full max-w-[90vw] translate-x-full flex-col overflow-y-auto border-l border-[var(--border)] bg-[var(--bg-secondary)] transition-transform duration-[0.25s] ease max-md:!w-screen max-md:!max-w-screen";

const DETAIL_PANEL_OPEN_CLS = "!translate-x-0";

const DETAIL_RESIZE_HANDLE_CLS =
  "absolute top-0 left-[-3px] z-[101] h-full w-[6px] cursor-col-resize transition-colors duration-150 hover:bg-[var(--accent)] hover:opacity-50 active:bg-[var(--accent)] active:opacity-50 max-md:hidden";

const DETAIL_HEADER_CLS = "border-b border-[var(--border)] px-5 py-4";

const DETAIL_HEADER_TOP_CLS = "mb-2 flex items-center justify-between";

const DETAIL_ID_CLS =
  "font-[inherit] text-[length:calc(var(--font-size-base)*0.8)] text-[var(--text-muted)]";

const DETAIL_CLOSE_CLS =
  "flex h-8 w-8 cursor-pointer items-center justify-center rounded border-0 bg-transparent text-[var(--text-muted)] transition-colors duration-150 hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11 pointer-coarse:min-w-11";

const DETAIL_TITLE_CLS =
  "my-1 text-[length:calc(var(--font-size-base)*1.05)] font-semibold";

const DETAIL_STATUS_CLS = "mt-1 inline-flex items-center gap-1.5";

const DETAIL_TRIGGER_CLS =
  "flex items-center gap-1.5 text-[length:calc(var(--font-size-base)*0.85)] text-[var(--text-secondary)]";

const DETAIL_BODY_CLS = "flex flex-1 flex-col gap-4 px-5 py-4";

const DETAIL_SECTION_CLS = "flex flex-col gap-1.5";

const DETAIL_LABEL_CLS =
  "text-[length:calc(var(--font-size-base)*0.7)] font-medium uppercase tracking-[0.05em] text-[var(--text-muted)]";

const DETAIL_VALUE_CLS =
  "text-[length:calc(var(--font-size-base)*0.85)] text-[var(--text-primary)]";

const DETAIL_MONO_CLS = "font-[inherit] text-[length:calc(var(--font-size-base)*0.8)]";

const DETAIL_CODE_CLS =
  "reports-detail-code max-h-[300px] overflow-x-auto overflow-y-auto whitespace-pre-wrap break-words rounded-md border border-[var(--border)] bg-[var(--bg-tertiary)] p-3 font-[inherit] text-[length:calc(var(--font-size-base)*0.75)] text-[var(--text-secondary)]";

const DETAIL_TOGGLE_CLS =
  "flex cursor-pointer items-center gap-1.5 border-0 bg-transparent p-0 font-[inherit] text-[length:calc(var(--font-size-base)*0.8)] text-[var(--text-secondary)] hover:text-[var(--text-primary)]";

const DETAIL_STATS_CLS =
  "grid grid-cols-3 gap-2 max-md:grid-cols-2 max-sm:grid-cols-1";

const DETAIL_STAT_CLS =
  "reports-detail-stat flex flex-col gap-0.5 rounded-md bg-[var(--bg-tertiary)] p-2";

const DETAIL_STAT_LABEL_CLS =
  "text-[length:calc(var(--font-size-base)*0.65)] uppercase tracking-[0.03em] text-[var(--text-muted)]";

const DETAIL_STAT_VALUE_CLS =
  "font-[inherit] text-[length:calc(var(--font-size-base)*0.9)] font-semibold text-[var(--text-primary)]";

const DETAIL_TAG_CLS =
  "reports-detail-tag inline-flex items-center rounded px-2 py-0.5 text-[length:calc(var(--font-size-base)*0.7)] font-medium bg-[var(--bg-tertiary)] text-[var(--text-secondary)]";

const DETAIL_TAGS_CLS = "flex flex-wrap gap-1.5";

const DETAIL_STEPS_CLS = "flex flex-col gap-1";

const APPROVAL_CLS =
  "flex items-center justify-between gap-3 rounded-md border border-[color-mix(in_srgb,var(--color-warning-foreground)_30%,transparent)] bg-[color-mix(in_srgb,var(--color-warning-foreground)_8%,transparent)] p-3";

const APPROVAL_MESSAGE_CLS =
  "flex items-center gap-1.5 text-[length:calc(var(--font-size-base)*0.85)] text-[var(--color-warning-foreground)]";

const APPROVAL_ACTIONS_CLS = "flex gap-2";

const BTN_BASE_CLS =
  "cursor-pointer rounded-md border-0 px-3 py-1.5 font-[inherit] text-[length:calc(var(--font-size-base)*0.8)] font-medium transition-opacity duration-150 disabled:cursor-default disabled:opacity-50 pointer-coarse:min-h-11";

const BTN_APPROVE_CLS = "bg-[var(--color-success-foreground)] text-[var(--text-on-success)]";

const BTN_REJECT_CLS = "bg-[var(--color-error)] text-[var(--text-on-error)]";

const DETAIL_ERROR_CLS =
  "rounded-md border border-[color-mix(in_srgb,var(--color-error)_30%,transparent)] bg-[color-mix(in_srgb,var(--color-error)_8%,transparent)] p-3 text-[length:calc(var(--font-size-base)*0.85)] text-[var(--color-error)]";

const DETAIL_COMMANDS_CLS = "flex flex-col gap-1";

const DETAIL_COMMAND_CLS =
  "flex items-center gap-2 rounded bg-[var(--bg-tertiary)] px-2 py-1.5 text-[length:calc(var(--font-size-base)*0.8)]";

const DETAIL_COMMAND_TYPE_CLS = "font-medium text-[var(--text-primary)]";

const DETAIL_COMMAND_TIME_CLS =
  "text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-muted)]";

const DETAIL_COMMAND_PAYLOAD_CLS =
  "max-w-[200px] overflow-hidden text-ellipsis whitespace-nowrap font-[inherit] text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-muted)]";

const GROUP_TOGGLE_CLS = "flex items-center gap-1.5";

const GROUP_LABEL_CLS =
  "whitespace-nowrap text-[length:var(--text-sm)] text-[var(--text-muted)]";

const GROUP_SELECT_CLS =
  "cursor-pointer rounded border border-[var(--border)] bg-[var(--bg-tertiary)] px-1.5 py-0.5 font-[inherit] text-[length:var(--text-sm)] text-[var(--text-primary)] focus:border-[var(--accent)] focus:outline-none";

const GROUP_CLS = "mb-3";

const GROUP_HEADER_CLS =
  "sticky top-0 z-[2] border-b border-[var(--border)] bg-[var(--bg-primary)] px-2.5 pt-2 pb-1 text-[length:calc(var(--font-size-base)*0.8)] font-semibold text-[var(--text-secondary)]";

const GROUP_COUNT_CLS =
  "font-normal text-[length:calc(var(--font-size-base)*0.7)] text-[var(--text-muted)]";

function statusMatchesFilter(status: string, filter: StatusFilter): boolean {
  if (filter === "all") return true;
  if (filter === "running") return status === "running" || status === "pending";
  if (filter === "waiting") return status === "waiting_approval";
  if (filter === "completed")
    return status === "completed" || status === "success";
  if (filter === "failed")
    return (
      status === "failed" ||
      status === "error" ||
      status === "timeout" ||
      status === "cancelled" ||
      status === "interrupted"
    );
  return true;
}

function normalizeStatus(status: string): string {
  return status.replace(/_/g, " ");
}

function formatDateTime(iso: string): string {
  const d = new Date(iso);
  return (
    d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) +
    " " +
    d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })
  );
}

const STATUS_OPTIONS: { value: StatusFilter; label: string }[] = [
  { value: "all", label: "All" },
  { value: "running", label: "Running" },
  { value: "waiting", label: "Waiting" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
];

// =============================================================================
// Sorting helpers
// =============================================================================

function comparePipelines(
  a: PipelineExecutionRecord,
  b: PipelineExecutionRecord,
  col: PipelineSortColumn,
  dir: SortDirection,
): number {
  let cmp = 0;
  switch (col) {
    case "name":
      cmp = a.pipeline_name.localeCompare(b.pipeline_name);
      break;
    case "time":
      cmp = a.created_at.localeCompare(b.created_at);
      break;
    case "duration": {
      const da = a.completed_at
        ? new Date(a.completed_at).getTime() - new Date(a.created_at).getTime()
        : 0;
      const db = b.completed_at
        ? new Date(b.completed_at).getTime() - new Date(b.created_at).getTime()
        : 0;
      cmp = da - db;
      break;
    }
    case "status":
      cmp = a.status.localeCompare(b.status);
      break;
  }
  return dir === "asc" ? cmp : -cmp;
}

function compareAgents(
  a: AgentRunRecord,
  b: AgentRunRecord,
  col: AgentSortColumn,
  dir: SortDirection,
): number {
  let cmp = 0;
  switch (col) {
    case "name":
      cmp = (a.workflow_name || "").localeCompare(b.workflow_name || "");
      break;
    case "provider":
      cmp = (a.provider || "").localeCompare(b.provider || "");
      break;
    case "time":
      cmp = a.created_at.localeCompare(b.created_at);
      break;
    case "duration": {
      const da =
        a.started_at && a.completed_at
          ? new Date(a.completed_at).getTime() -
            new Date(a.started_at).getTime()
          : 0;
      const db =
        b.started_at && b.completed_at
          ? new Date(b.completed_at).getTime() -
            new Date(b.started_at).getTime()
          : 0;
      cmp = da - db;
      break;
    }
    case "turns":
      cmp = (a.turns_used || 0) - (b.turns_used || 0);
      break;
    case "status":
      cmp = a.status.localeCompare(b.status);
      break;
  }
  return dir === "asc" ? cmp : -cmp;
}

function SortArrow<T extends string>({
  column,
  sortColumn,
  sortDirection,
}: {
  column: T;
  sortColumn: T;
  sortDirection: SortDirection;
}) {
  if (column !== sortColumn)
    return <span className="text-[var(--text-muted)] opacity-50">{"↕"}</span>;
  return (
    <span className="text-[var(--accent)]">
      {sortDirection === "asc" ? "↑" : "↓"}
    </span>
  );
}

function groupBy<T>(items: T[], keyFn: (item: T) => string): Map<string, T[]> {
  const groups = new Map<string, T[]>();
  for (const item of items) {
    const key = keyFn(item) || "Unknown";
    const arr = groups.get(key) || [];
    arr.push(item);
    groups.set(key, arr);
  }
  return groups;
}

// =============================================================================
// Resize handle for detail sidebar
// =============================================================================

function useResizablePanel(
  initialWidth: number,
  minWidth: number,
  maxWidth: number,
) {
  const [width, setWidth] = useState(initialWidth);
  const isDragging = useRef(false);
  const startX = useRef(0);
  const startWidth = useRef(0);
  const cleanupRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    return () => {
      cleanupRef.current?.();
    };
  }, []);

  const handleMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      isDragging.current = true;
      startX.current = e.clientX;
      startWidth.current = width;

      const onMove = (ev: MouseEvent) => {
        if (!isDragging.current) return;
        const delta = startX.current - ev.clientX;
        setWidth(
          Math.max(minWidth, Math.min(maxWidth, startWidth.current + delta)),
        );
      };
      const onUp = () => {
        isDragging.current = false;
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        cleanupRef.current = null;
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
      cleanupRef.current = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
      };
    },
    [width, minWidth, maxWidth],
  );

  const handleTouchStart = useCallback(
    (e: React.TouchEvent) => {
      e.preventDefault();
      isDragging.current = true;
      startX.current = e.touches[0].clientX;
      startWidth.current = width;

      const onMove = (ev: TouchEvent) => {
        ev.preventDefault();
        if (!isDragging.current) return;
        const delta = startX.current - ev.touches[0].clientX;
        setWidth(
          Math.max(minWidth, Math.min(maxWidth, startWidth.current + delta)),
        );
      };
      const onEnd = () => {
        isDragging.current = false;
        document.removeEventListener("touchmove", onMove);
        document.removeEventListener("touchend", onEnd);
        cleanupRef.current = null;
      };
      document.addEventListener("touchmove", onMove, { passive: false });
      document.addEventListener("touchend", onEnd);
      cleanupRef.current = () => {
        document.removeEventListener("touchmove", onMove);
        document.removeEventListener("touchend", onEnd);
      };
    },
    [width, minWidth, maxWidth],
  );

  return { width, handleMouseDown, handleTouchStart };
}

// =============================================================================
// Icons
// =============================================================================

function CloseIcon() {
  return (
    <svg
      width="18"
      height="18"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  );
}

function CronIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="10" />
      <polyline points="12 6 12 12 16 14" />
    </svg>
  );
}

// =============================================================================
// ReportsPage
// =============================================================================

export function ReportsPage({
  projectId,
  onNavigateToTrace,
}: {
  projectId?: string;
  onNavigateToTrace?: (traceId: string) => void;
}) {
  const [subTab, setSubTab] = useState<SubTab>("pipelines");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [searchText, setSearchText] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [agentDetails, setAgentDetails] = useState<
    Record<string, AgentRunDetail>
  >({});
  const [actionLoading, setActionLoading] = useState<string | null>(null);

  const [pipelineSortCol, setPipelineSortCol] =
    useState<PipelineSortColumn>("time");
  const [pipelineSortDir, setPipelineSortDir] = useState<SortDirection>("desc");
  const [agentSortCol, setAgentSortCol] = useState<AgentSortColumn>("time");
  const [agentSortDir, setAgentSortDir] = useState<SortDirection>("desc");

  const [pipelineGroupBy, setPipelineGroupBy] = useState<GroupBy>("none");
  const [agentGroupBy, setAgentGroupBy] = useState<GroupBy>("none");

  const {
    width: panelWidth,
    handleMouseDown: onResizeMouseDown,
    handleTouchStart: onResizeTouchStart,
  } = useResizablePanel(460, 300, 800);

  const handlePipelineSort = useCallback((col: PipelineSortColumn) => {
    setPipelineSortCol((prev) => {
      if (prev === col) {
        setPipelineSortDir((d) => (d === "asc" ? "desc" : "asc"));
        return col;
      }
      setPipelineSortDir("asc");
      return col;
    });
  }, []);

  const handleAgentSort = useCallback((col: AgentSortColumn) => {
    setAgentSortCol((prev) => {
      if (prev === col) {
        setAgentSortDir((d) => (d === "asc" ? "desc" : "asc"));
        return col;
      }
      setAgentSortDir("asc");
      return col;
    });
  }, []);

  const {
    executions: pipelineExecutions,
    isLoading: pipelinesLoading,
    approvePipeline,
    rejectPipeline,
  } = usePipelineExecutions(projectId);

  const {
    runs: agentRuns,
    isLoading: agentsLoading,
    cancelRun,
    fetchRunDetail,
  } = useAgentRuns(projectId);

  const pipelineCounts = useMemo(() => {
    const statuses = pipelineExecutions.map((pe) => pe.status);
    return {
      all: statuses.length,
      running: statuses.filter((s) => s === "running" || s === "pending")
        .length,
      waiting: statuses.filter((s) => s === "waiting_approval").length,
      completed: statuses.filter((s) => s === "completed").length,
      failed: statuses.filter(
        (s) => s === "failed" || s === "cancelled" || s === "interrupted",
      ).length,
    };
  }, [pipelineExecutions]);

  const agentCounts = useMemo(() => {
    const statuses = agentRuns.map((ar) => ar.status);
    return {
      all: statuses.length,
      running: statuses.filter((s) => s === "running" || s === "pending")
        .length,
      waiting: 0,
      completed: statuses.filter((s) => s === "success").length,
      failed: statuses.filter(
        (s) => s === "error" || s === "timeout" || s === "cancelled",
      ).length,
    };
  }, [agentRuns]);

  const counts = subTab === "pipelines" ? pipelineCounts : agentCounts;

  const filteredPipelines = useMemo(() => {
    let items = pipelineExecutions.filter((pe) =>
      statusMatchesFilter(pe.status, statusFilter),
    );
    if (searchText.trim()) {
      const q = searchText.toLowerCase();
      items = items.filter(
        (pe) =>
          pe.pipeline_name.toLowerCase().includes(q) ||
          pe.id.toLowerCase().includes(q),
      );
    }
    return [...items].sort((a, b) =>
      comparePipelines(a, b, pipelineSortCol, pipelineSortDir),
    );
  }, [
    pipelineExecutions,
    statusFilter,
    searchText,
    pipelineSortCol,
    pipelineSortDir,
  ]);

  const filteredAgents = useMemo(() => {
    let items = agentRuns.filter((ar) =>
      statusMatchesFilter(ar.status, statusFilter),
    );
    if (searchText.trim()) {
      const q = searchText.toLowerCase();
      items = items.filter(
        (ar) =>
          (ar.workflow_name || "").toLowerCase().includes(q) ||
          (ar.prompt || "").toLowerCase().includes(q) ||
          ar.id.toLowerCase().includes(q),
      );
    }
    return [...items].sort((a, b) =>
      compareAgents(a, b, agentSortCol, agentSortDir),
    );
  }, [agentRuns, statusFilter, searchText, agentSortCol, agentSortDir]);

  const pipelineGroups = useMemo(() => {
    if (pipelineGroupBy === "none") return null;
    return groupBy(filteredPipelines, (pe) => pe.pipeline_name);
  }, [filteredPipelines, pipelineGroupBy]);

  const agentGroups = useMemo(() => {
    if (agentGroupBy === "none") return null;
    if (agentGroupBy === "provider")
      return groupBy(filteredAgents, (ar) => ar.provider || "Unknown");
    return groupBy(filteredAgents, (ar) => ar.workflow_name || "Ad-hoc");
  }, [filteredAgents, agentGroupBy]);

  useEffect(() => {
    setSelectedId(null);
  }, [subTab]);

  const handleSelectAgent = useCallback(
    async (id: string) => {
      setSelectedId(id);
      if (!agentDetails[id]) {
        const detail = await fetchRunDetail(id);
        if (detail) setAgentDetails((prev) => ({ ...prev, [id]: detail }));
      }
    },
    [agentDetails, fetchRunDetail],
  );

  const handleApprove = async (token: string) => {
    setActionLoading(token);
    try {
      await approvePipeline(token);
    } catch (e) {
      console.error("Approve failed:", e);
    } finally {
      setActionLoading(null);
    }
  };

  const handleReject = async (token: string) => {
    setActionLoading(token);
    try {
      await rejectPipeline(token);
    } catch (e) {
      console.error("Reject failed:", e);
    } finally {
      setActionLoading(null);
    }
  };

  const handleCancel = async (runId: string) => {
    setActionLoading(runId);
    try {
      await cancelRun(runId);
    } catch (e) {
      console.error("Cancel failed:", e);
    } finally {
      setActionLoading(null);
    }
  };

  const isLoading = subTab === "pipelines" ? pipelinesLoading : agentsLoading;
  const isEmpty =
    subTab === "pipelines"
      ? filteredPipelines.length === 0
      : filteredAgents.length === 0;

  const selectedPipeline =
    subTab === "pipelines"
      ? pipelineExecutions.find((pe) => pe.id === selectedId)
      : null;
  const selectedAgent =
    subTab === "agents" ? agentRuns.find((ar) => ar.id === selectedId) : null;

  return (
    <main className={PAGE_CLS}>
      <div className={TOOLBAR_CLS}>
        <div className={TOOLBAR_LEFT_CLS}>
          <h2 className={TITLE_CLS}>Reports</h2>
          <SegmentedControl<SubTab>
            value={subTab}
            onChange={setSubTab}
            options={[
              { value: "pipelines", label: "Pipeline Executions" },
              { value: "agents", label: "Agent Runs" },
            ]}
            ariaLabel="Report type"
          />
        </div>
        <div className={TOOLBAR_RIGHT_CLS}>
          <div className={GROUP_TOGGLE_CLS}>
            <span className={GROUP_LABEL_CLS}>Group:</span>
            {subTab === "pipelines" ? (
              <select
                className={GROUP_SELECT_CLS}
                value={pipelineGroupBy}
                onChange={(e) => setPipelineGroupBy(e.target.value as GroupBy)}
              >
                <option value="none">None</option>
                <option value="name">Pipeline</option>
              </select>
            ) : (
              <select
                className={GROUP_SELECT_CLS}
                value={agentGroupBy}
                onChange={(e) => setAgentGroupBy(e.target.value as GroupBy)}
              >
                <option value="none">None</option>
                <option value="name">Workflow</option>
                <option value="provider">Provider</option>
              </select>
            )}
          </div>
          <input
            type="text"
            className={SEARCH_CLS}
            placeholder={
              subTab === "pipelines"
                ? "Search pipelines..."
                : "Search agents..."
            }
            value={searchText}
            onChange={(e) => setSearchText(e.target.value)}
          />
        </div>
      </div>

      <div className={FILTER_BAR_CLS}>
        <div className={FILTER_CHIPS_CLS}>
          {STATUS_OPTIONS.filter((opt) => {
            if (opt.value === "all") return true;
            return counts[opt.value] > 0;
          }).map((opt) => (
            <button
              key={opt.value}
              className={`${STAT_CHIP_BASE_CLS} ${statusFilter === opt.value ? STAT_CHIP_ACTIVE_CLS : ""}`}
              onClick={() =>
                setStatusFilter(
                  statusFilter === opt.value && opt.value !== "all"
                    ? "all"
                    : opt.value,
                )
              }
            >
              {opt.value !== "all" && (
                <StatusDot
                  status={
                    opt.value === "running"
                      ? "running"
                      : opt.value === "waiting"
                        ? "waiting_approval"
                        : opt.value === "completed"
                          ? "completed"
                          : "failed"
                  }
                />
              )}
              {opt.label} ({counts[opt.value]})
            </button>
          ))}
        </div>
      </div>

      {isLoading ? (
        <div className={LOADING_EMPTY_CLS}>Loading...</div>
      ) : isEmpty ? (
        <div className={LOADING_EMPTY_CLS}>
          No {subTab === "pipelines" ? "pipeline executions" : "agent runs"}{" "}
          found
        </div>
      ) : subTab === "pipelines" ? (
        <div className={TABLE_CONTAINER_CLS}>
          {pipelineGroups ? (
            Array.from(pipelineGroups).map(([group, items]) => (
              <div key={group} className={GROUP_CLS}>
                <div className={GROUP_HEADER_CLS}>
                  {group}{" "}
                  <span className={GROUP_COUNT_CLS}>({items.length})</span>
                </div>
                <table className={TABLE_CLS}>
                  <thead>
                    <tr>
                      <th className={TH_BASE_CLS} style={{ width: 28 }}></th>
                      <PipelineHeaders
                        onSort={handlePipelineSort}
                        sortCol={pipelineSortCol}
                        sortDir={pipelineSortDir}
                      />
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((pe) => (
                      <PipelineRow
                        key={pe.id}
                        pe={pe}
                        selectedId={selectedId}
                        onSelect={setSelectedId}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            ))
          ) : (
            <table className={TABLE_CLS}>
              <thead>
                <tr>
                  <th className={TH_BASE_CLS} style={{ width: 28 }}></th>
                  <PipelineHeaders
                    onSort={handlePipelineSort}
                    sortCol={pipelineSortCol}
                    sortDir={pipelineSortDir}
                  />
                </tr>
              </thead>
              <tbody>
                {filteredPipelines.map((pe) => (
                  <PipelineRow
                    key={pe.id}
                    pe={pe}
                    selectedId={selectedId}
                    onSelect={setSelectedId}
                  />
                ))}
              </tbody>
            </table>
          )}
        </div>
      ) : (
        <div className={TABLE_CONTAINER_CLS}>
          {agentGroups ? (
            Array.from(agentGroups).map(([group, items]) => (
              <div key={group} className={GROUP_CLS}>
                <div className={GROUP_HEADER_CLS}>
                  {group}{" "}
                  <span className={GROUP_COUNT_CLS}>({items.length})</span>
                </div>
                <table className={TABLE_CLS}>
                  <thead>
                    <tr>
                      <th className={TH_BASE_CLS} style={{ width: 28 }}></th>
                      <AgentHeaders
                        onSort={handleAgentSort}
                        sortCol={agentSortCol}
                        sortDir={agentSortDir}
                      />
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((ar) => (
                      <AgentRow
                        key={ar.id}
                        ar={ar}
                        selectedId={selectedId}
                        onSelect={handleSelectAgent}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            ))
          ) : (
            <table className={TABLE_CLS}>
              <thead>
                <tr>
                  <th className={TH_BASE_CLS} style={{ width: 28 }}></th>
                  <AgentHeaders
                    onSort={handleAgentSort}
                    sortCol={agentSortCol}
                    sortDir={agentSortDir}
                  />
                </tr>
              </thead>
              <tbody>
                {filteredAgents.map((ar) => (
                  <AgentRow
                    key={ar.id}
                    ar={ar}
                    selectedId={selectedId}
                    onSelect={handleSelectAgent}
                  />
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {selectedId && (selectedPipeline || selectedAgent) && (
        <>
          <div
            className={DETAIL_BACKDROP_CLS}
            onClick={() => setSelectedId(null)}
          />
          <div
            className={`${DETAIL_PANEL_BASE_CLS} ${selectedId ? DETAIL_PANEL_OPEN_CLS : ""}`}
            style={{ width: panelWidth }}
          >
            <div
              className={DETAIL_RESIZE_HANDLE_CLS}
              onMouseDown={onResizeMouseDown}
              onTouchStart={onResizeTouchStart}
            />
            {selectedPipeline && (
              <PipelineDetail
                execution={selectedPipeline}
                actionLoading={actionLoading}
                onApprove={handleApprove}
                onReject={handleReject}
                onNavigateToTrace={onNavigateToTrace}
                onClose={() => setSelectedId(null)}
              />
            )}
            {selectedAgent && (
              <AgentDetail
                run={selectedAgent}
                detail={agentDetails[selectedAgent.id]}
                actionLoading={actionLoading}
                onCancel={handleCancel}
                onClose={() => setSelectedId(null)}
              />
            )}
          </div>
        </>
      )}
    </main>
  );
}

// =============================================================================
// Table headers (extracted for group-by reuse)
// =============================================================================

function PipelineHeaders({
  onSort,
  sortCol,
  sortDir,
}: {
  onSort: (c: PipelineSortColumn) => void;
  sortCol: PipelineSortColumn;
  sortDir: SortDirection;
}) {
  return (
    <>
      <th
        className={`${TH_BASE_CLS} ${TH_SORTABLE_CLS}`}
        onClick={() => onSort("name")}
      >
        Name{" "}
        <SortArrow column="name" sortColumn={sortCol} sortDirection={sortDir} />
      </th>
      <th className={`${TH_BASE_CLS} ${TH_ID_CLS}`} style={{ width: 120 }}>
        ID
      </th>
      <th
        className={`${TH_BASE_CLS} ${TH_SORTABLE_CLS}`}
        style={{ width: 140 }}
        onClick={() => onSort("time")}
      >
        Time{" "}
        <SortArrow column="time" sortColumn={sortCol} sortDirection={sortDir} />
      </th>
      <th
        className={`${TH_BASE_CLS} ${TH_SORTABLE_CLS} max-md:hidden`}
        style={{ width: 80 }}
        onClick={() => onSort("duration")}
      >
        Duration{" "}
        <SortArrow
          column="duration"
          sortColumn={sortCol}
          sortDirection={sortDir}
        />
      </th>
      <th
        className={`${TH_BASE_CLS} ${TH_SORTABLE_CLS}`}
        style={{ width: 100 }}
        onClick={() => onSort("status")}
      >
        Status{" "}
        <SortArrow
          column="status"
          sortColumn={sortCol}
          sortDirection={sortDir}
        />
      </th>
    </>
  );
}

function AgentHeaders({
  onSort,
  sortCol,
  sortDir,
}: {
  onSort: (c: AgentSortColumn) => void;
  sortCol: AgentSortColumn;
  sortDir: SortDirection;
}) {
  return (
    <>
      <th
        className={`${TH_BASE_CLS} ${TH_SORTABLE_CLS}`}
        onClick={() => onSort("name")}
      >
        Name{" "}
        <SortArrow column="name" sortColumn={sortCol} sortDirection={sortDir} />
      </th>
      <th
        className={`${TH_BASE_CLS} ${TH_SORTABLE_CLS}`}
        style={{ width: 80 }}
        onClick={() => onSort("provider")}
      >
        Provider{" "}
        <SortArrow
          column="provider"
          sortColumn={sortCol}
          sortDirection={sortDir}
        />
      </th>
      <th className={`${TH_BASE_CLS} ${TH_ID_CLS}`} style={{ width: 120 }}>
        ID
      </th>
      <th
        className={`${TH_BASE_CLS} ${TH_SORTABLE_CLS}`}
        style={{ width: 140 }}
        onClick={() => onSort("time")}
      >
        Time{" "}
        <SortArrow column="time" sortColumn={sortCol} sortDirection={sortDir} />
      </th>
      <th
        className={`${TH_BASE_CLS} ${TH_SORTABLE_CLS} max-md:hidden`}
        style={{ width: 80 }}
        onClick={() => onSort("duration")}
      >
        Duration{" "}
        <SortArrow
          column="duration"
          sortColumn={sortCol}
          sortDirection={sortDir}
        />
      </th>
      <th
        className={`${TH_BASE_CLS} ${TH_SORTABLE_CLS}`}
        style={{ width: 70 }}
        onClick={() => onSort("turns")}
      >
        Turns{" "}
        <SortArrow
          column="turns"
          sortColumn={sortCol}
          sortDirection={sortDir}
        />
      </th>
      <th
        className={`${TH_BASE_CLS} ${TH_SORTABLE_CLS}`}
        style={{ width: 100 }}
        onClick={() => onSort("status")}
      >
        Status{" "}
        <SortArrow
          column="status"
          sortColumn={sortCol}
          sortDirection={sortDir}
        />
      </th>
    </>
  );
}

// =============================================================================
// Table rows (extracted for group-by reuse)
// =============================================================================

function PipelineRow({
  pe,
  selectedId,
  onSelect,
}: {
  pe: PipelineExecutionRecord;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <tr
      className={`reports-row ${ROW_BASE_CLS} ${selectedId === pe.id ? ROW_SELECTED_CLS : ""}`}
      onClick={() => onSelect(pe.id)}
    >
      <td className={CELL_BASE_CLS} data-label="">
        <StatusDot status={pe.status} />
      </td>
      <td className={`${CELL_BASE_CLS} ${CELL_NAME_CLS}`} data-label="Name">{pe.pipeline_name}</td>
      <td className={`${CELL_BASE_CLS} ${CELL_ID_CLS}`} data-label="ID">{pe.id.slice(0, 12)}</td>
      <td className={`${CELL_BASE_CLS} ${CELL_TIME_CLS}`} data-label="Time">
        {formatDateTime(pe.created_at)}
      </td>
      <td className={`${CELL_BASE_CLS} ${CELL_DURATION_CLS}`} data-label="Duration">
        {pe.completed_at
          ? formatDuration(pe.created_at, pe.completed_at)
          : pe.status === "running"
            ? "..."
            : "—"}
      </td>
      <td className={`${CELL_BASE_CLS} ${CELL_STATUS_CLS}`} data-label="Status">
        {normalizeStatus(pe.status)}
      </td>
    </tr>
  );
}

function AgentRow({
  ar,
  selectedId,
  onSelect,
}: {
  ar: AgentRunRecord;
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <tr
      className={`reports-row ${ROW_BASE_CLS} ${selectedId === ar.id ? ROW_SELECTED_CLS : ""}`}
      onClick={() => onSelect(ar.id)}
    >
      <td className={CELL_BASE_CLS} data-label="">
        <StatusDot status={ar.status} />
      </td>
      <td className={`${CELL_BASE_CLS} ${CELL_NAME_CLS}`} data-label="Name">
        {ar.workflow_name || ar.prompt?.slice(0, 60) || "Agent Run"}
      </td>
      <td className={CELL_BASE_CLS} data-label="Provider">
        <span className={`${TYPE_BADGE_BASE_CLS} ${TYPE_BADGE_AGENT_CLS}`}>
          {ar.provider}
        </span>
      </td>
      <td className={`${CELL_BASE_CLS} ${CELL_ID_CLS}`} data-label="ID">{ar.id.slice(0, 12)}</td>
      <td className={`${CELL_BASE_CLS} ${CELL_TIME_CLS}`} data-label="Time">
        {formatDateTime(ar.created_at)}
      </td>
      <td className={`${CELL_BASE_CLS} ${CELL_DURATION_CLS}`} data-label="Duration">
        {ar.started_at && ar.completed_at
          ? formatDuration(ar.started_at, ar.completed_at)
          : ar.status === "running"
            ? "..."
            : "—"}
      </td>
      <td className={CELL_BASE_CLS} data-label="Turns" style={{ textAlign: "center" }}>
        {ar.turns_used}
      </td>
      <td className={`${CELL_BASE_CLS} ${CELL_STATUS_CLS}`} data-label="Status">
        {normalizeStatus(ar.status)}
      </td>
    </tr>
  );
}

// =============================================================================
// Pipeline Detail Sidebar
// =============================================================================

function PipelineDetail({
  execution,
  actionLoading,
  onApprove,
  onReject,
  onNavigateToTrace,
  onClose,
}: {
  execution: PipelineExecutionRecord;
  actionLoading: string | null;
  onApprove: (token: string) => Promise<void>;
  onReject: (token: string) => Promise<void>;
  onNavigateToTrace?: (traceId: string) => void;
  onClose: () => void;
}) {
  const [showConfig, setShowConfig] = useState(false);
  const [showInputs, setShowInputs] = useState(false);
  const [showOutputs, setShowOutputs] = useState(false);
  return (
    <>
      <div className={DETAIL_HEADER_CLS}>
        <div className={DETAIL_HEADER_TOP_CLS}>
          <span className={DETAIL_ID_CLS}>{execution.id}</span>
          <button className={DETAIL_CLOSE_CLS} onClick={onClose}>
            <CloseIcon />
          </button>
        </div>
        <div className={DETAIL_TITLE_CLS}>{execution.pipeline_name}</div>
        <div className={DETAIL_STATUS_CLS}>
          <StatusDot status={execution.status} />
          <span className={STATUS_TEXT_CLS}>
            {normalizeStatus(execution.status)}
          </span>
          {execution.cron_job_name && (
            <span className={DETAIL_TRIGGER_CLS}>
              <CronIcon /> {execution.cron_job_name}
            </span>
          )}
        </div>
      </div>

      <div className={DETAIL_BODY_CLS}>
        {(execution as any).trace_id && onNavigateToTrace && (
          <div className={DETAIL_SECTION_CLS}>
            <button
              type="button"
              className={BTN_BASE_CLS}
              onClick={() => onNavigateToTrace((execution as any).trace_id)}
              title="View telemetry trace for this execution"
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
                style={{ marginRight: "6px", verticalAlign: "middle" }}
              >
                <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
              </svg>
              View Trace
            </button>
          </div>
        )}

        {execution.status === "waiting_approval" &&
          (() => {
            const waitingStep = execution.steps.find(
              (s) => s.status === "waiting_approval" && s.approval_token,
            );
            return waitingStep?.approval_token ? (
              <div className={APPROVAL_CLS}>
                <div className={APPROVAL_MESSAGE_CLS}>
                  <AlertIcon />
                  <span>
                    Step &ldquo;{waitingStep.step_id}&rdquo; requires approval
                  </span>
                </div>
                <div className={APPROVAL_ACTIONS_CLS}>
                  <button
                    type="button"
                    className={`${BTN_BASE_CLS} ${BTN_APPROVE_CLS}`}
                    onClick={() => onApprove(waitingStep.approval_token!)}
                    disabled={actionLoading === waitingStep.approval_token}
                  >
                    {actionLoading === waitingStep.approval_token
                      ? "Approving..."
                      : "Approve"}
                  </button>
                  <button
                    type="button"
                    className={`${BTN_BASE_CLS} ${BTN_REJECT_CLS}`}
                    onClick={() => onReject(waitingStep.approval_token!)}
                    disabled={actionLoading === waitingStep.approval_token}
                  >
                    {actionLoading === waitingStep.approval_token
                      ? "Rejecting..."
                      : "Reject"}
                  </button>
                </div>
              </div>
            ) : null;
          })()}

        {execution.steps.length > 0 && (
          <div className={DETAIL_SECTION_CLS}>
            <span className={DETAIL_LABEL_CLS}>Execution Report</span>
            <div className={DETAIL_STEPS_CLS}>
              {execution.steps.map((step, index) => (
                <StepDisplay key={step.id} step={step} index={index} />
              ))}
            </div>
          </div>
        )}

        {execution.outputs_json &&
          (() => {
            try {
              const outputs = JSON.parse(execution.outputs_json);
              if (outputs.error) {
                return (
                  <div className={DETAIL_ERROR_CLS}>
                    Error: {outputs.error}
                  </div>
                );
              }
            } catch {
              /* ignore */
            }
            return null;
          })()}

        {execution.inputs_json && (
          <div className={DETAIL_SECTION_CLS}>
            <button
              type="button"
              className={DETAIL_TOGGLE_CLS}
              onClick={() => setShowInputs(!showInputs)}
            >
              <ChevronIcon expanded={showInputs} /> Inputs
            </button>
            {showInputs && (
              <div className={DETAIL_CODE_CLS}>
                {formatJson(execution.inputs_json)}
              </div>
            )}
          </div>
        )}

        {execution.status === "completed" && execution.outputs_json && (
          <div className={DETAIL_SECTION_CLS}>
            <button
              type="button"
              className={DETAIL_TOGGLE_CLS}
              onClick={() => setShowOutputs(!showOutputs)}
            >
              <ChevronIcon expanded={showOutputs} /> Outputs
            </button>
            {showOutputs && (
              <div className={DETAIL_CODE_CLS}>
                {formatJson(execution.outputs_json)}
              </div>
            )}
          </div>
        )}

        {execution.definition_json && (
          <div className={DETAIL_SECTION_CLS}>
            <button
              type="button"
              className={DETAIL_TOGGLE_CLS}
              onClick={() => setShowConfig(!showConfig)}
            >
              <ChevronIcon expanded={showConfig} /> Pipeline Config
            </button>
            {showConfig && (
              <div className={DETAIL_CODE_CLS}>
                {formatJson(execution.definition_json)}
              </div>
            )}
          </div>
        )}

        {execution.parent_execution_id && (
          <div className={DETAIL_SECTION_CLS}>
            <span className={DETAIL_LABEL_CLS}>Parent</span>
            <span className={`${DETAIL_VALUE_CLS} ${DETAIL_MONO_CLS}`}>
              {execution.parent_execution_id}
            </span>
          </div>
        )}
      </div>
    </>
  );
}

// =============================================================================
// Agent Detail Sidebar
// =============================================================================

function AgentDetail({
  run,
  detail,
  actionLoading,
  onCancel,
  onClose,
}: {
  run: AgentRunRecord;
  detail?: AgentRunDetail;
  actionLoading: string | null;
  onCancel: (runId: string) => Promise<void>;
  onClose: () => void;
}) {
  const [showPrompt, setShowPrompt] = useState(false);
  const [showResult, setShowResult] = useState(false);

  const totalTokens =
    (run.usage_input_tokens || 0) + (run.usage_output_tokens || 0);

  return (
    <>
      <div className={DETAIL_HEADER_CLS}>
        <div className={DETAIL_HEADER_TOP_CLS}>
          <span className={DETAIL_ID_CLS}>{run.id}</span>
          <button className={DETAIL_CLOSE_CLS} onClick={onClose}>
            <CloseIcon />
          </button>
        </div>
        <div className={DETAIL_TITLE_CLS}>
          {run.workflow_name || run.prompt?.slice(0, 80) || "Agent Run"}
        </div>
        <div className={DETAIL_STATUS_CLS}>
          <StatusDot status={run.status} />
          <span className={STATUS_TEXT_CLS}>
            {normalizeStatus(run.status)}
          </span>
        </div>
        <div className={DETAIL_TAGS_CLS}>
          <span className={DETAIL_TAG_CLS}>{run.provider}</span>
          {run.model && <span className={DETAIL_TAG_CLS}>{run.model}</span>}
          <span className={DETAIL_TAG_CLS}>{run.mode}</span>
        </div>
      </div>

      <div className={DETAIL_BODY_CLS}>
        {run.status === "running" && (
          <div className={DETAIL_SECTION_CLS}>
            <button
              type="button"
              className={`${BTN_BASE_CLS} ${BTN_REJECT_CLS}`}
              onClick={() => onCancel(run.id)}
              disabled={actionLoading === run.id}
            >
              {actionLoading === run.id ? "Cancelling..." : "Cancel Agent"}
            </button>
          </div>
        )}

        {run.summary_markdown && (
          <div className={DETAIL_SECTION_CLS}>
            <span className={DETAIL_LABEL_CLS}>Summary</span>
            <div className={DETAIL_CODE_CLS}>{run.summary_markdown}</div>
          </div>
        )}

        {(run.status === "error" || run.status === "timeout") && run.error && (
          <div className={DETAIL_ERROR_CLS}>Error: {run.error}</div>
        )}

        {run.status === "success" && run.result && (
          <div className={DETAIL_SECTION_CLS}>
            <button
              type="button"
              className={DETAIL_TOGGLE_CLS}
              onClick={() => setShowResult(!showResult)}
            >
              <ChevronIcon expanded={showResult} /> Result
            </button>
            {showResult && (
              <div className={DETAIL_CODE_CLS}>{run.result}</div>
            )}
          </div>
        )}

        {detail?.commands && detail.commands.length > 0 && (
          <div className={DETAIL_SECTION_CLS}>
            <span className={DETAIL_LABEL_CLS}>
              Commands ({detail.commands.length})
            </span>
            <div className={DETAIL_COMMANDS_CLS}>
              {detail.commands.map((cmd) => (
                <div key={cmd.id} className={DETAIL_COMMAND_CLS}>
                  <span className={DETAIL_COMMAND_TYPE_CLS}>
                    {cmd.command_text}
                  </span>
                  <span className={DETAIL_COMMAND_TIME_CLS}>
                    {formatTime(cmd.created_at)}
                  </span>
                  {cmd.command_text && (
                    <span className={DETAIL_COMMAND_PAYLOAD_CLS}>
                      {cmd.command_text.slice(0, 80)}
                    </span>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {totalTokens > 0 && (
          <div className={DETAIL_SECTION_CLS}>
            <span className={DETAIL_LABEL_CLS}>Usage</span>
            <div className={DETAIL_STATS_CLS}>
              <div className={DETAIL_STAT_CLS}>
                <span className={DETAIL_STAT_LABEL_CLS}>Input</span>
                <span className={DETAIL_STAT_VALUE_CLS}>
                  {(run.usage_input_tokens || 0).toLocaleString()}
                </span>
              </div>
              <div className={DETAIL_STAT_CLS}>
                <span className={DETAIL_STAT_LABEL_CLS}>Output</span>
                <span className={DETAIL_STAT_VALUE_CLS}>
                  {(run.usage_output_tokens || 0).toLocaleString()}
                </span>
              </div>
              {(run.usage_cache_read_tokens || 0) > 0 && (
                <div className={DETAIL_STAT_CLS}>
                  <span className={DETAIL_STAT_LABEL_CLS}>Cache</span>
                  <span className={DETAIL_STAT_VALUE_CLS}>
                    {(run.usage_cache_read_tokens || 0).toLocaleString()}
                  </span>
                </div>
              )}
              <div className={DETAIL_STAT_CLS}>
                <span className={DETAIL_STAT_LABEL_CLS}>Tools</span>
                <span className={DETAIL_STAT_VALUE_CLS}>
                  {run.tool_calls_count}
                </span>
              </div>
            </div>
          </div>
        )}

        {run.prompt && (
          <div className={DETAIL_SECTION_CLS}>
            <button
              type="button"
              className={DETAIL_TOGGLE_CLS}
              onClick={() => setShowPrompt(!showPrompt)}
            >
              <ChevronIcon expanded={showPrompt} /> Prompt
            </button>
            {showPrompt && (
              <div className={DETAIL_CODE_CLS}>{run.prompt}</div>
            )}
          </div>
        )}

        {(run.task_id || run.worktree_id || run.clone_id || run.git_branch) && (
          <div className={DETAIL_SECTION_CLS}>
            <span className={DETAIL_LABEL_CLS}>Context</span>
            {run.task_id && (
              <span className={`${DETAIL_VALUE_CLS} ${DETAIL_MONO_CLS}`}>
                Task: {run.task_id}
              </span>
            )}
            {run.git_branch && (
              <span className={`${DETAIL_VALUE_CLS} ${DETAIL_MONO_CLS}`}>
                Branch: {run.git_branch}
              </span>
            )}
            {(run.worktree_id || run.clone_id) && (
              <span className={DETAIL_VALUE_CLS}>
                {run.worktree_id
                  ? `Worktree: ${run.worktree_id}`
                  : `Clone: ${run.clone_id}`}
              </span>
            )}
          </div>
        )}
      </div>
    </>
  );
}
