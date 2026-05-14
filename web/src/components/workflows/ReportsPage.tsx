import { useState, useMemo, useCallback, useEffect } from "react";
import { usePipelineExecutions } from "../../hooks/usePipelineExecutions";
import type { PipelineExecutionRecord } from "../../hooks/usePipelineExecutions";
import { useAgentRuns } from "../../hooks/useAgentRuns";
import type { AgentRunRecord, AgentRunDetail } from "../../hooks/useAgentRuns";
import { PipelineStatusDot as StatusDot } from "./execution-utils";
import { formatDuration } from "./executionFormatters";
import { SegmentedControl } from "../ui/SegmentedControl";
import { cn } from "../../lib/utils";
import { AgentDetail } from "./ReportsPage.AgentDetail";
import { PipelineDetail } from "./ReportsPage.PipelineDetail";
import {
  compareAgents,
  comparePipelines,
  formatDateTime,
  groupBy,
  normalizeStatus,
  STATUS_OPTIONS,
  statusMatchesFilter,
} from "./ReportsPage.helpers";
import type {
  AgentSortColumn,
  GroupBy,
  PipelineSortColumn,
  SortDirection,
  StatusFilter,
  SubTab,
} from "./ReportsPage.helpers";
import { SortArrow } from "./ReportsPage.icons";
import { useResizablePanel } from "./ReportsPage.useResizablePanel";
import {
  CELL_BASE_CLS,
  CELL_DURATION_CLS,
  CELL_ID_CLS,
  CELL_NAME_CLS,
  CELL_STATUS_CLS,
  CELL_TIME_CLS,
  DETAIL_BACKDROP_CLS,
  DETAIL_PANEL_BASE_CLS,
  DETAIL_PANEL_OPEN_CLS,
  DETAIL_RESIZE_HANDLE_CLS,
  FILTER_BAR_CLS,
  FILTER_CHIPS_CLS,
  GROUP_CLS,
  GROUP_COUNT_CLS,
  GROUP_HEADER_CLS,
  GROUP_LABEL_CLS,
  GROUP_SELECT_CLS,
  GROUP_TOGGLE_CLS,
  LOADING_EMPTY_CLS,
  PAGE_CLS,
  ROW_BASE_CLS,
  ROW_SELECTED_CLS,
  SEARCH_CLS,
  STAT_CHIP_ACTIVE_CLS,
  STAT_CHIP_BASE_CLS,
  TABLE_CLS,
  TABLE_CONTAINER_CLS,
  TH_BASE_CLS,
  TH_ID_CLS,
  TH_SORTABLE_CLS,
  TITLE_CLS,
  TOOLBAR_CLS,
  TOOLBAR_LEFT_CLS,
  TOOLBAR_RIGHT_CLS,
  TYPE_BADGE_AGENT_CLS,
  TYPE_BADGE_BASE_CLS,
} from "./ReportsPage.styles";
import "./reports-page.css";
import { Heading } from '../shared/Heading'

