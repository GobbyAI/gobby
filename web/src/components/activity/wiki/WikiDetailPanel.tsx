import type { WikiSourceRecord } from "../../../hooks/useWiki";
import { DetailPaneHeader } from "../fields";
import {
  sourceLabel,
  sourceLinks,
  sourcePath,
  type WikiFinding,
  type WikiLink,
  type WikiMetric,
  type WikiSummary,
} from "./WikiTabData";

interface WikiDetailPanelProps {
  source: WikiSourceRecord | null;
  summary: WikiSummary;
}

function MetricGrid({ metrics }: { metrics: WikiMetric[] }) {
  return (
    <div className="grid grid-cols-1 gap-2 min-[520px]:grid-cols-3">
      {metrics.map((metric) => (
        <div
          key={metric.label}
          className="rounded-md border border-border bg-[var(--bg-primary)] p-3"
        >
          <div className="text-xs font-medium uppercase text-muted-foreground">
            {metric.label}
          </div>
          <div className="mt-1 break-words text-sm text-foreground">{metric.value}</div>
        </div>
      ))}
    </div>
  );
}

function TextList({ items }: { items: string[] }) {
  if (!items.length) {
    return <div className="text-sm text-muted-foreground">None</div>;
  }
  return (
    <ul className="space-y-1">
      {items.map((item, index) => (
        <li
          key={`${item}-${index}`}
          className="break-all rounded-md bg-[var(--bg-primary)] px-2 py-1 text-sm text-foreground"
        >
          {item}
        </li>
      ))}
    </ul>
  );
}

function LinksList({ links }: { links: WikiLink[] }) {
  if (!links.length) {
    return <div className="text-sm text-muted-foreground">None</div>;
  }
  return (
    <ul className="space-y-1">
      {links.map((link) => (
        <li key={`${link.href}-${link.label}`}>
          <a className="text-sm text-accent hover:underline" href={link.href}>
            {link.label}
          </a>
        </li>
      ))}
    </ul>
  );
}

function FindingsList({ findings }: { findings: WikiFinding[] }) {
  if (!findings.length) {
    return <div className="text-sm text-muted-foreground">None</div>;
  }
  return (
    <ul className="space-y-1">
      {findings.map((finding, index) => (
        <li
          key={`${finding.label}-${index}`}
          className="rounded-md bg-[var(--bg-primary)] px-2 py-1 text-sm text-foreground"
        >
          <span>{finding.label}</span>
          {finding.path && (
            <span className="ml-2 break-all text-muted-foreground">{finding.path}</span>
          )}
        </li>
      ))}
    </ul>
  );
}

function DetailSection({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="space-y-2">
      <h4 className="text-sm font-medium text-foreground">{title}</h4>
      {children}
    </section>
  );
}

export function WikiDetailPanel({ source, summary }: WikiDetailPanelProps) {
  const title = source ? sourceLabel(source) : "Wiki status";
  const sourceLinksForDetail = source ? sourceLinks(source) : [];

  return (
    <div className="flex h-full min-h-0 flex-col bg-[var(--bg-secondary)]">
      <DetailPaneHeader title={title} dirty={false} onSave={() => {}} onDiscard={() => {}} />
      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        <h3 className="sr-only">{title}</h3>
        {source && (
          <div className="mb-3 rounded-md border border-border bg-[var(--bg-primary)] p-3">
            <div className="text-xs font-medium uppercase text-muted-foreground">
              Selected source
            </div>
            <div className="mt-2 space-y-1 text-sm">
              <div className="break-all text-foreground">
                {sourcePath(source) || source.id}
              </div>
              {source.wiki_path && (
                <div className="break-all text-muted-foreground">{source.wiki_path}</div>
              )}
              <LinksList links={sourceLinksForDetail} />
            </div>
          </div>
        )}

        <div className="space-y-4">
          <MetricGrid metrics={summary.metrics} />
          <DetailSection title="Degraded Services">
            <TextList items={summary.degradedServices} />
          </DetailSection>
          <DetailSection title="Health Findings">
            <FindingsList findings={summary.findings} />
          </DetailSection>
          <DetailSection title="Recent Searches">
            <TextList items={summary.searches} />
          </DetailSection>
          <DetailSection title="Indexed Paths">
            <TextList items={summary.indexedPaths} />
          </DetailSection>
          <DetailSection title="Wiki Page Links">
            <LinksList links={summary.links} />
          </DetailSection>
        </div>
      </div>
    </div>
  );
}
