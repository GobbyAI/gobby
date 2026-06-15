import { useMemo, useState } from "react";

import { useConfirmDialog } from "../../../hooks/useConfirmDialog";
import { DetailPaneHeader } from "../fields";
import { ActivityPanelEmpty, TasksEmptyIcon } from "../ActivityPanelEmpty";
import { installHubSkill, scanHubSkill } from "./SkillsTabActions";
import type { ActivitySkill, SkillHubResult, SkillScanFinding, SkillScanResult } from "./SkillsTabData";

interface SkillsHubDetailProps {
  result: SkillHubResult | null;
  projectId?: string | null;
  onInstalled: (skill: ActivitySkill) => void;
  onError: (message: string | null) => void;
}

type SeverityKey = "critical" | "high" | "medium" | "low" | "info" | "unknown";

const SEVERITY_ORDER: Record<SeverityKey, number> = {
  critical: 5,
  high: 4,
  medium: 3,
  low: 2,
  info: 1,
  unknown: 0,
};

const SEVERITY_STYLES: Record<SeverityKey, string> = {
  critical: "bg-error-soft text-error",
  high: "bg-error-soft text-error",
  medium: "bg-[var(--color-warning-soft)] text-[var(--color-warning-foreground)]",
  low: "bg-info-soft text-info",
  info: "bg-muted text-muted-foreground",
  unknown: "bg-muted text-muted-foreground",
};

function severityKey(value: string | null | undefined): SeverityKey {
  const normalized = value?.toLowerCase();
  if (
    normalized === "critical" ||
    normalized === "high" ||
    normalized === "medium" ||
    normalized === "low" ||
    normalized === "info"
  ) {
    return normalized;
  }
  return "unknown";
}

function severityLabel(value: string | null | undefined): string {
  return severityKey(value).toUpperCase();
}

function SeverityIcon({ severity }: { severity: SeverityKey }) {
  if (severity === "critical" || severity === "high") {
    return (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
        <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z" />
        <path d="M12 9v4" />
        <path d="M12 17h.01" />
      </svg>
    );
  }

  if (severity === "medium") {
    return (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
        <path d="m12 3 9 9-9 9-9-9 9-9Z" />
        <path d="M12 8v5" />
        <path d="M12 16h.01" />
      </svg>
    );
  }

  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" aria-hidden="true">
      <circle cx="12" cy="12" r="9" />
      <path d="M12 11v5" />
      <path d="M12 8h.01" />
    </svg>
  );
}

function findingSortValue(finding: SkillScanFinding): number {
  return SEVERITY_ORDER[severityKey(finding.severity)];
}