export function ReportsPage({
  projectId,
  onNavigateToTrace,
  initialPipelineExecutionId,
  onInitialPipelineExecutionConsumed,
}: {
  projectId?: string;
  onNavigateToTrace?: (traceId: string) => void;
  initialPipelineExecutionId?: string | null;
  onInitialPipelineExecutionConsumed?: () => void;
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

  useEffect(() => {
    if (!initialPipelineExecutionId) return;
    setSubTab("pipelines");
    setStatusFilter("all");
    setSelectedId(initialPipelineExecutionId);
    onInitialPipelineExecutionConsumed?.();
  }, [initialPipelineExecutionId, onInitialPipelineExecutionConsumed]);

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
          <Heading level={1} className={TITLE_CLS}>Reports</Heading>
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
              className={cn(STAT_CHIP_BASE_CLS, statusFilter === opt.value && STAT_CHIP_ACTIVE_CLS)}
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
                      <th className={TH_BASE_CLS} style={{ width: 28 }} aria-label="Select"></th>
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
                  <th className={TH_BASE_CLS} style={{ width: 28 }} aria-label="Select"></th>
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
                      <th className={TH_BASE_CLS} style={{ width: 28 }} aria-label="Select"></th>
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
                  <th className={TH_BASE_CLS} style={{ width: 28 }} aria-label="Select"></th>
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
          <aside
            className={cn(DETAIL_PANEL_BASE_CLS, selectedId && DETAIL_PANEL_OPEN_CLS)}
            style={{ width: panelWidth }}
            aria-label={selectedPipeline ? "Pipeline details" : "Agent run details"}
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
          </aside>
        </>
      )}
    </main>
  );
}

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
        className={cn(TH_BASE_CLS, TH_SORTABLE_CLS)}
        onClick={() => onSort("name")}
      >
        Name{" "}
        <SortArrow column="name" sortColumn={sortCol} sortDirection={sortDir} />
      </th>
      <th className={cn(TH_BASE_CLS, TH_ID_CLS)} style={{ width: 120 }}>
        ID
      </th>
      <th
        className={cn(TH_BASE_CLS, TH_SORTABLE_CLS)}
        style={{ width: 140 }}
        onClick={() => onSort("time")}
      >
        Time{" "}
        <SortArrow column="time" sortColumn={sortCol} sortDirection={sortDir} />
      </th>
      <th
        className={cn(TH_BASE_CLS, TH_SORTABLE_CLS, "max-md:hidden")}
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
        className={cn(TH_BASE_CLS, TH_SORTABLE_CLS)}
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
        className={cn(TH_BASE_CLS, TH_SORTABLE_CLS)}
        onClick={() => onSort("name")}
      >
        Name{" "}
        <SortArrow column="name" sortColumn={sortCol} sortDirection={sortDir} />
      </th>
      <th
        className={cn(TH_BASE_CLS, TH_SORTABLE_CLS)}
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
      <th className={cn(TH_BASE_CLS, TH_ID_CLS)} style={{ width: 120 }}>
        ID
      </th>
      <th
        className={cn(TH_BASE_CLS, TH_SORTABLE_CLS)}
        style={{ width: 140 }}
        onClick={() => onSort("time")}
      >
        Time{" "}
        <SortArrow column="time" sortColumn={sortCol} sortDirection={sortDir} />
      </th>
      <th
        className={cn(TH_BASE_CLS, TH_SORTABLE_CLS, "max-md:hidden")}
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
        className={cn(TH_BASE_CLS, TH_SORTABLE_CLS)}
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
        className={cn(TH_BASE_CLS, TH_SORTABLE_CLS)}
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
      className={cn("reports-row", ROW_BASE_CLS, selectedId === pe.id && ROW_SELECTED_CLS)}
      onClick={() => onSelect(pe.id)}
    >
      <td className={CELL_BASE_CLS} data-label="">
        <StatusDot status={pe.status} />
      </td>
      <td className={cn(CELL_BASE_CLS, CELL_NAME_CLS)} data-label="Name">{pe.pipeline_name}</td>
      <td className={cn(CELL_BASE_CLS, CELL_ID_CLS)} data-label="ID">{pe.id.slice(0, 12)}</td>
      <td className={cn(CELL_BASE_CLS, CELL_TIME_CLS)} data-label="Time">
        {formatDateTime(pe.created_at)}
      </td>
      <td className={cn(CELL_BASE_CLS, CELL_DURATION_CLS)} data-label="Duration">
        {pe.completed_at
          ? formatDuration(pe.created_at, pe.completed_at)
          : pe.status === "running"
            ? "..."
            : "—"}
      </td>
      <td className={cn(CELL_BASE_CLS, CELL_STATUS_CLS)} data-label="Status">
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
      className={cn("reports-row", ROW_BASE_CLS, selectedId === ar.id && ROW_SELECTED_CLS)}
      onClick={() => onSelect(ar.id)}
    >
      <td className={CELL_BASE_CLS} data-label="">
        <StatusDot status={ar.status} />
      </td>
      <td className={cn(CELL_BASE_CLS, CELL_NAME_CLS)} data-label="Name">
        {ar.workflow_name || ar.prompt?.slice(0, 60) || "Agent Run"}
      </td>
      <td className={CELL_BASE_CLS} data-label="Provider">
        <span className={cn(TYPE_BADGE_BASE_CLS, TYPE_BADGE_AGENT_CLS)}>
          {ar.provider}
        </span>
      </td>
      <td className={cn(CELL_BASE_CLS, CELL_ID_CLS)} data-label="ID">{ar.id.slice(0, 12)}</td>
      <td className={cn(CELL_BASE_CLS, CELL_TIME_CLS)} data-label="Time">
        {formatDateTime(ar.created_at)}
      </td>
      <td className={cn(CELL_BASE_CLS, CELL_DURATION_CLS)} data-label="Duration">
        {ar.started_at && ar.completed_at
          ? formatDuration(ar.started_at, ar.completed_at)
          : ar.status === "running"
            ? "..."
            : "—"}
      </td>
      <td className={CELL_BASE_CLS} data-label="Turns" style={{ textAlign: "center" }}>
        {ar.turns_used}
      </td>
      <td className={cn(CELL_BASE_CLS, CELL_STATUS_CLS)} data-label="Status">
        {normalizeStatus(ar.status)}
      </td>
    </tr>
  );
}
