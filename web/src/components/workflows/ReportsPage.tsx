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
    return <span className="sort-arrow muted">{"\u2195"}</span>;
  return (
    <span className="sort-arrow active">
      {sortDirection === "asc" ? "\u2191" : "\u2193"}
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

  // Clean up any active listeners on unmount
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
// Status dot
// =============================================================================


// =============================================================================
// Close icon
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

  // Sorting state
  const [pipelineSortCol, setPipelineSortCol] =
    useState<PipelineSortColumn>("time");
  const [pipelineSortDir, setPipelineSortDir] = useState<SortDirection>("desc");
  const [agentSortCol, setAgentSortCol] = useState<AgentSortColumn>("time");
  const [agentSortDir, setAgentSortDir] = useState<SortDirection>("desc");

  // Group-by state
  const [pipelineGroupBy, setPipelineGroupBy] = useState<GroupBy>("none");
  const [agentGroupBy, setAgentGroupBy] = useState<GroupBy>("none");

  // Resizable sidebar
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

  // Compute counts
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

  // Filtered + sorted lists
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

  // Grouped data
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

  // Clear selection on tab switch
  useEffect(() => {
    setSelectedId(null);
  }, [subTab]);

  // Fetch agent detail when selected
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
    <main className="reports-page">
      {/* Toolbar */}
      <div className="reports-toolbar">
        <div className="reports-toolbar-left">
          <h2 className="reports-title">Reports</h2>
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
        <div className="reports-toolbar-right">
          <div className="reports-group-toggle">
            <span className="reports-group-label">Group:</span>
            {subTab === "pipelines" ? (
              <select
                className="reports-group-select"
                value={pipelineGroupBy}
                onChange={(e) => setPipelineGroupBy(e.target.value as GroupBy)}
              >
                <option value="none">None</option>
                <option value="name">Pipeline</option>
              </select>
            ) : (
              <select
                className="reports-group-select"
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
            className="reports-search"
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

      {/* Filter bar */}
      <div className="reports-filter-bar">
        <div className="reports-filter-chips">
          {STATUS_OPTIONS.filter((opt) => {
            if (opt.value === "all") return true;
            return counts[opt.value] > 0;
          }).map((opt) => (
            <button
              key={opt.value}
              className={`reports-stat-chip ${statusFilter === opt.value ? "active" : ""}`}
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

      {/* Content */}
      {isLoading ? (
        <div className="reports-loading">Loading...</div>
      ) : isEmpty ? (
        <div className="reports-empty">
          No {subTab === "pipelines" ? "pipeline executions" : "agent runs"}{" "}
          found
        </div>
      ) : subTab === "pipelines" ? (
        <div className="reports-table-container">
          {pipelineGroups ? (
            Array.from(pipelineGroups).map(([group, items]) => (
              <div key={group} className="reports-group">
                <div className="reports-group-header">
                  {group}{" "}
                  <span className="reports-group-count">({items.length})</span>
                </div>
                <table className="reports-table">
                  <thead>
                    <tr>
                      <th className="reports-th" style={{ width: 28 }}></th>
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
            <table className="reports-table">
              <thead>
                <tr>
                  <th className="reports-th" style={{ width: 28 }}></th>
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
        <div className="reports-table-container">
          {agentGroups ? (
            Array.from(agentGroups).map(([group, items]) => (
              <div key={group} className="reports-group">
                <div className="reports-group-header">
                  {group}{" "}
                  <span className="reports-group-count">({items.length})</span>
                </div>
                <table className="reports-table">
                  <thead>
                    <tr>
                      <th className="reports-th" style={{ width: 28 }}></th>
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
            <table className="reports-table">
              <thead>
                <tr>
                  <th className="reports-th" style={{ width: 28 }}></th>
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

      {/* Detail sidebar */}
      {selectedId && (selectedPipeline || selectedAgent) && (
        <>
          <div
            className="reports-detail-backdrop"
            onClick={() => setSelectedId(null)}
          />
          <div
            className={`reports-detail-panel ${selectedId ? "open" : ""}`}
            style={{ width: panelWidth }}
          >
            <div
              className="reports-detail-resize-handle"
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
        className="reports-th reports-th--sortable"
        onClick={() => onSort("name")}
      >
        Name{" "}
        <SortArrow column="name" sortColumn={sortCol} sortDirection={sortDir} />
      </th>
      <th className="reports-th reports-th--id" style={{ width: 120 }}>
        ID
      </th>
      <th
        className="reports-th reports-th--sortable"
        style={{ width: 140 }}
        onClick={() => onSort("time")}
      >
        Time{" "}
        <SortArrow column="time" sortColumn={sortCol} sortDirection={sortDir} />
      </th>
      <th
        className="reports-th reports-th--sortable"
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
        className="reports-th reports-th--sortable"
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
        className="reports-th reports-th--sortable"
        onClick={() => onSort("name")}
      >
        Name{" "}
        <SortArrow column="name" sortColumn={sortCol} sortDirection={sortDir} />
      </th>
      <th
        className="reports-th reports-th--sortable"
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
      <th className="reports-th reports-th--id" style={{ width: 120 }}>
        ID
      </th>
      <th
        className="reports-th reports-th--sortable"
        style={{ width: 140 }}
        onClick={() => onSort("time")}
      >
        Time{" "}
        <SortArrow column="time" sortColumn={sortCol} sortDirection={sortDir} />
      </th>
      <th
        className="reports-th reports-th--sortable"
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
        className="reports-th reports-th--sortable"
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
        className="reports-th reports-th--sortable"
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
      className={`reports-row ${selectedId === pe.id ? "reports-row--selected" : ""}`}
      onClick={() => onSelect(pe.id)}
    >
      <td className="reports-cell">
        <StatusDot status={pe.status} />
      </td>
      <td className="reports-cell reports-cell--name">{pe.pipeline_name}</td>
      <td className="reports-cell reports-cell--id">{pe.id.slice(0, 12)}</td>
      <td className="reports-cell reports-cell--time">
        {formatDateTime(pe.created_at)}
      </td>
      <td className="reports-cell reports-cell--duration">
        {pe.completed_at
          ? formatDuration(pe.created_at, pe.completed_at)
          : pe.status === "running"
            ? "..."
            : "—"}
      </td>
      <td className="reports-cell reports-cell--status-text">
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
      className={`reports-row ${selectedId === ar.id ? "reports-row--selected" : ""}`}
      onClick={() => onSelect(ar.id)}
    >
      <td className="reports-cell">
        <StatusDot status={ar.status} />
      </td>
      <td className="reports-cell reports-cell--name">
        {ar.workflow_name || ar.prompt?.slice(0, 60) || "Agent Run"}
      </td>
      <td className="reports-cell">
        <span className="reports-type-badge reports-type-badge--agent">
          {ar.provider}
        </span>
      </td>
      <td className="reports-cell reports-cell--id">{ar.id.slice(0, 12)}</td>
      <td className="reports-cell reports-cell--time">
        {formatDateTime(ar.created_at)}
      </td>
      <td className="reports-cell reports-cell--duration">
        {ar.started_at && ar.completed_at
          ? formatDuration(ar.started_at, ar.completed_at)
          : ar.status === "running"
            ? "..."
            : "—"}
      </td>
      <td className="reports-cell" style={{ textAlign: "center" }}>
        {ar.turns_used}
      </td>
      <td className="reports-cell reports-cell--status-text">
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
      <div className="reports-detail-header">
        <div className="reports-detail-header-top">
          <span className="reports-detail-id">{execution.id}</span>
          <button className="reports-detail-close" onClick={onClose}>
            <CloseIcon />
          </button>
        </div>
        <div className="reports-detail-title">{execution.pipeline_name}</div>
        <div className="reports-detail-status">
          <StatusDot status={execution.status} />
          <span className="reports-cell--status-text">
            {normalizeStatus(execution.status)}
          </span>
          {execution.cron_job_name && (
            <span className="reports-detail-trigger">
              <CronIcon /> {execution.cron_job_name}
            </span>
          )}
        </div>
      </div>

      <div className="reports-detail-body">
        {/* Trace link */}
        {(execution as any).trace_id && onNavigateToTrace && (
          <div className="reports-detail-section">
            <button
              type="button"
              className="reports-btn"
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

        {/* Approval banner — actionable, goes first */}
        {execution.status === "waiting_approval" &&
          (() => {
            const waitingStep = execution.steps.find(
              (s) => s.status === "waiting_approval" && s.approval_token,
            );
            return waitingStep?.approval_token ? (
              <div className="reports-approval">
                <div className="reports-approval-message">
                  <AlertIcon />
                  <span>
                    Step &ldquo;{waitingStep.step_id}&rdquo; requires approval
                  </span>
                </div>
                <div className="reports-approval-actions">
                  <button
                    type="button"
                    className="reports-btn reports-btn--approve"
                    onClick={() => onApprove(waitingStep.approval_token!)}
                    disabled={actionLoading === waitingStep.approval_token}
                  >
                    {actionLoading === waitingStep.approval_token
                      ? "Approving..."
                      : "Approve"}
                  </button>
                  <button
                    type="button"
                    className="reports-btn reports-btn--reject"
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

        {/* Execution report — the main content */}
        {execution.steps.length > 0 && (
          <div className="reports-detail-section">
            <span className="reports-detail-label">Execution Report</span>
            <div className="reports-detail-steps">
              {execution.steps.map((step, index) => (
                <StepDisplay key={step.id} step={step} index={index} />
              ))}
            </div>
          </div>
        )}

        {/* Error */}
        {execution.outputs_json &&
          (() => {
            try {
              const outputs = JSON.parse(execution.outputs_json);
              if (outputs.error) {
                return (
                  <div className="reports-detail-error">
                    Error: {outputs.error}
                  </div>
                );
              }
            } catch {
              /* ignore */
            }
            return null;
          })()}

        {/* Collapsible sections for config/inputs/outputs */}
        {execution.inputs_json && (
          <div className="reports-detail-section">
            <button
              type="button"
              className="reports-detail-toggle"
              onClick={() => setShowInputs(!showInputs)}
            >
              <ChevronIcon expanded={showInputs} /> Inputs
            </button>
            {showInputs && (
              <div className="reports-detail-code">
                {formatJson(execution.inputs_json)}
              </div>
            )}
          </div>
        )}

        {execution.status === "completed" && execution.outputs_json && (
          <div className="reports-detail-section">
            <button
              type="button"
              className="reports-detail-toggle"
              onClick={() => setShowOutputs(!showOutputs)}
            >
              <ChevronIcon expanded={showOutputs} /> Outputs
            </button>
            {showOutputs && (
              <div className="reports-detail-code">
                {formatJson(execution.outputs_json)}
              </div>
            )}
          </div>
        )}

        {execution.definition_json && (
          <div className="reports-detail-section">
            <button
              type="button"
              className="reports-detail-toggle"
              onClick={() => setShowConfig(!showConfig)}
            >
              <ChevronIcon expanded={showConfig} /> Pipeline Config
            </button>
            {showConfig && (
              <div className="reports-detail-code">
                {formatJson(execution.definition_json)}
              </div>
            )}
          </div>
        )}

        {execution.parent_execution_id && (
          <div className="reports-detail-section">
            <span className="reports-detail-label">Parent</span>
            <span className="reports-detail-value reports-detail-mono">
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
      <div className="reports-detail-header">
        <div className="reports-detail-header-top">
          <span className="reports-detail-id">{run.id}</span>
          <button className="reports-detail-close" onClick={onClose}>
            <CloseIcon />
          </button>
        </div>
        <div className="reports-detail-title">
          {run.workflow_name || run.prompt?.slice(0, 80) || "Agent Run"}
        </div>
        <div className="reports-detail-status">
          <StatusDot status={run.status} />
          <span className="reports-cell--status-text">
            {normalizeStatus(run.status)}
          </span>
        </div>
        <div className="reports-detail-tags">
          <span className="reports-detail-tag">{run.provider}</span>
          {run.model && <span className="reports-detail-tag">{run.model}</span>}
          <span className="reports-detail-tag">{run.mode}</span>
        </div>
      </div>

      <div className="reports-detail-body">
        {/* Cancel — actionable, first */}
        {run.status === "running" && (
          <div className="reports-detail-section">
            <button
              type="button"
              className="reports-btn reports-btn--cancel"
              onClick={() => onCancel(run.id)}
              disabled={actionLoading === run.id}
            >
              {actionLoading === run.id ? "Cancelling..." : "Cancel Agent"}
            </button>
          </div>
        )}

        {/* Summary — the execution narrative */}
        {run.summary_markdown && (
          <div className="reports-detail-section">
            <span className="reports-detail-label">Summary</span>
            <div className="reports-detail-code">{run.summary_markdown}</div>
          </div>
        )}

        {/* Error */}
        {(run.status === "error" || run.status === "timeout") && run.error && (
          <div className="reports-detail-error">Error: {run.error}</div>
        )}

        {/* Result */}
        {run.status === "success" && run.result && (
          <div className="reports-detail-section">
            <button
              type="button"
              className="reports-detail-toggle"
              onClick={() => setShowResult(!showResult)}
            >
              <ChevronIcon expanded={showResult} /> Result
            </button>
            {showResult && (
              <div className="reports-detail-code">{run.result}</div>
            )}
          </div>
        )}

        {/* Commands — what the agent actually did */}
        {detail?.commands && detail.commands.length > 0 && (
          <div className="reports-detail-section">
            <span className="reports-detail-label">
              Commands ({detail.commands.length})
            </span>
            <div className="reports-detail-commands">
              {detail.commands.map((cmd) => (
                <div key={cmd.id} className="reports-detail-command">
                  <span className="reports-detail-command-type">
                    {cmd.command_text}
                  </span>
                  <span className="reports-detail-command-time">
                    {formatTime(cmd.created_at)}
                  </span>
                  {cmd.command_text && (
                    <span className="reports-detail-command-payload">
                      {cmd.command_text.slice(0, 80)}
                    </span>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Stats — compact, not rehashing row data */}
        {totalTokens > 0 && (
          <div className="reports-detail-section">
            <span className="reports-detail-label">Usage</span>
            <div className="reports-detail-stats">
              <div className="reports-detail-stat">
                <span className="reports-detail-stat-label">Input</span>
                <span className="reports-detail-stat-value">
                  {(run.usage_input_tokens || 0).toLocaleString()}
                </span>
              </div>
              <div className="reports-detail-stat">
                <span className="reports-detail-stat-label">Output</span>
                <span className="reports-detail-stat-value">
                  {(run.usage_output_tokens || 0).toLocaleString()}
                </span>
              </div>
              {(run.usage_cache_read_tokens || 0) > 0 && (
                <div className="reports-detail-stat">
                  <span className="reports-detail-stat-label">Cache</span>
                  <span className="reports-detail-stat-value">
                    {(run.usage_cache_read_tokens || 0).toLocaleString()}
                  </span>
                </div>
              )}
              <div className="reports-detail-stat">
                <span className="reports-detail-stat-label">Tools</span>
                <span className="reports-detail-stat-value">
                  {run.tool_calls_count}
                </span>
              </div>
            </div>
          </div>
        )}

        {/* Prompt — collapsible, not the main focus */}
        {run.prompt && (
          <div className="reports-detail-section">
            <button
              type="button"
              className="reports-detail-toggle"
              onClick={() => setShowPrompt(!showPrompt)}
            >
              <ChevronIcon expanded={showPrompt} /> Prompt
            </button>
            {showPrompt && (
              <div className="reports-detail-code">{run.prompt}</div>
            )}
          </div>
        )}

        {/* Context — task, isolation, branch */}
        {(run.task_id || run.worktree_id || run.clone_id || run.git_branch) && (
          <div className="reports-detail-section">
            <span className="reports-detail-label">Context</span>
            {run.task_id && (
              <span className="reports-detail-value reports-detail-mono">
                Task: {run.task_id}
              </span>
            )}
            {run.git_branch && (
              <span className="reports-detail-value reports-detail-mono">
                Branch: {run.git_branch}
              </span>
            )}
            {(run.worktree_id || run.clone_id) && (
              <span className="reports-detail-value">
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
