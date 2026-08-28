import {
  useState,
  useEffect,
  useRef,
  Suspense,
  lazy,
  type CSSProperties,
} from "react";

import { cn } from "../../lib/utils";
import { ResizeHandle } from "../shared/ResizeHandle";
import { Button } from "../ui/Button";
import { coarseHitAreaCls } from "../ui/controlStyles";
import { DropdownCaret } from "../ui/DropdownCaret";
import { PlansTab } from "./PlansTab";
import { FileChangesTab } from "./FileChangesTab";
import { SessionsTab } from "./SessionsTab";
import { PipelinesTab } from "./PipelinesTab";
import { TasksTab } from "./TasksTab";
import { FilesTab } from "./FilesTab";
import { CronTab } from "./CronTab";
import { TracesTab } from "./TracesTab";
import { ActivityMcpTab, type ActivityMcpTabProps } from "./ActivityMcpTab";
import { AgentsTab } from "./AgentsTab";
import { StagesTab } from "./StagesTab";
import { SkillsTab } from "./SkillsTab";
import { MemoryTab } from "./MemoryTab";
import { IntegrationsTab } from "./IntegrationsTab";
import { WikiTab } from "./WikiTab";
import { RulesTab } from "./RulesTab";
import { DirtyGuardProvider } from "./DirtyGuardContext";
import {
  useDirtyGuardController,
  type DirtyGuardContextValue,
} from "./dirtyGuard";
import type { Plan } from "../../types/plans";
import type { ApprovalOption } from "../../types/chat";
import type { GobbySession } from "../../types/sessions";
import type { PlanPendingVariant } from "../chat/planPendingSurface";
import type { SessionsFilters } from "./sessionsFilters";
import type { LayoutMode } from "./useActivityPanel";
import { PanelIcon } from "../chat/icons/PanelIcon";
import { Heading } from "../shared/Heading";
import {
  ACTIVITY_PANEL_DROPDOWN_TABS,
  ACTIVITY_PANEL_TABS,
  type ActivityTab,
} from "./ActivityPanelTabs";
import {
  ActivityActionButtons,
  ActivityActionsProvider,
} from "./ActivityActionsContext";

const noopFetchDiff = async (): Promise<string> => "";

// wterm and its renderer stay a lazy asset — only loaded when the terminal
// tab is actually opened.
const TerminalTab = lazy(() =>
  import("./terminal/TerminalTab").then((module) => ({
    default: module.TerminalTab,
  })),
);

// Width constants shared between the inline style, ResizeHandle, and the
// `useActivityPanel` localStorage validator. CHAT_MIN_WIDTH mirrors the
// `min-w-[320px]` on the chat column in ChatPage.tsx so the maxWidth calc
// always leaves enough room for the chat to keep its floor.
const PANEL_MIN_WIDTH = 320;
const CHAT_MIN_WIDTH = 320;
// LAYOUT_BUFFER used to be 24 (cushion before chat hit its hard min-width).
// Dropped to 0 so dragging the activity panel can compress chat to exactly
// CHAT_MIN_WIDTH, matching the explicit 320 ask.
const LAYOUT_BUFFER = 0;

