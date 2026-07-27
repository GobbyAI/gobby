/**
 * Research mode (plan wiki-obsidian-panel §5.2): launch and monitor
 * wiki-research pipeline runs without leaving the panel.
 *
 * Layout top→bottom: composer card (question textarea + Options disclosure
 * feeding the pipeline inputs), the live run zone (per-step progress with the
 * Pipelines-tab escape hatch, or the completion strip, or a recovery
 * callout), then past runs merging executions history with `*-run-report.md`
 * vault outputs. Reports open in the shared WikiPageReader in place; report
 * wikilinks navigate through `nav` (auto mode-flip to the browse modes).
 *
 * Single-flight research composer: while a live execution exists the
 * composer disables with "A research run is in progress" — mirroring the
 * pipeline's own re-entrancy guard instead of racing it.
 *
 * Resilient monitoring: `usePipelineExecutions` refetches on pipeline WS
 * events, and a 10s polling fallback runs while a run is live or the
 * WebSocket is disconnected — monitoring never depends on the socket alone.
 * A live run with no progress inside the stall window, or one the startup
 * sweep marked failed after a daemon restart, surfaces a recovery callout
 * with Refresh and Dismiss; the composer re-enables whenever no live
 * execution remains.
 */

import { useEffect, useMemo, useRef, useState } from "react";

import {
  usePipelineExecutions,
  type PipelineExecutionRecord,
} from "../../../hooks/usePipelineExecutions";
import { useNow } from "../../../hooks/useNow";
import { useWebSocketConnected } from "../../../hooks/useWebSocketEvent";
import { getExecStatusKind } from "../../../lib/pipelineColors";
import {
  fetchProviderModelCatalog,
  getModelsForProvider,
  getOrderedProviders,
  type ProviderModelEntry,
} from "../../../lib/providerModels";
import { formatRelativeTime } from "../../../utils/formatTime";
import { ActivityRowStatusDot } from "../ActivityRowStatusDot";
import { showActivityTab } from "../activityEvents";
import { Switch } from "../../ui/Switch";
import { WikiPageReader } from "./WikiPageReader";
import { fetchPages, type WikiFetchScope, type WikiPagesResult } from "./WikiTabData";
import type { WikiTabActions } from "./WikiTabActions";
import { buildNodeIndex, resolveWikilinkTarget, type WikiOutputMeta } from "./WikiTabModel";
import type { WikiNav } from "./WikiTabState";

const RESEARCH_PIPELINE = "wiki-research";
const POLL_INTERVAL_MS = 10_000;
/** Bounded no-progress window before a live run is flagged as stalled. */
const STALL_AFTER_MS = 60_000;
const RUN_REPORT_RE = /-run-report\.md$/;
/** Startup-sweep marker written into outputs_json when a daemon restart
 * orphans a RUNNING execution (storage.fail_stale_running_executions). */
const RESTART_MARKER = "Daemon restart";

const LIVE_STATUSES = new Set(["pending", "running", "waiting_approval"]);

/** The pipeline's meaningful steps; re-entrancy/failure guards are hidden. */
const STEP_LABELS: ReadonlyArray<readonly [string, string]> = [
  ["create_research_task", "Create research task"],
  ["spawn_researcher", "Spawn researcher"],
  ["wait_researcher", "Wait for researcher"],
];

const ghostButton =
  "rounded-md border border-border px-2 py-1 text-xs text-muted-foreground " +
  "hover:bg-muted hover:text-foreground disabled:opacity-40";

const accentButton =
  "rounded-md border border-[var(--accent)] bg-[var(--accent)] px-3 py-1 text-xs " +
  "font-medium text-[var(--accent-foreground)] hover:border-[var(--accent-hover)] " +
  "hover:bg-[var(--accent-hover)] disabled:opacity-50";

const fieldInput =
  "rounded-md border border-border bg-transparent px-2 py-1 text-sm text-foreground " +
  "disabled:opacity-60";