export function SkillsHubDetail({ result, projectId, onInstalled, onError }: SkillsHubDetailProps) {
  const { confirm, ConfirmDialogElement } = useConfirmDialog();
  const [scanResult, setScanResult] = useState<SkillScanResult | null>(null);
  const [scanning, setScanning] = useState(false);
  const [installing, setInstalling] = useState(false);

  const content = result?.content?.trim() ?? "";
  const sortedFindings = useMemo(
    () => [...(scanResult?.findings ?? [])].sort((a, b) => findingSortValue(b) - findingSortValue(a)),
    [scanResult],
  );

  if (!result) {
    return (
      <ActivityPanelEmpty
        icon={<TasksEmptyIcon />}
        heading="Skill Hub"
        body="Search a hub skill, then inspect scan results before installing."
      />
    );
  }

  const title = result.display_name || result.slug;
  const canScan = Boolean(content) && !scanning && !installing;
  const canAttemptInstall = Boolean(content && scanResult) && !scanning && !installing;
  const installReason = !content
    ? "Hub result does not include content to scan."
    : !scanResult
      ? "Run a safety scan before installing."
      : scanResult.is_safe
        ? "Safety scan passed."
        : "Findings require confirmation before install.";
  const scanStatusLabel = scanResult
    ? scanResult.is_safe
      ? "SAFE"
      : severityLabel(scanResult.max_severity)
    : null;

  async function handleScan() {
    if (!content || !result) return;
    setScanning(true);
    onError(null);
    try {
      setScanResult(await scanHubSkill(content, title));
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error));
    } finally {
      setScanning(false);
    }
  }

  async function handleInstall() {
    if (!result || !scanResult || !content) return;
    if (!scanResult.is_safe) {
      const maxSeverity = severityLabel(scanResult.max_severity);
      const confirmed = await confirm({
        title: `Install despite ${maxSeverity} findings?`,
        description: "Review the scan findings before installing this hub skill into your local skill library.",
        confirmLabel: "Install anyway",
        destructive: true,
      });
      if (!confirmed) return;
    }

    setInstalling(true);
    onError(null);
    try {
      const installed = await installHubSkill({
        hubName: result.hub_name,
        slug: result.slug,
        version: result.version,
        projectId,
      });
      if (!installed) throw new Error(`Failed to install ${title}`);
      onInstalled(installed);
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error));
    } finally {
      setInstalling(false);
    }
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {ConfirmDialogElement}
      <DetailPaneHeader
        title={title}
        dirty={false}
        onSave={() => undefined}
        onDiscard={() => undefined}
        actions={
          <>
            {scanStatusLabel && (
              <span
                className={`inline-flex min-h-8 items-center gap-1.5 rounded-md px-2 text-xs font-semibold ${SEVERITY_STYLES[severityKey(scanResult?.max_severity)]}`}
              >
                <SeverityIcon severity={severityKey(scanResult?.max_severity)} />
                {scanStatusLabel}
              </span>
            )}
            <button
              type="button"
              aria-label="Scan hub skill"
              className="btn btn-secondary btn-sm"
              disabled={!canScan}
              onClick={() => void handleScan()}
            >
              {scanning ? "Scanning..." : "Scan"}
            </button>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={!canAttemptInstall}
              aria-label="Install hub skill"
              title={installReason}
              onClick={() => void handleInstall()}
            >
              {installing ? "Installing..." : "Install"}
            </button>
          </>
        }
      />

      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        <div className="grid gap-3 [grid-template-columns:repeat(auto-fit,minmax(min(24rem,100%),1fr))]">
          <section className="flex min-w-0 flex-col gap-3">
            <div className="grid gap-2 rounded-md border border-border bg-[var(--bg-secondary)] p-3">
              <div className="grid grid-cols-[6rem_minmax(0,1fr)] gap-2 text-sm">
                <span className="text-muted-foreground">Name</span>
                <span className="min-w-0 truncate text-foreground">{title}</span>
                <span className="text-muted-foreground">Hub</span>
                <span className="min-w-0 truncate text-foreground">{result.hub_name}</span>
                <span className="text-muted-foreground">Version</span>
                <span className="text-foreground">{result.version ? `v${result.version}` : "Unknown"}</span>
                <span className="text-muted-foreground">License</span>
                <span className="text-foreground">{result.license || "Unknown"}</span>
              </div>
              <div className="border-t border-border pt-2">
                <div className="mb-1 text-xs font-medium text-muted-foreground">Description</div>
                <p className="m-0 text-sm leading-relaxed text-foreground">{result.description || "No description"}</p>
              </div>
            </div>

            <div className="rounded-md border border-border bg-[var(--bg-secondary)] p-3">
              <div className="mb-2 flex items-center justify-between gap-2">
                <span className="text-sm font-medium text-foreground">Safety scan</span>
                <span className="text-xs text-muted-foreground">{installReason}</span>
              </div>
              {!scanResult ? (
                <p className="m-0 text-sm text-muted-foreground">
                  Scan the candidate content before installing from a hub.
                </p>
              ) : sortedFindings.length === 0 ? (
                <p className="m-0 text-sm text-[var(--color-success-foreground)]">
                  No findings were reported by the safety scan.
                </p>
              ) : (
                <div className="flex flex-col gap-2">
                  {sortedFindings.map((finding, index) => {
                    const severity = severityKey(finding.severity);
                    return (
                      <article
                        key={`${finding.title}-${finding.location}-${index}`}
                        className="rounded-md border border-border bg-[var(--bg-primary)] p-3"
                      >
                        <div className="mb-2 flex items-start justify-between gap-3">
                          <div className="min-w-0">
                            <div className="text-sm font-medium text-foreground">{finding.title}</div>
                            <div className="text-xs text-muted-foreground">{finding.location}</div>
                          </div>
                          <span
                            className={`inline-flex shrink-0 items-center gap-1.5 rounded-md px-2 py-1 text-xs font-semibold ${SEVERITY_STYLES[severity]}`}
                          >
                            <SeverityIcon severity={severity} />
                            {severityLabel(finding.severity)}
                          </span>
                        </div>
                        <p className="m-0 text-sm leading-relaxed text-foreground">{finding.description}</p>
                        <p className="mt-2 mb-0 text-sm leading-relaxed text-muted-foreground">
                          <span className="font-medium text-foreground">Remediation:</span>{" "}
                          {finding.remediation || "Review the finding before installing."}
                        </p>
                      </article>
                    );
                  })}
                </div>
              )}
            </div>
          </section>

          <section className="flex min-h-0 min-w-0 flex-col gap-2">
            <div className="flex items-center justify-between gap-2">
              <span className="text-sm font-medium text-foreground">Content preview</span>
              <span className="truncate text-xs text-muted-foreground">{result.slug}.md</span>
            </div>
            <pre
              aria-label="Hub skill content preview"
              className="m-0 min-h-64 flex-1 overflow-auto whitespace-pre-wrap rounded-md border border-border bg-[var(--bg-secondary)] p-3 font-mono text-xs leading-relaxed text-foreground"
            >
              {content || "This hub result did not include preview content."}
            </pre>
          </section>
        </div>
      </div>
    </div>
  );
}