function activityPanelClassName(className?: string) {
  return cn(
    "activity-panel @container/activity-panel flex h-full flex-col overflow-hidden border-l border-border bg-[var(--bg-primary)] [--activity-panel-control-height:2.25rem]",
    "@max-[380px]/activity-panel:[&_.activity-panel-tab-label]:hidden",
    // Literal underscores in targeted class names must stay escaped (\_) and
    // the literals must be String.raw: Tailwind turns bare `_` into a space
    // inside arbitrary variants, silently producing dead selectors, and a
    // cooked string would strip the escape before it reaches the DOM.
    // Mobile toolbar pattern (.impeccable.md): labeled toolbar buttons
    // collapse to icon-only at the mobile tier, and also when the panel is
    // resized narrow on desktop. This root rule is the single collapse
    // authority — label spans carry only the marker class, never their own
    // collapse CSS.
    String.raw`@max-[479px]/activity-panel:[&_.activity-panel-action-btn\_\_label]:hidden`,
    String.raw`mobile:[&_.activity-panel-action-btn\_\_label]:hidden`,
    String.raw`@max-[360px]/activity-panel:[&_.activity-panel-status-bar\_\_watching-prefix]:hidden`,
    "[&_.activity-row-title]:min-w-0 [&_.activity-row-title]:flex-1 [&_.activity-row-title]:truncate",
    "[&_.activity-row-title]:text-[length:var(--text-base)] [&_.activity-row-title]:font-[var(--font-weight-medium)] [&_.activity-row-title]:text-[var(--text-primary)]",
    "[&_.activity-row-meta]:shrink-0 [&_.activity-row-meta]:text-[length:var(--text-sm)] [&_.activity-row-meta]:font-[var(--font-weight-normal)]",
    "[&_.activity-row-meta]:text-[var(--text-muted)] [&_.activity-row-meta]:tabular-nums",
    "[&_.activity-list-row]:flex [&_.activity-list-row]:min-h-[var(--activity-panel-row-height)] [&_.activity-list-row]:w-full",
    "[&_.activity-list-row]:items-center [&_.activity-list-row]:border-b [&_.activity-list-row]:border-border [&_.activity-list-row]:bg-transparent",
    "[&_.activity-list-row]:text-[var(--text-primary)] [&_.activity-list-row]:transition-colors [&_.activity-list-row:hover]:bg-[var(--bg-tertiary)]",
    "[&_.activity-list-row--selected]:bg-[color-mix(in_srgb,var(--accent)_8%,transparent)] [&_.activity-list-row--selected:hover]:bg-[color-mix(in_srgb,var(--accent)_8%,transparent)]",
    String.raw`[&_.activity-list-row\_\_body]:flex [&_.activity-list-row\_\_body]:min-w-0 [&_.activity-list-row\_\_body]:flex-[1_1_auto] [&_.activity-list-row\_\_body]:items-center`,
    String.raw`[&_.activity-list-row\_\_body]:appearance-none [&_.activity-list-row\_\_body]:justify-start [&_.activity-list-row\_\_body]:gap-2 [&_.activity-list-row\_\_body]:border-0 [&_.activity-list-row\_\_body]:bg-transparent`,
    String.raw`[&_.activity-list-row\_\_body]:px-3 [&_.activity-list-row\_\_body]:py-2 [&_.activity-list-row\_\_body]:text-left [&_.activity-list-row\_\_body]:font-[inherit] [&_.activity-list-row\_\_body]:text-[inherit]`,
    String.raw`[&_.activity-list-row\_\_body]:cursor-pointer [&_.activity-list-row\_\_body:focus-visible]:shadow-[inset_0_0_0_2px_var(--accent)] [&_.activity-list-row\_\_body:focus-visible]:outline-none`,
    "[&_.activity-panel-toolbar]:[--control-row-height-sm:var(--status-bar-control-height)]",
    "[&_.activity-panel-toolbar]:relative [&_.activity-panel-toolbar]:flex [&_.activity-panel-toolbar]:min-h-[var(--activity-panel-bar-height)]",
    "[&_.activity-panel-toolbar]:shrink-0 [&_.activity-panel-toolbar]:flex-nowrap [&_.activity-panel-toolbar]:items-center [&_.activity-panel-toolbar]:gap-1.5",
    "[&_.activity-panel-toolbar]:border-b [&_.activity-panel-toolbar]:border-border [&_.activity-panel-toolbar]:bg-[var(--bg-secondary)] [&_.activity-panel-toolbar]:px-3",
    "[&_.activity-panel-search]:min-h-[var(--control-row-height-sm)] [&_.activity-panel-search]:min-w-0 [&_.activity-panel-search]:flex-[1_1_9rem]",
    "[&_.activity-panel-search]:rounded-[0.45rem] [&_.activity-panel-search]:border [&_.activity-panel-search]:border-border [&_.activity-panel-search]:bg-[var(--bg-primary)]",
    "[&_.activity-panel-search]:px-[0.55rem] [&_.activity-panel-search]:font-[inherit] [&_.activity-panel-search]:text-[length:var(--text-base)] [&_.activity-panel-search]:font-[var(--font-weight-normal)]",
    "[&_.activity-panel-search]:text-[var(--text-primary)] [&_.activity-panel-search]:transition-colors [&_.activity-panel-search:focus]:border-accent",
    "[&_.activity-panel-search:focus-visible]:outline-2 [&_.activity-panel-search:focus-visible]:outline-offset-1 [&_.activity-panel-search:focus-visible]:outline-accent",
    "[&_.activity-panel-search::placeholder]:text-[var(--text-muted)]",
    "[&_.activity-panel-status-bar]:flex [&_.activity-panel-status-bar]:min-h-[var(--activity-panel-bar-height)] [&_.activity-panel-status-bar]:shrink-0",
    "[&_.activity-panel-status-bar]:items-center [&_.activity-panel-status-bar]:gap-3 [&_.activity-panel-status-bar]:border-b [&_.activity-panel-status-bar]:border-border",
    "[&_.activity-panel-status-bar]:bg-[var(--bg-secondary)] [&_.activity-panel-status-bar]:px-3 [&_.activity-panel-status-bar--detail]:justify-between",
    String.raw`[&_.activity-panel-status-bar\_\_title]:block [&_.activity-panel-status-bar\_\_title]:min-w-0 [&_.activity-panel-status-bar\_\_title]:truncate`,
    String.raw`[&_.activity-panel-status-bar\_\_title]:text-[length:var(--text-base)] [&_.activity-panel-status-bar\_\_title]:font-[var(--font-weight-medium)] [&_.activity-panel-status-bar\_\_title]:text-[var(--text-primary)]`,
    String.raw`[&_.activity-panel-status-bar\_\_actions]:flex [&_.activity-panel-status-bar\_\_actions]:flex-none [&_.activity-panel-status-bar\_\_actions]:items-center [&_.activity-panel-status-bar\_\_actions]:gap-3`,
    String.raw`[&_.activity-panel-toolbar>.activity-panel-toolbar\_\_end]:ml-auto [&_.activity-panel-toolbar>.activity-panel-toolbar\_\_end]:flex [&_.activity-panel-toolbar>.activity-panel-toolbar\_\_end]:items-center [&_.activity-panel-toolbar>.activity-panel-toolbar\_\_end]:gap-2`,
    String.raw`[&_.activity-filter-dropdown\_\_item]:inline-flex [&_.activity-filter-dropdown\_\_item]:w-full [&_.activity-filter-dropdown\_\_item]:items-center`,
    String.raw`[&_.activity-filter-dropdown\_\_item]:rounded-md [&_.activity-filter-dropdown\_\_item]:border-0 [&_.activity-filter-dropdown\_\_item]:bg-transparent`,
    String.raw`[&_.activity-filter-dropdown\_\_item]:px-[0.6rem] [&_.activity-filter-dropdown\_\_item]:py-[0.4rem] [&_.activity-filter-dropdown\_\_item]:text-left`,
    String.raw`[&_.activity-filter-dropdown\_\_item]:text-[length:var(--text-sm)] [&_.activity-filter-dropdown\_\_item]:text-[var(--text-secondary)] [&_.activity-filter-dropdown\_\_item]:transition-colors`,
    String.raw`[&_.activity-filter-dropdown\_\_item]:cursor-pointer [&_.activity-filter-dropdown\_\_item:hover]:bg-[var(--bg-tertiary)] [&_.activity-filter-dropdown\_\_item:hover]:text-[var(--text-primary)]`,
    String.raw`[&_.activity-filter-dropdown\_\_item--active]:bg-[color-mix(in_srgb,var(--accent)_15%,transparent)] [&_.activity-filter-dropdown\_\_item--active]:text-accent`,
    String.raw`[[data-theme=light]_&_.activity-filter-dropdown\_\_item--active]:bg-accent [[data-theme=light]_&_.activity-filter-dropdown\_\_item--active]:text-accent-foreground`,
    // Toolbar controls keep --status-bar-control-height on touch — the bar
    // supplies the 44px row; each control's invisible ::before hit-area
    // expansion (coarseHitAreaCls via SegmentedControl / Input / NativeSelect)
    // floors the tap target instead of inflating the rendered box.
    String.raw`pointer-coarse:[&_.activity-filter-dropdown\_\_item]:min-h-11`,
    className,
  );
}

