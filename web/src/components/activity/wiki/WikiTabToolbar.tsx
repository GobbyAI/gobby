/**
 * Toolbar for the wiki activity tab (plan wiki-obsidian-panel §2.2): mode
 * segmented control, search, graph button, and the kebab action menu. Also
 * exports the slim degraded-state banner rendered directly under the
 * toolbar.
 */

import { cn } from "../../../lib/utils";
import { Button } from "../../ui/Button";
import { coarseHitAreaCls } from "../../ui/controlStyles";
import { SegmentedControl, type SegmentedControlOption } from "../../ui/SegmentedControl";
import { ActivityPanelSearch } from "../ActivityPanelSearch";
import { QuickMenu, type QuickMenuItem } from "../QuickMenu";
import type { WikiStatusSummary } from "./WikiTabData";
import type { WikiMode } from "./WikiTabModel";

const MODE_OPTIONS: readonly SegmentedControlOption<WikiMode>[] = [
  { value: "wiki", label: "Wiki" },
  { value: "code", label: "Code" },
];

const SEARCH_PLACEHOLDER: Record<WikiMode, string> = {
  wiki: "Filter pages",
  code: "Filter code pages",
};

export interface WikiToolbarActions {
  onRefreshIndex: () => void;
  onCompile: () => void;
  onAudit: () => void;
  onAttachFile: () => void;
  onIngestUrl: () => void;
  onManageSources: () => void;
  onTopicScope: () => void;
}

interface WikiTabToolbarProps {
  mode: WikiMode;
  onModeChange: (mode: WikiMode) => void;
  search: string;
  onSearchChange: (value: string) => void;
  /** Container ≥560px — narrow layouts move search to its own row. */
  wide: boolean;
  onOpenGraph: () => void;
  /** Gateway down: index/compile/audit/attach/ingest can't reach gwiki. */
  actionsDisabled: boolean;
  actions: WikiToolbarActions;
}

function GraphIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <circle cx="5" cy="6" r="2.2" />
      <circle cx="19" cy="6" r="2.2" />
      <circle cx="12" cy="18" r="2.2" />
      <path d="M6.8 7.5 10.6 16M17.2 7.5 13.4 16M7.2 6h9.6" />
    </svg>
  );
}

const noop = () => {};

export function WikiTabToolbar({
  mode,
  onModeChange,
  search,
  onSearchChange,
  wide,
  onOpenGraph,
  actionsDisabled,
  actions,
}: WikiTabToolbarProps) {
  const menuItems: QuickMenuItem[] = [
    // Quick-open and page creation land with the browse milestone (§3.1/§3.2).
    { label: "New page", disabled: true, onSelect: noop },
    { label: "Quick open", disabled: true, onSelect: noop },
    { type: "separator" },
    { label: "Refresh index", disabled: actionsDisabled, onSelect: actions.onRefreshIndex },
    { label: "Compile", disabled: actionsDisabled, onSelect: actions.onCompile },
    { label: "Audit", disabled: actionsDisabled, onSelect: actions.onAudit },
    { type: "separator" },
    { label: "Attach file…", disabled: actionsDisabled, onSelect: actions.onAttachFile },
    { label: "Ingest URL…", disabled: actionsDisabled, onSelect: actions.onIngestUrl },
    { type: "separator" },
    { label: "Manage sources…", onSelect: actions.onManageSources },
    { label: "Topic scope…", onSelect: actions.onTopicScope },
  ];

  const searchInput = (
    <ActivityPanelSearch
      value={search}
      onChange={onSearchChange}
      placeholder={SEARCH_PLACEHOLDER[mode]}
      ariaLabel="Filter wiki"
    />
  );

  return (
    <div className="flex flex-col gap-2 border-b border-border px-3 py-2">
      <div className="flex items-center gap-2">
        <SegmentedControl
          value={mode}
          onChange={onModeChange}
          options={MODE_OPTIONS}
          ariaLabel="Wiki mode"
          controlHeight="sm"
        />
        {wide ? <div className="min-w-0 flex-1">{searchInput}</div> : null}
        <div className={cn("flex items-center gap-1", !wide && "ml-auto")}>
          <Button
            type="button"
            onClick={onOpenGraph}
            aria-label="Open graph"
            title="Open graph"
            variant="secondary"
            size="sm"
            className={coarseHitAreaCls}
          >
            <GraphIcon />
            Graph
          </Button>
          <QuickMenu items={menuItems} menuLabel="Wiki actions" triggerLabel="Wiki actions" />
        </div>
      </div>
      {/* Row wrapper: the shared input is flex:1 1 9rem, which grows
          vertically if dropped straight into this column container. */}
      {!wide ? <div className="flex">{searchInput}</div> : null}
    </div>
  );
}

function InfoIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <circle cx="12" cy="12" r="9" />
      <path d="M12 8h.01M12 11v5" />
    </svg>
  );
}

/**
 * One slim state line under the toolbar. Info tokens + icon (state is never
 * hue-only per .impeccable.md); browse keeps working while degraded, so the
 * banner informs rather than blocks.
 */
export function WikiDegradedBanner({ summary }: { summary: WikiStatusSummary }) {
  // "loading" is the first fetch in flight — not an outage; stay quiet.
  if (summary.state === "ready" || summary.state === "loading") return null;
  const offline = summary.state === "unavailable";
  const label = offline
    ? `Wiki gateway offline${summary.message ? ` — ${summary.message}` : ""}`
    : `Wiki degraded: ${summary.degradedServices.join(", ")}`;
  const detail = offline
    ? "Browse is unavailable until the gateway recovers."
    : `${summary.brokenLinks} broken links · ${summary.stalePages} stale pages · ${summary.uncompiledSources} uncompiled sources`;
  return (
    <div role="status" className="flex items-start gap-2 bg-info-soft px-3 py-1.5 text-xs text-info">
      <span className="mt-px shrink-0" aria-hidden="true">
        <InfoIcon />
      </span>
      <span className="min-w-0">
        {label}
        <span className="ml-2 text-muted-foreground" title={detail}>
          {detail}
        </span>
      </span>
    </div>
  );
}
