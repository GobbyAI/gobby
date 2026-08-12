import { memo, useMemo, useState } from "react";
import { ResizeHandle } from "../shared/ResizeHandle";
import { Button } from "../ui/Button";
import { coarseHitAreaCls } from "../ui/controlStyles";
import { formatTime } from "../shared/executions/executionFormatters";
import { useTraces, useTraceDetail } from "../../hooks/useTraces";
import type { TraceRecord, SpanRecord } from "../../hooks/useTraces";
import { useRegisterActivityActions } from "./activityActions";
import { ActivityPanelEmpty, TracesEmptyIcon } from "./ActivityPanelEmpty";
import { ActivityRowStatusDot } from "./ActivityRowStatusDot";

interface TracesTabProps {
  projectId?: string | null;
}

const FILTER_OPTIONS = [
  { id: "all", label: "All" },
  { id: "OK", label: "OK" },
  { id: "ERROR", label: "Error" },
] as const;

type StatusFilter = (typeof FILTER_OPTIONS)[number]["id"];

const PAGE_SIZE = 20;

export const TracesTab = memo(function TracesTab({
  projectId,
}: TracesTabProps) {
  const { traces, isLoading, error, selectedTraceId, setSelectedTraceId } =
    useTraces(projectId ?? undefined);
  const {
    spans,
    isLoading: isDetailLoading,
    error: detailError,
  } = useTraceDetail(selectedTraceId);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [topHeight, setTopHeight] = useState(50);
  const [displayLimitState, setDisplayLimitState] = useState<{
    filter: StatusFilter;
    limit: number;
  }>({ filter: "all", limit: PAGE_SIZE });

  const filteredTraces = useMemo(() => {
    const sorted = [...traces].sort(
      (a, b) =>
        new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime(),
    );
    if (statusFilter === "all") return sorted;
    return sorted.filter((t) => t.status === statusFilter);
  }, [traces, statusFilter]);

  const displayLimit =
    displayLimitState.filter === statusFilter
      ? displayLimitState.limit
      : PAGE_SIZE;

  const visibleTraces = useMemo(
    () => filteredTraces.slice(0, displayLimit),
    [filteredTraces, displayLimit],
  );
  const hasMore = filteredTraces.length > visibleTraces.length;

  const selectedTrace = useMemo(
    () => filteredTraces.find((t) => t.trace_id === selectedTraceId) ?? null,
    [filteredTraces, selectedTraceId],
  );

  function handleStatusFilterChange(nextStatusFilter: StatusFilter): void {
    setStatusFilter(nextStatusFilter);
    setDisplayLimitState({ filter: nextStatusFilter, limit: PAGE_SIZE });
  }

  useRegisterActivityActions(
    {
      selector: {
        value: statusFilter,
        onChange: (value) => handleStatusFilterChange(value as StatusFilter),
        options: FILTER_OPTIONS.map((o) => ({ value: o.id, label: o.label })),
        ariaLabel: "Trace status filter",
      },
    },
    [statusFilter],
  );

  if (isLoading && traces.length === 0) {
    return <ActivityPanelEmpty body="Loading traces…" />;
  }

  return (
    <div className="flex h-full flex-col">
      {error && (
        <p
          role="alert"
          className="border-b border-border px-3 py-1.5 text-xs text-destructive"
        >
          {error}
        </p>
      )}

      <div
        className={`overflow-y-auto ${selectedTrace ? "border-b border-border" : "flex-1"}`}
        style={selectedTrace ? { height: `${topHeight}%` } : undefined}
      >
        {filteredTraces.length === 0 ? (
          <ActivityPanelEmpty
            icon={<TracesEmptyIcon />}
            heading="Traces"
            body={
              statusFilter === "all"
                ? "Tool-call traces appear here as agents work"
                : `No ${statusFilter} traces yet`
            }
          />
        ) : (
          <>
            {visibleTraces.map((trace) => (
              <Button
                key={trace.trace_id}
                type="button"
                variant="ghost"
                data-testid="trace-row-button"
                className={`flex min-h-[var(--activity-panel-row-height)] w-full cursor-pointer appearance-none items-center justify-between gap-2 rounded-none border-0 border-b border-[var(--border)] bg-transparent px-3 py-2 text-left [color:inherit] transition-colors duration-100 [font:inherit] hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11 pointer-coarse:min-w-11 ${selectedTraceId === trace.trace_id ? "bg-[color-mix(in_srgb,var(--accent)_8%,transparent)] hover:bg-[color-mix(in_srgb,var(--accent)_8%,transparent)]" : ""} ${coarseHitAreaCls}`}
                onClick={() => setSelectedTraceId(trace.trace_id)}
              >
                <div className="flex min-w-0 items-center gap-2">
                  <TraceStatusDot status={trace.status} />
                  <span className="truncate text-sm text-foreground">
                    {trace.root_span_name || "Unknown span"}
                  </span>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <span className="text-2xs text-muted-foreground tabular-nums">
                    {formatDurationMs(trace.duration_ms)}
                  </span>
                  <span className="text-2xs text-muted-foreground">
                    {formatTime(trace.timestamp)}
                  </span>
                </div>
              </Button>
            ))}
            {hasMore && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className={`w-full rounded-none py-2 text-xs text-muted-foreground transition-colors hover:bg-muted/30 hover:text-foreground ${coarseHitAreaCls}`}
                onClick={() =>
                  setDisplayLimitState({
                    filter: statusFilter,
                    limit: displayLimit + PAGE_SIZE,
                  })
                }
              >
                Load more
              </Button>
            )}
          </>
        )}
      </div>

      {selectedTrace && (
        <ResizeHandle
          direction="vertical"
          onResize={setTopHeight}
          panelHeight={topHeight}
          minHeight={15}
          maxHeight={80}
        />
      )}

      {selectedTrace && (
        <div className="flex min-h-0 flex-1 flex-col">
          <div className="flex h-10 items-center gap-3 border-b border-border bg-[var(--bg-secondary)] px-3">
            <div className="flex min-w-0 items-center gap-2">
              <TraceStatusDot status={selectedTrace.status} />
              <span className="truncate text-xs font-medium text-foreground">
                {selectedTrace.root_span_name ||
                  selectedTrace.trace_id.slice(0, 8)}
              </span>
              <span className="shrink-0 text-2xs text-muted-foreground tabular-nums">
                {formatDurationMs(selectedTrace.duration_ms)}
              </span>
            </div>
          </div>
          <div className="flex-1 overflow-y-auto">
            {detailError ? (
              <p role="alert" className="p-2 text-xs text-destructive">
                {detailError}
              </p>
            ) : isDetailLoading && spans.length === 0 ? (
              <p className="p-2 text-xs text-muted-foreground">
                Loading spans...
              </p>
            ) : spans.length === 0 ? (
              <p className="p-2 text-xs text-muted-foreground">No spans</p>
            ) : (
              <ul className="m-0 list-none p-0">
                {spans.map((span) => (
                  <li
                    key={span.id}
                    className="flex min-w-0 items-center gap-2 border-b border-[var(--border)] px-3 py-1.5 text-[length:var(--text-xs)] last:border-b-0"
                  >
                    <TraceStatusDot status={span.status} />
                    <span
                      className="min-w-0 flex-1 truncate text-[var(--text-primary)]"
                      title={span.name}
                    >
                      {span.name}
                    </span>
                    <span className="shrink-0 text-[var(--text-muted)] tabular-nums">
                      {formatDurationNs(
                        Math.max(0, span.end_time_ns - span.start_time_ns),
                      )}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </div>
  );
});

function TraceStatusDot({
  status,
}: {
  status: SpanRecord["status"] | TraceRecord["status"];
}) {
  if (status === "OK") {
    return <ActivityRowStatusDot kind="success" label="OK" />;
  }
  if (status === "ERROR") {
    return <ActivityRowStatusDot kind="error" label="Error" />;
  }
  return <ActivityRowStatusDot kind="disabled" label="Unset" />;
}

function formatDurationMs(ms: number): string {
  if (ms < 1) return "<1ms";
  if (ms < 1000) return `${ms.toFixed(0)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(2)}s`;
  return `${(ms / 60_000).toFixed(1)}m`;
}

function formatDurationNs(ns: number): string {
  return formatDurationMs(ns / 1_000_000);
}