type ActivityTabConfig = (typeof ACTIVITY_PANEL_TABS)[number];

interface ActivityPanelProps {
  // Effective layout mode (desktop value, or the mobile binary collapsed into
  // 'chat'|'panel'). The panel renders for 'split' and 'panel'; never 'chat'.
  mode: LayoutMode;
  // Toggles the chat pane: desktop split<->panel, mobile panel->chat (close).
  onToggleChat: () => void;
  panelWidth: number;
  onWidthChange: (width: number) => void;
  activeTab: ActivityTab;
  onTabChange: (tab: ActivityTab) => void;
  plans: Map<string, Plan>;
  activePlan: Plan | null;
  onOpenPlan: (id: string) => void;
  onSetPlanVersion: (id: string, index: number) => void;
  planPendingApproval?: boolean;
  planApproved?: boolean;
  planApprovalOptions?: ApprovalOption[];
  onApprovePlan?: (option?: ApprovalOption) => void;
  onRequestPlanChanges?: (feedback: string) => void;
  planPendingVariant?: PlanPendingVariant;
  // File changes tab
  changedFiles?: { path: string; status: string }[];
  fetchDiff?: (path: string) => Promise<string>;
  changesLoading?: boolean;
  changesError?: string | null;
  onRetryChanges?: () => void;
  // Tasks tab
  projectId?: string | null;
  projectName?: string | null;
  sessions?: GobbySession[];
  sessionsLoading?: boolean;
  sessionsFilters?: SessionsFilters;
  onSessionsFiltersChange?: (filters: SessionsFilters) => void;
  // Files tab
  onAddFileToChat?: (filePath: string) => void;
  // MCP tab
  mcp?: ActivityMcpTabProps;
  // Sessions tab
  onKillAgent?: (runId: string) => Promise<boolean | void> | boolean | void;
  onExpireSession?: (
    sessionId: string,
  ) => Promise<boolean | void> | boolean | void;
  onAcpCloseSession?: (
    sessionId: string,
  ) => Promise<boolean | void> | boolean | void;
  onAcpDeleteSession?: (
    sessionId: string,
  ) => Promise<boolean | void> | boolean | void;
  chatSessionId?: string | null;
  focusSessionId?: string | null;
  dirtyGuard?: DirtyGuardContextValue;
  onFocusSessionHandled?: () => void;
  onSwapSession?: (
    target: import("../../types/chat").SwappedSessionTarget,
  ) => void;
  onResumeSession?: (sessionId: string) => Promise<string> | string | void;
  // Terminal tab — separate focus channel from the Sessions tab routing.
  terminalFocusSessionId?: string | null;
  onTerminalFocusHandled?: () => void;
  isMobile?: boolean;
  requestPanelOverride?: () => void;
  releasePanelOverride?: () => void;
}

