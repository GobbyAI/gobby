import { memo, useEffect, useMemo, useState } from "react";
import { ResizeHandle } from "../shared/ResizeHandle";
import { Button } from "../ui/Button";
import { coarseHitAreaCls } from "../ui/controlStyles";
import { formatDateTime } from "../shared/executions/executionFormatters";
import { useCronJobs } from "../../hooks/useCronJobs";
import type { CronJob, CronRun, CronRunChild } from "../../hooks/useCronJobs";
import { cronRunStatusKind } from "./cronRunStatus";
import { ActivityPanelEmpty, CronEmptyIcon } from "./ActivityPanelEmpty";
import { ActivityToolbarSearchRow } from "./ActivityPanelSearch";
import { useRegisterActivityActions } from "./activityActions";
import { ActivityRowStatusDot } from "./ActivityRowStatusDot";
import { QuickMenu, type QuickMenuItem } from "./QuickMenu";

interface CronTabProps {
  projectId?: string | null;
}

const FILTER_OPTIONS = [
  { id: "enabled", label: "Enabled" },
  { id: "disabled", label: "Disabled" },
] as const;

type StatusFilter = (typeof FILTER_OPTIONS)[number]["id"];

const PAGE_SIZE = 20;

export const CronTab = memo(function CronTab({ projectId }: CronTabProps) {
  const {
    jobs,
    selectedJob,
    selectJob,
    runs,
    isRunsLoading,
    isLoading,
    toggleJob,
    deleteJob,
    updateJob,
  } = useCronJobs(projectId);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("enabled");
  const [topHeight, setTopHeight] = useState(50);
  const [displayLimitState, setDisplayLimitState] = useState<{
    filter: StatusFilter;
    limit: number;
  }>({ filter: "enabled", limit: PAGE_SIZE });
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    let interval: number | null = null;
    const stopTimer = () => {
      if (interval !== null) {
        window.clearInterval(interval);
        interval = null;
      }
    };
    const startTimer = () => {
      stopTimer();
      setNow(Date.now());
      interval = window.setInterval(() => setNow(Date.now()), 15_000);
    };
    const syncTimer = () => {
      if (document.visibilityState === "visible") {
        startTimer();
      } else {
        stopTimer();
      }
    };

    syncTimer();
    document.addEventListener("visibilitychange", syncTimer);
    return () => {
      stopTimer();
      document.removeEventListener("visibilitychange", syncTimer);
    };
  }, []);

  const [search, setSearch] = useState("");
  const [searchOpen, setSearchOpen] = useState(false);
  const closeSearch = () => {
    setSearchOpen(false);
    setSearch("");
  };

  useRegisterActivityActions(
    {
      selector: {
        value: statusFilter,
        onChange: (value) => setStatusFilter(value as StatusFilter),
        options: FILTER_OPTIONS.map((o) => ({ value: o.id, label: o.label })),
        ariaLabel: "Cron status filter",
      },
      search: {
        open: searchOpen,
        onToggle: searchOpen ? closeSearch : () => setSearchOpen(true),
        ariaLabel: "Search cron jobs",
      },
    },
    [statusFilter, searchOpen],
  );

  const query = search.trim().toLowerCase();
  const filteredJobs = useMemo(
    () =>
      jobs.filter((j) => {
        if (statusFilter === "enabled" ? !j.enabled : j.enabled) return false;
        if (!query) return true;
        return [
          j.name,
          j.display_name ?? "",
          j.description ?? "",
          j.cron_expr ?? "",
        ].some((field) => field.toLowerCase().includes(query));
      }),
    [jobs, statusFilter, query],
  );

  const displayLimit =
    displayLimitState.filter === statusFilter
      ? displayLimitState.limit
      : PAGE_SIZE;

  const visibleJobs = useMemo(
    () => filteredJobs.slice(0, displayLimit),
    [filteredJobs, displayLimit],
  );
  const hasMore = filteredJobs.length > visibleJobs.length;

  // Keep the selection inside the visible rows: default-select the topmost
  // job so the runs pane is populated as soon as the tab opens (#19152).
  useEffect(() => {
    if (filteredJobs.length === 0) {
      if (selectedJob) selectJob(null);
      return;
    }
    if (!selectedJob || !filteredJobs.some((j) => j.id === selectedJob.id)) {
      selectJob(filteredJobs[0]);
    }
  }, [filteredJobs, selectedJob, selectJob]);

  if (isLoading && jobs.length === 0) {
    return <ActivityPanelEmpty body="Loading cron jobs…" />;
  }

  return (
    <div className="flex h-full flex-col">
      {searchOpen && (
        <ActivityToolbarSearchRow
          value={search}
          onChange={setSearch}
          placeholder="Search cron jobs"
          ariaLabel="Search cron jobs"
          onClose={closeSearch}
        />
      )}

      <div
        className={`overflow-y-auto ${selectedJob ? "border-b border-border" : "flex-1"}`}
        style={selectedJob ? { height: `${topHeight}%` } : undefined}
      >
        {filteredJobs.length === 0 ? (
          <ActivityPanelEmpty
            icon={<CronEmptyIcon />}
            heading="Cron Jobs"
            body={
              jobs.length === 0
                ? "Cron jobs appear here when scheduled"
                : query
                  ? "No cron jobs match your search"
                  : `No ${statusFilter} cron jobs yet`
            }
          />
        ) : (
          <>
            {visibleJobs.map((job) => {
              const jobLabel = job.display_name ?? job.name;
              const renameJob = () => {
                const next = window.prompt(
                  "Rename cron job (empty resets to the default name)",
                  jobLabel,
                );
                if (next === null || next === jobLabel) return;
                void updateJob(job.id, { display_name: next.trim() });
              };
              // System crons reject operator toggle and delete server-side,
              // so they only get the rename action (identifier stays stable).
              const menuItems: QuickMenuItem[] = job.is_system
                ? [{ label: "Rename", onSelect: renameJob }]
                : [
                    { label: "Rename", onSelect: renameJob },
                    {
                      label: job.enabled ? "Disable" : "Enable",
                      onSelect: () => void toggleJob(job.id),
                    },
                    { type: "separator" },
                    {
                      label: "Delete",
                      destructive: true,
                      onSelect: () => {
                        if (window.confirm(`Delete cron job "${job.name}"?`)) {
                          void deleteJob(job.id);
                        }
                      },
                    },
                  ];
              return (
                <div
                  key={job.id}
                  className={`activity-list-row${selectedJob?.id === job.id ? "activity-list-row--selected" : ""}`}
                >
                  <Button
                    type="button"
                    variant="ghost"
                    className={`activity-list-row__body ${coarseHitAreaCls}`}
                    aria-label={`Select ${jobLabel}`}
                    onClick={() => selectJob(job)}
                  >
                    <CronStatusDot enabled={job.enabled} />
                    <span className="activity-row-title">{jobLabel}</span>
                    <span className="activity-row-meta shrink-0">
                      {formatNextFiring(job, now)}
                    </span>
                  </Button>
                  <div className="flex items-center px-1">
                    <QuickMenu
                      items={menuItems}
                      menuLabel={`Actions for ${jobLabel}`}
                      triggerLabel={`Open actions for ${jobLabel}`}
                    />
                  </div>
                </div>
              );
            })}
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

      {selectedJob && (
        <ResizeHandle
          direction="vertical"
          onResize={setTopHeight}
          panelHeight={topHeight}
          minHeight={15}
          maxHeight={80}
        />
      )}

      {selectedJob && (
        <div className="flex min-h-0 flex-1 flex-col">
          <div className="flex h-10 items-center gap-3 border-b border-border bg-[var(--bg-secondary)] px-3">
            <div className="flex min-w-0 items-center gap-2">
              <CronStatusDot enabled={selectedJob.enabled} />
              <span className="activity-row-title">
                {selectedJob.display_name ?? selectedJob.name}
              </span>
              {selectedJob.display_name != null &&
                selectedJob.display_name !== selectedJob.name && (
                  <span
                    className="activity-row-meta truncate font-mono"
                    title={selectedJob.name}
                  >
                    {selectedJob.name}
                  </span>
                )}
              <span className="activity-row-meta shrink-0">
                {selectedJob.schedule_type === "cron"
                  ? selectedJob.cron_expr
                  : selectedJob.schedule_type === "interval"
                    ? `every ${selectedJob.interval_seconds ?? "?"}s`
                    : selectedJob.schedule_type === "once"
                      ? "once"
                      : ""}
              </span>
            </div>
          </div>
          <div className="flex-1 overflow-y-auto">
            {isRunsLoading && runs.length === 0 ? (
              <p className="p-2 text-xs text-muted-foreground">
                Loading runs...
              </p>
            ) : runs.length === 0 ? (
              <p className="p-2 text-xs text-muted-foreground">No runs yet</p>
            ) : (
              <ul className="m-0 list-none p-0">
                {runs.map((run) => (
                  <li
                    key={run.id}
                    className="flex min-w-0 items-center gap-2 border-b border-[var(--border)] px-3 py-1.5 text-[length:var(--text-sm)] font-[var(--font-weight-normal)] last:border-b-0"
                  >
                    <RunStatusGlyph status={run.status} />
                    <span className="flex min-w-0 flex-1 items-center gap-2">
                      <span className="min-w-0 truncate text-[var(--text-secondary)]">
                        {formatDateTime(run.triggered_at)}
                      </span>
                      {run.child && <RunChildStatus child={run.child} />}
                    </span>
                    <span className="shrink-0 text-[var(--text-muted)] capitalize tabular-nums">
                      {formatRunStatus(run.status)}
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

function CronStatusDot({ enabled }: { enabled: boolean }) {
  if (enabled) {
    return <ActivityRowStatusDot kind="success" pulse label="Enabled" />;
  }
  return <ActivityRowStatusDot kind="disabled" label="Disabled" />;
}

function RunStatusGlyph({ status }: { status: string }) {
  const kind = cronRunStatusKind(status);
  if (kind === "success") {
    return <ActivityRowStatusDot kind="success" label="Success" />;
  }
  if (kind === "failure") {
    return <ActivityRowStatusDot kind="error" label="Failure" />;
  }
  if (kind === "running") {
    return <ActivityRowStatusDot kind="info" pulse label="Running" />;
  }
  if (kind === "dispatched") {
    return <ActivityRowStatusDot kind="info" label="Dispatched" />;
  }
  if (kind === "skipped") {
    return <ActivityRowStatusDot kind="warning" label="Skipped" />;
  }
  if (kind === "interrupted") {
    return <ActivityRowStatusDot kind="warning" label="Interrupted" />;
  }
  return <ActivityRowStatusDot kind="disabled" label={status} />;
}

function RunChildStatus({ child }: { child: CronRunChild }) {
  const status = child.missing ? "missing" : (child.status ?? "unknown");
  return (
    <span className="inline-flex min-w-0 items-center gap-1 text-[length:var(--text-xs)] whitespace-nowrap text-[var(--text-muted)] [&>span:last-child]:truncate">
      <ActivityRowStatusDot
        kind={childStatusKind(child)}
        pulse={!child.terminal && !child.missing}
        label={formatChildLabel(child)}
      />
      <span>
        {formatChildType(child.type)} {formatRunStatus(status)}
      </span>
    </span>
  );
}

function childStatusKind(child: CronRunChild) {
  if (child.missing) return "warning";
  if (child.terminal) {
    const statusKind = cronRunStatusKind(child.status ?? "");
    if (statusKind === "success") return "success";
    if (statusKind === "failure") return "error";
    if (statusKind === "skipped") return "warning";
    return "disabled";
  }
  return "info";
}

function formatChildLabel(child: CronRunChild): string {
  const status = child.missing ? "missing" : (child.status ?? "unknown");
  return `${formatChildType(child.type)} ${formatRunStatus(status)}`;
}

function formatChildType(type: CronRunChild["type"]): string {
  return type === "pipeline_execution" ? "pipeline" : "agent";
}

function formatRunStatus(status: string): string {
  return status.replace(/_/g, " ");
}

function formatNextFiring(job: CronJob, now: number): string {
  if (!job.enabled) return "disabled";
  if (!job.next_run_at) return "—";
  const ms = new Date(job.next_run_at).getTime() - now;
  if (ms <= 0) return "due";
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return "in <1m";
  const min = Math.floor(sec / 60);
  if (min < 60) return `in ${min}m`;
  const hr = Math.floor(min / 60);
  if (hr < 24) {
    const remMin = min % 60;
    return remMin > 0 ? `in ${hr}h ${remMin}m` : `in ${hr}h`;
  }
  const day = Math.floor(hr / 24);
  return `in ${day}d`;
}

export type { CronJob, CronRun };