// ── Execution helpers ───────────────────────────────────────────

function parseJsonRecord(raw: string | null): Record<string, unknown> {
  if (!raw) return {};
  try {
    const parsed: unknown = JSON.parse(raw);
    return typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

function recordText(record: Record<string, unknown>, key: string): string | null {
  const value = record[key];
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function executionQuestion(run: PipelineExecutionRecord): string {
  return recordText(parseJsonRecord(run.inputs_json), "question") ?? "(no question)";
}

/** Compiled-topic slug: outputs_json wins when the researcher reported one,
 * else the launch input. Null when the run never named a topic. */
function executionTopicSlug(run: PipelineExecutionRecord): string | null {
  const outputs = parseJsonRecord(run.outputs_json ?? null);
  const fromOutputs =
    recordText(outputs, "topic_slug") ??
    recordText(outputs, "topic_path")?.replace(/^knowledge\/topics\//, "").replace(/\.md$/, "") ??
    null;
  if (fromOutputs) return fromOutputs;
  return recordText(parseJsonRecord(run.inputs_json), "topic_slug");
}

function isRestartOrphan(run: PipelineExecutionRecord): boolean {
  if (run.status !== "failed" && run.status !== "interrupted") return false;
  const error = recordText(parseJsonRecord(run.outputs_json ?? null), "error");
  return error !== null && error.includes(RESTART_MARKER);
}

/** Anything the daemon moves on a heartbeat: status, updated_at, step states.
 * An unchanged signature across the stall window means no observable progress. */
function progressSignature(run: PipelineExecutionRecord): string {
  const steps = run.steps.map((step) => `${step.step_id}=${step.status}`).join(",");
  return `${run.id}:${run.status}:${run.updated_at}:${steps}`;
}

function sortResearchExecutions(
  executions: PipelineExecutionRecord[],
): PipelineExecutionRecord[] {
  return executions
    .filter((run) => run.pipeline_name === RESEARCH_PIPELINE)
    .slice()
    .sort((a, b) => (Date.parse(b.created_at) || 0) - (Date.parse(a.created_at) || 0));
}

function formatDuration(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

function executionDuration(run: PipelineExecutionRecord): string | null {
  if (!run.completed_at) return null;
  const started = Date.parse(run.created_at);
  const finished = Date.parse(run.completed_at);
  if (Number.isNaN(started) || Number.isNaN(finished)) return null;
  return formatDuration(finished - started);
}

function reportName(path: string): string {
  const base = path.split("/").filter(Boolean).pop() ?? path;
  return base.endsWith(".md") ? base.slice(0, -3) : base;
}

function parsePositiveInt(raw: string, fallback: number): number {
  const value = Number.parseInt(raw, 10);
  return Number.isFinite(value) && value > 0 ? value : fallback;
}

type PastRow =
  | { kind: "execution"; key: string; ts: number; run: PipelineExecutionRecord }
  | { kind: "report"; key: string; ts: number; report: WikiOutputMeta };

// ── Icons ───────────────────────────────────────────────────────

function WarningIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      aria-hidden="true"
      className="shrink-0"
    >
      <path d="M8 1.5 15 14H1L8 1.5Z" strokeLinejoin="round" />
      <path d="M8 6v4" strokeLinecap="round" />
      <circle cx="8" cy="12" r="0.5" fill="currentColor" stroke="none" />
    </svg>
  );
}

function ChevronLeftIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      aria-hidden="true"
      className="shrink-0"
    >
      <path d="M10 3 5 8l5 5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// ── Component ───────────────────────────────────────────────────

interface WikiResearchModeProps {
  scope: WikiFetchScope;
  nav: WikiNav;
  offline: boolean;
  actions: WikiTabActions;
  onOpenGraph: () => void;
}

export function WikiResearchMode({
  scope,
  nav,
  offline,
  actions,
  onOpenGraph,
}: WikiResearchModeProps) {
  const [question, setQuestion] = useState("");
  const [optionsOpen, setOptionsOpen] = useState(false);
  const [topicSlug, setTopicSlug] = useState("");
  const [maxSources, setMaxSources] = useState("12");
  const [maxItems, setMaxItems] = useState("8");
  const [createTasks, setCreateTasks] = useState(false);
  const [provider, setProvider] = useState("claude");
  const [model, setModel] = useState("");
  const [launching, setLaunching] = useState(false);
  const [readingPath, setReadingPath] = useState<string | null>(null);
  const [catalog, setCatalog] = useState<ProviderModelEntry[]>([]);
  const [vault, setVault] = useState<WikiPagesResult | null>(null);
  // Keyed to the live run's progress signature so any observed progress
  // clears the stall flag by derivation — no reset effect.
  const [stalledSignature, setStalledSignature] = useState<string | null>(null);
  const [dismissedRecovery, setDismissedRecovery] = useState<string | null>(null);

  const { executions, refetch } = usePipelineExecutions({
    projectId: scope.projectId ?? undefined,
    pipelineName: RESEARCH_PIPELINE,
  });
  const wsConnected = useWebSocketConnected();

  const researchRuns = useMemo(() => sortResearchExecutions(executions), [executions]);
  const liveRun = researchRuns.find((run) => LIVE_STATUSES.has(run.status)) ?? null;
  const newestRun = researchRuns[0] ?? null;
  const completedRun = !liveRun && newestRun?.status === "completed" ? newestRun : null;
  const orphanedRun = !liveRun && newestRun && isRestartOrphan(newestRun) ? newestRun : null;

  // Elapsed ticker only while a run is live (0 disables the interval).
  const now = useNow(liveRun ? 1000 : 0);

  useEffect(() => {
    let cancelled = false;
    void fetchProviderModelCatalog().then((entries) => {
      if (!cancelled) setCatalog(entries);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // Vault snapshot for the node index and report outputs; refreshed when the
  // newest run changes state so a fresh report appears without a reload.
  const completionKey = `${newestRun?.id ?? ""}:${newestRun?.status ?? ""}`;
  useEffect(() => {
    let cancelled = false;
    fetchPages(scope)
      .then((result) => {
        if (!cancelled) setVault(result);
      })
      .catch(() => {
        if (!cancelled) setVault({ pages: [], outputs: [] });
      });
    return () => {
      cancelled = true;
    };
  }, [scope, completionKey]);

  const nodeIndex = useMemo(() => buildNodeIndex(vault?.pages ?? []), [vault]);
  const reports = useMemo(
    () =>
      (vault?.outputs ?? [])
        .filter((output) => RUN_REPORT_RE.test(output.path))
        .slice()
        .sort(
          (a, b) => (Date.parse(b.modified ?? "") || 0) - (Date.parse(a.modified ?? "") || 0),
        ),
    [vault],
  );

  // ── Resilient monitoring (§5.2 / 5.2.4) ─────────────────────────
  const liveSignature = liveRun ? progressSignature(liveRun) : null;
  const liveSignatureRef = useRef<string | null>(null);
  // Poll ticks observed with an unchanged signature — timestamps live in the
  // interval callback's world, not render's (react-hooks/purity).
  const progressRef = useRef<{ signature: string | null; ticks: number }>({
    signature: null,
    ticks: 0,
  });

  useEffect(() => {
    liveSignatureRef.current = liveSignature;
  }, [liveSignature]);

  const hasLive = liveRun !== null;
  useEffect(() => {
    // Poll while a run is live or the event socket is down — the WS refetch
    // path in usePipelineExecutions is an accelerator, never the only source.
    if (!hasLive && wsConnected) return;
    const intervalId = window.setInterval(() => {
      void refetch();
      const signature = liveSignatureRef.current;
      const progress = progressRef.current;
      if (signature === null || progress.signature !== signature) {
        progressRef.current = { signature, ticks: 0 };
        return;
      }
      progress.ticks += 1;
      if (progress.ticks * POLL_INTERVAL_MS >= STALL_AFTER_MS) {
        setStalledSignature(signature);
      }
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(intervalId);
  }, [hasLive, refetch, wsConnected]);

  const stalled = liveSignature !== null && stalledSignature === liveSignature;
  const recovery = stalled
    ? { key: `stall:${liveSignature}`, kind: "stalled" as const }
    : orphanedRun
      ? { key: `orphan:${orphanedRun.id}`, kind: "orphaned" as const }
      : null;
  const recoveryVisible = recovery !== null && recovery.key !== dismissedRecovery;

  // ── Derived composer state ──────────────────────────────────────
  const composerDisabled = offline || hasLive || launching;
  const providerOptions = useMemo(() => {
    const discovered = getOrderedProviders(catalog.map((entry) => entry.provider));
    return discovered.includes("claude") ? discovered : ["claude", ...discovered];
  }, [catalog]);
  const modelOptions = useMemo(
    () => getModelsForProvider(catalog, provider),
    [catalog, provider],
  );

  const topicSlugOfCompleted = completedRun ? executionTopicSlug(completedRun) : null;
  const resolvedTopicPath = topicSlugOfCompleted
    ? resolveWikilinkTarget(nodeIndex, `knowledge/topics/${topicSlugOfCompleted}`)
    : null;

  const pastRows = useMemo(() => {
    const rows: PastRow[] = [];
    for (const run of researchRuns) {
      if (LIVE_STATUSES.has(run.status)) continue;
      rows.push({
        kind: "execution",
        key: `exec:${run.id}`,
        ts: Date.parse(run.created_at) || 0,
        run,
      });
    }
    for (const report of reports) {
      rows.push({
        kind: "report",
        key: `report:${report.path}`,
        ts: Date.parse(report.modified ?? "") || 0,
        report,
      });
    }
    return rows.sort((a, b) => b.ts - a.ts);
  }, [researchRuns, reports]);

  const handleLaunch = async () => {
    const trimmed = question.trim();
    if (!trimmed || composerDisabled) return;
    setLaunching(true);
    try {
      const launch = await actions.launchResearchRun({
        question: trimmed,
        topic_slug: topicSlug.trim(),
        max_sources: parsePositiveInt(maxSources, 12),
        max_items: parsePositiveInt(maxItems, 8),
        create_tasks: createTasks ? "true" : "false",
        provider,
        model,
      });
      if (launch) {
        setQuestion("");
        await refetch();
      }
    } finally {
      setLaunching(false);
    }
  };

  const openPipelinesTab = () => {
    showActivityTab("pipelines");
  };

  // ── Reader takeover for run reports ─────────────────────────────
  if (readingPath) {
    return (
      <div className="flex min-h-0 flex-1 flex-col">
        <div className="flex items-center gap-2 border-b border-border px-3 py-1.5">
          <button
            type="button"
            onClick={() => setReadingPath(null)}
            className={`${ghostButton} flex items-center gap-1`}
          >
            <ChevronLeftIcon />
            Back to research
          </button>
        </div>
        <WikiPageReader
          scope={scope}
          path={readingPath}
          nav={nav}
          nodeIndex={nodeIndex}
          graph={null}
          onOpenGraph={onOpenGraph}
        />
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-4 py-4">
      {/* ── Composer card ── */}
      <div className="flex max-w-[65ch] flex-col gap-2 rounded-md border border-border p-3">
        <textarea
          aria-label="Research question"
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          disabled={composerDisabled}
          rows={3}
          placeholder="What should the researcher investigate?"
          className="resize-none rounded-md border border-border bg-transparent px-2 py-1.5 text-sm text-foreground placeholder:text-muted-foreground disabled:opacity-60"
        />

        {offline ? (
          <p role="status" className="text-xs text-muted-foreground">
            The wiki gateway is unreachable — the composer is disabled until it recovers.
          </p>
        ) : hasLive ? (
          <p role="status" className="text-xs text-muted-foreground">
            A research run is in progress
          </p>
        ) : null}

        {optionsOpen ? (
          <div className="flex flex-col gap-2 rounded-md bg-muted/40 p-2">
            <label className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
              Topic slug
              <input
                aria-label="Topic slug"
                value={topicSlug}
                onChange={(event) => setTopicSlug(event.target.value)}
                disabled={composerDisabled}
                placeholder="derive from question"
                className={`${fieldInput} w-48`}
              />
            </label>
            <label className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
              Max sources
              <input
                aria-label="Max sources"
                type="number"
                min={1}
                value={maxSources}
                onChange={(event) => setMaxSources(event.target.value)}
                disabled={composerDisabled}
                className={`${fieldInput} w-20`}
              />
            </label>
            <label className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
              Max items
              <input
                aria-label="Max items"
                type="number"
                min={1}
                value={maxItems}
                onChange={(event) => setMaxItems(event.target.value)}
                disabled={composerDisabled}
                className={`${fieldInput} w-20`}
              />
            </label>
            <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
              <span>Create follow-up tasks</span>
              <Switch
                aria-label="Create follow-up tasks"
                checked={createTasks}
                onChange={setCreateTasks}
                disabled={composerDisabled}
              />
            </div>
            <label className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
              Provider
              <select
                aria-label="Provider"
                value={provider}
                onChange={(event) => {
                  setProvider(event.target.value);
                  setModel("");
                }}
                disabled={composerDisabled}
                className={`${fieldInput} w-40`}
              >
                {providerOptions.map((option) => (
                  <option key={option} value={option}>
                    {option}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
              Model
              <select
                aria-label="Model"
                value={model}
                onChange={(event) => setModel(event.target.value)}
                disabled={composerDisabled}
                className={`${fieldInput} w-40`}
              >
                <option value="">(agent default)</option>
                {modelOptions.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
          </div>
        ) : null}

        <div className="flex items-center justify-between gap-2">
          <button
            type="button"
            onClick={() => setOptionsOpen((open) => !open)}
            aria-expanded={optionsOpen}
            className={ghostButton}
          >
            Options
          </button>
          <button
            type="button"
            onClick={() => void handleLaunch()}
            disabled={composerDisabled || !question.trim()}
            className={accentButton}
          >
            Run research
          </button>
        </div>
      </div>

      {/* ── Live run / completion / recovery zone ── */}
      {liveRun ? (
        <section
          aria-label="Live research run"
          className="flex max-w-[65ch] flex-col gap-2 rounded-md border border-border p-3"
        >
          <div className="flex items-center gap-2 text-xs">
            <ActivityRowStatusDot
              kind={getExecStatusKind(liveRun.status)}
              pulse={liveRun.status === "running"}
              label={liveRun.status}
            />
            <span className="font-medium text-foreground">{liveRun.status}</span>
            <span className="text-muted-foreground">
              Elapsed {formatDuration(now - (Date.parse(liveRun.created_at) || now))}
            </span>
            <span className="flex-1" />
            <button type="button" onClick={openPipelinesTab} className={ghostButton}>
              View in Pipelines tab
            </button>
          </div>
          <ul aria-label="Run steps" className="flex flex-col gap-1">
            {STEP_LABELS.map(([stepId, label]) => {
              const status =
                liveRun.steps.find((step) => step.step_id === stepId)?.status ?? "pending";
              return (
                <li key={stepId} className="flex items-center gap-2 text-xs">
                  <ActivityRowStatusDot
                    kind={getExecStatusKind(status)}
                    pulse={status === "running"}
                    label={status}
                  />
                  <span className="text-foreground">{label}</span>
                  <span className="text-muted-foreground">{status}</span>
                </li>
              );
            })}
          </ul>
        </section>
      ) : null}

      {completedRun ? (
        <section
          role="status"
          aria-label="Research run completed"
          className="flex max-w-[65ch] flex-wrap items-center gap-2 rounded-md border border-[var(--color-success-foreground)]/40 bg-[var(--accent-tint)] px-3 py-2 text-xs"
        >
          <span className="font-medium text-[var(--color-success-foreground)]">
            Research run completed
          </span>
          <span className="flex-1" />
          {reports.length > 0 ? (
            <button
              type="button"
              onClick={() => setReadingPath(reports[0]?.path ?? null)}
              className={ghostButton}
            >
              Open report
            </button>
          ) : null}
          {resolvedTopicPath ? (
            <button
              type="button"
              onClick={() => void nav.openPage(resolvedTopicPath)}
              className={ghostButton}
            >
              Open topic page
            </button>
          ) : null}
        </section>
      ) : null}

      {recoveryVisible && recovery ? (
        <section
          role="status"
          aria-label="Research run recovery"
          className="flex max-w-[65ch] flex-col gap-1 rounded-md border border-[var(--color-warning-foreground)]/40 bg-[var(--color-warning-soft)] px-3 py-2"
        >
          <div className="flex items-center gap-2 text-xs font-medium text-[var(--color-warning-foreground)]">
            <WarningIcon />
            {recovery.kind === "stalled"
              ? "Run may be stalled"
              : "Interrupted by daemon restart"}
          </div>
          <p className="text-xs text-muted-foreground">
            {recovery.kind === "stalled"
              ? "The run has reported no progress for over a minute. It may still be working, or the daemon may have lost it."
              : "The daemon restarted while this run was in progress; the startup sweep marked it failed."}
          </p>
          <div className="flex items-center gap-2">
            <button type="button" onClick={() => void refetch()} className={ghostButton}>
              Refresh
            </button>
            <button
              type="button"
              onClick={() => setDismissedRecovery(recovery.key)}
              className={ghostButton}
            >
              Dismiss
            </button>
          </div>
        </section>
      ) : null}

      {/* ── Past runs ── */}
      {pastRows.length > 0 ? (
        <div className="flex max-w-[65ch] flex-col gap-1">
          <h3 className="text-xs font-medium text-muted-foreground">Past runs</h3>
          <ul aria-label="Past research runs" className="flex flex-col">
            {pastRows.map((row) =>
              row.kind === "report" ? (
                <li key={row.key} className="border-b border-border/60 py-1.5 last:border-b-0">
                  <button
                    type="button"
                    onClick={() => setReadingPath(row.report.path)}
                    className="flex w-full items-center gap-2 text-left text-xs hover:text-foreground"
                  >
                    <span className="rounded border border-border px-1 text-2xs text-muted-foreground">
                      Report
                    </span>
                    <span className="min-w-0 flex-1 truncate text-foreground">
                      {reportName(row.report.path)}
                    </span>
                    {row.report.modified ? (
                      <span className="text-muted-foreground">
                        {formatRelativeTime(row.report.modified)}
                      </span>
                    ) : null}
                  </button>
                </li>
              ) : (
                <li
                  key={row.key}
                  className="flex items-center gap-2 border-b border-border/60 py-1.5 text-xs last:border-b-0"
                >
                  <ActivityRowStatusDot
                    kind={getExecStatusKind(row.run.status)}
                    label={row.run.status}
                  />
                  <span className="min-w-0 flex-1 truncate text-foreground">
                    {executionQuestion(row.run)}
                  </span>
                  <span className="text-muted-foreground">{row.run.status}</span>
                  {executionDuration(row.run) ? (
                    <span className="text-muted-foreground">{executionDuration(row.run)}</span>
                  ) : null}
                </li>
              ),
            )}
          </ul>
        </div>
      ) : null}
    </div>
  );
}