interface ActivityDropdownProps {
  tabs: ActivityTabConfig[];
  activeTab: ActivityTab;
  activeTabConfig: ActivityTabConfig;
  isOpen: boolean;
  onToggle: () => void;
  onSelect: (tab: ActivityTab) => void;
  wrapperRef: React.Ref<HTMLDivElement>;
}

function ActivityDropdown({
  tabs,
  activeTab,
  activeTabConfig,
  isOpen,
  onToggle,
  onSelect,
  wrapperRef,
}: ActivityDropdownProps) {
  return (
    <div
      className="activity-panel-mobile-select-wrap relative flex max-w-full min-w-0 flex-[1_1_auto]"
      ref={wrapperRef}
    >
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className={cn(
          "activity-panel-mobile-trigger flex w-full items-center justify-between gap-1.5 rounded border-0 bg-transparent px-2 py-1 text-[length:var(--text-base)] font-[var(--font-weight-medium)] text-[var(--text-primary)] transition-colors hover:bg-[var(--bg-tertiary)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent pointer-coarse:min-h-11 mobile:min-h-11 mobile:px-3 mobile:py-2",
          coarseHitAreaCls,
        )}
        onClick={onToggle}
        aria-expanded={isOpen}
      >
        <span className="activity-panel-mobile-trigger__value inline-flex min-w-0 items-center gap-1.5">
          <span className="activity-panel-tab-icon flex items-center justify-center">
            {activeTabConfig.icon}
          </span>
          <span className="min-w-0 truncate">{activeTabConfig.label}</span>
        </span>
        <DropdownCaret open={isOpen} />
      </Button>
      {isOpen && (
        <div className="activity-panel-mobile-menu absolute top-[calc(100%+0.25rem)] left-0 z-[5] grid max-h-[70vh] w-[min(19rem,calc(100vw-1.5rem))] grid-cols-2 gap-0.5 overflow-y-auto rounded-lg border border-border bg-[var(--bg-secondary)] p-1 shadow-[var(--shadow-lg)]">
          {[...tabs]
            .sort((a, b) => a.label.localeCompare(b.label))
            .map((tab) => (
              <Button
                key={tab.id}
                type="button"
                variant="ghost"
                size="sm"
                dense
                aria-current={activeTab === tab.id ? "page" : undefined}
                className={cn(
                  "activity-panel-mobile-menu__item",
                  "inline-flex min-h-7 w-full items-center justify-start gap-1.5 rounded-md border-0 bg-transparent px-2 py-1 text-left text-[length:var(--text-base)] font-[var(--font-weight-medium)] text-[var(--text-primary)] hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:min-h-11 pointer-coarse:min-w-11 mobile:min-h-11 mobile:min-w-11",
                  // Active pill mirrors .activity-filter-dropdown__item--active:
                  // bg-tertiary on bg-secondary was too faint to read as the
                  // current tab (#20047).
                  activeTab === tab.id &&
                    "active bg-[color-mix(in_srgb,var(--accent)_15%,transparent)] text-accent hover:bg-[color-mix(in_srgb,var(--accent)_15%,transparent)] hover:text-accent",
                  activeTab === tab.id &&
                    "[[data-theme=light]_&]:bg-accent [[data-theme=light]_&]:text-accent-foreground [[data-theme=light]_&]:hover:bg-accent [[data-theme=light]_&]:hover:text-accent-foreground",
                  coarseHitAreaCls,
                )}
                onClick={() => onSelect(tab.id)}
              >
                <span className="activity-panel-tab-icon flex items-center justify-center">
                  {tab.icon}
                </span>
                <span>{tab.label}</span>
              </Button>
            ))}
        </div>
      )}
    </div>
  );
}

export function ActivityPanel({
  mode,
  onToggleChat,
  panelWidth,
  onWidthChange,
  activeTab,
  onTabChange,
  plans,
  activePlan,
  onOpenPlan,
  onSetPlanVersion,
  planPendingApproval,
  planApproved,
  planApprovalOptions,
  onApprovePlan,
  onRequestPlanChanges,
  planPendingVariant,
  changedFiles = [],
  fetchDiff,
  changesLoading = false,
  changesError = null,
  onRetryChanges,
  projectId,
  projectName,
  sessions = [],
  sessionsLoading = false,
  sessionsFilters,
  onSessionsFiltersChange,
  onAddFileToChat,
  mcp,
  onKillAgent,
  onExpireSession,
  onAcpCloseSession,
  onAcpDeleteSession,
  chatSessionId,
  focusSessionId,
  onFocusSessionHandled,
  onSwapSession,
  onResumeSession,
  terminalFocusSessionId = null,
  onTerminalFocusHandled,
  isMobile = false,
  requestPanelOverride,
  releasePanelOverride,
  dirtyGuard,
}: ActivityPanelProps) {
  const localDirtyGuard = useDirtyGuardController();
  const dirtyGuardValue = dirtyGuard ?? localDirtyGuard;
  // viewportWidth feeds the resize-handle max-width calc only. The overlay
  // decision is mobile-only now (decoupled from desktop): the desktop
  // tri-state owns chat/split/panel, mobile owns chat/panel as an overlay.
  const [viewportWidth, setViewportWidth] = useState(() => window.innerWidth);
  const [showMobileTabMenu, setShowMobileTabMenu] = useState(false);
  const mobileTabMenuRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const handleResize = () => {
      setViewportWidth(window.innerWidth);
    };
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, []);
  // Activity panel max width derives from viewport minus chat floor — without
  // this the resize handle's static PANEL_MAX_WIDTH (1200) prevented chat from
  // reaching 320 on wide viewports (e.g. maximized 1920 → chat floor was 720).
  const effectivePanelMaxWidth = Math.max(
    PANEL_MIN_WIDTH,
    viewportWidth - CHAT_MIN_WIDTH - LAYOUT_BUFFER,
  );
  const effectivePanelWidth = Math.min(
    Math.max(panelWidth, PANEL_MIN_WIDTH),
    effectivePanelMaxWidth,
  );
  useEffect(() => {
    if (!showMobileTabMenu) {
      return;
    }

    const handleMouseDown = (event: MouseEvent) => {
      if (!(event.target instanceof Node)) {
        return;
      }
      if (!mobileTabMenuRef.current?.contains(event.target)) {
        setShowMobileTabMenu(false);
      }
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setShowMobileTabMenu(false);
      }
    };

    document.addEventListener("mousedown", handleMouseDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handleMouseDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [showMobileTabMenu]);
  const useOverlay = isMobile;
  const activeTabConfig =
    ACTIVITY_PANEL_TABS.find((tab) => tab.id === activeTab) ??
    ACTIVITY_PANEL_TABS[0];

  if (mode === "chat") return null;

  // Mobile close / desktop "Show Chat" — both delegate to the same toggle.
  const handleToggleChat = () => {
    void dirtyGuardValue.guardedRun(() => {
      setShowMobileTabMenu(false);
      onToggleChat();
    });
  };
  // Desktop only: in 'split' the button hides the chat (-> panel); in 'panel'
  // it brings the chat back (-> split).
  const chatHidden = mode === "panel";
  const handleTabSelect = (tab: ActivityTab) => {
    void dirtyGuardValue.guardedRun(() => {
      onTabChange(tab);
      setShowMobileTabMenu(false);
    });
  };

  const tabContent = () => {
    switch (activeTab) {
      case "sessions":
        return (
          <SessionsTab
            sessions={sessions}
            isLoadingSessions={sessionsLoading}
            projectName={projectName}
            filters={sessionsFilters}
            onFiltersChange={onSessionsFiltersChange}
            onKillAgent={onKillAgent}
            onExpireSession={onExpireSession}
            onAcpCloseSession={onAcpCloseSession}
            onAcpDeleteSession={onAcpDeleteSession}
            chatSessionId={chatSessionId ?? undefined}
            focusSessionId={focusSessionId}
            onFocusHandled={onFocusSessionHandled}
            onSwapSession={onSwapSession}
            onResumeSession={onResumeSession}
          />
        );
      case "terminal":
        return (
          <Suspense fallback={null}>
            <TerminalTab
              sessions={sessions}
              projectId={projectId ?? null}
              focusSessionId={terminalFocusSessionId}
              onFocusHandled={onTerminalFocusHandled}
            />
          </Suspense>
        );
      case "pipelines":
        return <PipelinesTab projectId={projectId} />;
      case "cron":
        return <CronTab projectId={projectId} />;
      case "traces":
        return <TracesTab projectId={projectId} />;
      case "mcp":
        return mcp ? <ActivityMcpTab {...mcp} /> : null;
      case "agents":
        return <AgentsTab projectId={projectId} />;
      case "stages":
        return <StagesTab projectId={projectId} />;
      case "skills":
        return <SkillsTab projectId={projectId} />;
      case "memory":
        return (
          <MemoryTab
            projectId={projectId}
            requestPanelOverride={requestPanelOverride}
            releasePanelOverride={releasePanelOverride}
          />
        );
      case "integrations":
        return <IntegrationsTab />;
      case "wiki":
        return (
          <WikiTab
            projectId={projectId}
            requestPanelOverride={requestPanelOverride}
            releasePanelOverride={releasePanelOverride}
          />
        );
      case "rules":
        return <RulesTab projectId={projectId} />;
      case "tasks":
        return <TasksTab projectId={projectId} chatSessionId={chatSessionId} />;
      case "files":
        return <FilesTab projectId={projectId} onAddToChat={onAddFileToChat} />;
      case "plans":
        return (
          <PlansTab
            plans={plans}
            activePlan={activePlan}
            onOpenPlan={onOpenPlan}
            onSetPlanVersion={onSetPlanVersion}
            planPendingApproval={planPendingApproval}
            planApproved={planApproved}
            planApprovalOptions={planApprovalOptions}
            onApprovePlan={onApprovePlan}
            onRequestPlanChanges={onRequestPlanChanges}
            planPendingVariant={planPendingVariant}
          />
        );
      case "changes":
        return (
          <FileChangesTab
            changedFiles={changedFiles}
            fetchDiff={fetchDiff || noopFetchDiff}
            loading={changesLoading}
            error={changesError}
            onRetry={onRetryChanges}
          />
        );
      default:
        return null;
    }
  };

  if (useOverlay) {
    return (
      <DirtyGuardProvider value={dirtyGuardValue}>
        <div className="activity-panel-mobile-overlay absolute inset-0 z-[200] flex flex-col bg-[var(--bg-primary)]">
          <aside
            className={activityPanelClassName("flex-1 border-l-0")}
            aria-labelledby="activity-panel-title"
          >
            <Heading
              level={1}
              id="activity-panel-title"
              className="sr-only"
            >{`Activity: ${activeTabConfig.label}`}</Heading>
            <ActivityActionsProvider>
              <div className="activity-panel-tabs flex min-h-[var(--activity-panel-bar-height)] shrink-0 items-center gap-2 border-b border-border bg-[var(--bg-secondary)] px-3 @max-[280px]/activity-panel:gap-1 @max-[280px]/activity-panel:px-1.5">
                <ActivityDropdown
                  tabs={ACTIVITY_PANEL_DROPDOWN_TABS}
                  activeTab={activeTab}
                  activeTabConfig={activeTabConfig}
                  isOpen={showMobileTabMenu}
                  onToggle={() => setShowMobileTabMenu((open) => !open)}
                  onSelect={handleTabSelect}
                  wrapperRef={mobileTabMenuRef}
                />
                <ActivityActionButtons />
                <span className="activity-panel-close-slot ml-auto flex shrink-0 items-center">
                  <Button
                    type="button"
                    variant="accent"
                    size="sm"
                    onClick={handleToggleChat}
                    aria-label="Close panel"
                    title="Close panel"
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
                      aria-hidden="true"
                    >
                      <line x1="18" y1="6" x2="6" y2="18" />
                      <line x1="6" y1="6" x2="18" y2="18" />
                    </svg>
                    <span className="activity-panel-action-btn__label">
                      Close
                    </span>
                  </Button>
                </span>
              </div>

              {/* Tab content */}
              <div className="activity-panel-content flex min-h-0 flex-1 flex-col overflow-hidden">
                {tabContent()}
              </div>
            </ActivityActionsProvider>
          </aside>
        </div>
      </DirtyGuardProvider>
    );
  }

  // 'split' = panel sits beside the chat at a resizable fixed width.
  // 'panel' = panel is the only pane: full width, no resize handle.
  const isFullWidth = mode === "panel";
  const asideStyle: CSSProperties = isFullWidth
    ? { flex: "1 1 auto", width: "100%", minWidth: PANEL_MIN_WIDTH }
    : {
        width: effectivePanelWidth,
        minWidth: PANEL_MIN_WIDTH,
        maxWidth: `calc(100vw - ${CHAT_MIN_WIDTH + LAYOUT_BUFFER}px)`,
        flexShrink: 1,
      };

  return (
    <DirtyGuardProvider value={dirtyGuardValue}>
      {!isFullWidth && (
        <ResizeHandle
          onResize={onWidthChange}
          panelWidth={effectivePanelWidth}
          minWidth={PANEL_MIN_WIDTH}
          maxWidth={effectivePanelMaxWidth}
        />
      )}
      <aside
        className={activityPanelClassName()}
        aria-labelledby="activity-panel-title"
        style={asideStyle}
      >
        <Heading
          level={1}
          id="activity-panel-title"
          className="sr-only"
        >{`Activity: ${activeTabConfig.label}`}</Heading>
        <ActivityActionsProvider>
          <div className="activity-panel-tabs flex min-h-[var(--activity-panel-bar-height)] shrink-0 items-center gap-2 border-b border-border bg-[var(--bg-secondary)] px-3 @max-[280px]/activity-panel:gap-1 @max-[280px]/activity-panel:px-1.5">
            <ActivityDropdown
              tabs={ACTIVITY_PANEL_DROPDOWN_TABS}
              activeTab={activeTab}
              activeTabConfig={activeTabConfig}
              isOpen={showMobileTabMenu}
              onToggle={() => setShowMobileTabMenu((open) => !open)}
              onSelect={handleTabSelect}
              wrapperRef={mobileTabMenuRef}
            />
            <ActivityActionButtons />
            <span className="activity-panel-close-slot ml-auto flex shrink-0 items-center">
              <Button
                type="button"
                variant="accent"
                size="sm"
                onClick={handleToggleChat}
                aria-label={chatHidden ? "Show chat" : "Hide chat"}
                title={chatHidden ? "Show chat" : "Hide chat"}
              >
                <PanelIcon visible={!chatHidden} />
                <span className="activity-panel-action-btn__label">
                  {chatHidden ? "Show Chat" : "Hide Chat"}
                </span>
              </Button>
            </span>
          </div>

          {/* Tab content */}
          <div className="activity-panel-content flex min-h-0 flex-1 flex-col overflow-hidden">
            {tabContent()}
          </div>
        </ActivityActionsProvider>
      </aside>
    </DirtyGuardProvider>
  );
}
