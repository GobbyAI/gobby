import {
  useState,
  useEffect,
  useRef,
  type CSSProperties,
} from "react";
import { ResizeHandle } from "../chat/artifacts/ResizeHandle";
import { PlansTab } from "./PlansTab";
import { ArtifactsTab } from "./ArtifactsTab";
import { FileChangesTab } from "./FileChangesTab";
import { CanvasTab } from "./CanvasTab";
import { SessionsTab } from "./SessionsTab";
import { PipelinesTab } from "./PipelinesTab";
import { TasksTab } from "./TasksTab";
import { FilesTab } from "./FilesTab";
import { CronTab } from "./CronTab";
import { TracesTab } from "./TracesTab";
import { ActivityMcpTab, type ActivityMcpTabProps } from "./ActivityMcpTab";
import type { Artifact } from "../../types/artifacts";
import type { GobbySession } from "../../types/sessions";
import type { CanvasPanelState } from "../canvas/hooks/useCanvasPanel";
import type { SessionsFilters } from "./sessionsFilters";
import type { LayoutMode } from "./useActivityPanel";
import { PanelIcon } from "../chat/icons/PanelIcon";
import { Heading } from '../shared/Heading'
import {
  ACTIVITY_PANEL_DROPDOWN_TABS,
  ACTIVITY_PANEL_TABS,
  type ActivityTab,
} from "./ActivityPanelTabs";

const noopFetchDiff = async (): Promise<string> => "";

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
  // Artifacts tab props
  artifacts: Map<string, Artifact>;
  activeArtifact: Artifact | null;
  onOpenArtifact: (id: string) => void;
  onCloseArtifact: () => void;
  onUpdateArtifactContent?: (id: string, content: string) => void;
  onSetArtifactVersion: (id: string, index: number) => void;
  planPendingApproval?: boolean;
  onApprovePlan?: () => void;
  onRequestPlanChanges?: (feedback: string) => void;
  // Canvas tab props
  canvasState: CanvasPanelState | null;
  onCloseCanvas: () => void;
  // Clear callbacks
  onClearCanvas?: () => void;
  // File changes tab
  changedFiles?: { path: string; status: string }[];
  fetchDiff?: (path: string) => Promise<string>;
  // Tasks tab
  projectId?: string | null;
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
  onExpireSession?: (sessionId: string) => Promise<boolean | void> | boolean | void;
  chatSessionId?: string | null;
  focusSessionId?: string | null;
  onFocusSessionHandled?: () => void;
  onSwapSession?: (target: import("../../types/chat").SwappedSessionTarget) => void;
  onResumeSession?: (sessionId: string) => Promise<string> | string | void;
  isMobile?: boolean;
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
    <div className="activity-panel-mobile-select-wrap" ref={wrapperRef}>
      <button
        type="button"
        className="activity-panel-mobile-trigger"
        onClick={onToggle}
        aria-haspopup="menu"
        aria-expanded={isOpen}
      >
        <span className="activity-panel-mobile-trigger__value">
          <span className="activity-panel-tab-icon">{activeTabConfig.icon}</span>
          <span>{activeTabConfig.label}</span>
        </span>
        <span className="activity-panel-mobile-trigger__caret" aria-hidden="true">
          <svg
            viewBox="0 0 16 16"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            {isOpen ? (
              <polyline points="4 10 8 6 12 10" />
            ) : (
              <polyline points="4 6 8 10 12 6" />
            )}
          </svg>
        </span>
      </button>
      {isOpen && (
        <div className="activity-panel-mobile-menu" role="menu">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              type="button"
              role="menuitemradio"
              aria-checked={activeTab === tab.id}
              className={`activity-panel-mobile-menu__item${activeTab === tab.id ? " active" : ""}`}
              onClick={() => onSelect(tab.id)}
            >
              <span className="activity-panel-tab-icon">{tab.icon}</span>
              <span>{tab.label}</span>
            </button>
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
  artifacts,
  activeArtifact,
  onOpenArtifact,
  onCloseArtifact,
  onUpdateArtifactContent,
  onSetArtifactVersion,
  planPendingApproval,
  onApprovePlan,
  onRequestPlanChanges,
  canvasState,
  onCloseCanvas,
  onClearCanvas,
  changedFiles = [],
  fetchDiff,
  projectId,
  sessions = [],
  sessionsLoading = false,
  sessionsFilters,
  onSessionsFiltersChange,
  onAddFileToChat,
  mcp,
  onKillAgent,
  onExpireSession,
  chatSessionId,
  focusSessionId,
  onFocusSessionHandled,
  onSwapSession,
  onResumeSession,
  isMobile = false,
}: ActivityPanelProps) {
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
    ACTIVITY_PANEL_TABS.find((tab) => tab.id === activeTab) ?? ACTIVITY_PANEL_TABS[0];

  if (mode === "chat") return null;

  // Mobile close / desktop "Show Chat" — both delegate to the same toggle.
  const handleToggleChat = () => {
    setShowMobileTabMenu(false);
    onToggleChat();
  };
  // Desktop only: in 'split' the button hides the chat (-> panel); in 'panel'
  // it brings the chat back (-> split).
  const chatHidden = mode === "panel";
  const handleTabSelect = (tab: ActivityTab) => {
    onTabChange(tab);
    setShowMobileTabMenu(false);
  };

  const tabContent = () => {
    switch (activeTab) {
      case "sessions":
        return (
          <SessionsTab
            sessions={sessions}
            isLoadingSessions={sessionsLoading}
            filters={sessionsFilters}
            onFiltersChange={onSessionsFiltersChange}
            onKillAgent={onKillAgent}
            onExpireSession={onExpireSession}
            chatSessionId={chatSessionId ?? undefined}
            focusSessionId={focusSessionId}
            onFocusHandled={onFocusSessionHandled}
            onSwapSession={onSwapSession}
            onResumeSession={onResumeSession}
          />
        );
      case "pipelines":
        return <PipelinesTab projectId={projectId} />;
      case "cron":
        return <CronTab projectId={projectId} />;
      case "traces":
        return <TracesTab projectId={projectId} />;
      case "mcp":
        return mcp ? <ActivityMcpTab {...mcp} /> : null;
      case "tasks":
        return <TasksTab projectId={projectId} chatSessionId={chatSessionId} />;
      case "files":
        return <FilesTab projectId={projectId} onAddToChat={onAddFileToChat} />;
      case "plans":
        return (
          <PlansTab
            artifacts={artifacts}
            artifact={activeArtifact}
            onOpenArtifact={onOpenArtifact}
            onClose={onCloseArtifact}
            onUpdateContent={onUpdateArtifactContent}
            onSetVersion={onSetArtifactVersion}
            planPendingApproval={planPendingApproval}
            onApprovePlan={onApprovePlan}
            onRequestPlanChanges={onRequestPlanChanges}
          />
        );
      case "artifacts":
        return (
          <ArtifactsTab
            artifacts={artifacts}
            artifact={activeArtifact}
            onOpenArtifact={onOpenArtifact}
            onClose={onCloseArtifact}
            onUpdateContent={onUpdateArtifactContent}
            onSetVersion={onSetArtifactVersion}
            planPendingApproval={planPendingApproval}
            onApprovePlan={onApprovePlan}
            onRequestPlanChanges={onRequestPlanChanges}
          />
        );
      case "changes":
        return (
          <FileChangesTab
            changedFiles={changedFiles}
            fetchDiff={fetchDiff || noopFetchDiff}
          />
        );
      case "canvas":
        return (
          <CanvasTab
            state={canvasState}
            onClose={onCloseCanvas}
            onClearAll={onClearCanvas}
          />
        );
      default:
        return null;
    }
  };

  if (useOverlay) {
    return (
      <div className="activity-panel-mobile-overlay">
        <aside className="activity-panel" aria-labelledby="activity-panel-title">
          <Heading level={1} id="activity-panel-title" className="sr-only">{`Activity: ${activeTabConfig.label}`}</Heading>
          <div className="activity-panel-tabs">
            <ActivityDropdown
              tabs={ACTIVITY_PANEL_DROPDOWN_TABS}
              activeTab={activeTab}
              activeTabConfig={activeTabConfig}
              isOpen={showMobileTabMenu}
              onToggle={() => setShowMobileTabMenu((open) => !open)}
              onSelect={handleTabSelect}
              wrapperRef={mobileTabMenuRef}
            />
            <span className="activity-panel-close-slot">
              <button
                type="button"
                className="btn btn-accent btn-sm activity-panel-action-btn"
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
                <span className="activity-panel-action-btn__label">Close</span>
              </button>
            </span>
          </div>

          {/* Tab content */}
          <div className="activity-panel-content">{tabContent()}</div>
        </aside>
      </div>
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
    <>
      {!isFullWidth && (
        <ResizeHandle
          onResize={onWidthChange}
          panelWidth={effectivePanelWidth}
          minWidth={PANEL_MIN_WIDTH}
          maxWidth={effectivePanelMaxWidth}
        />
      )}
      <aside
        className="activity-panel"
        aria-labelledby="activity-panel-title"
        style={asideStyle}
      >
        <Heading level={1} id="activity-panel-title" className="sr-only">{`Activity: ${activeTabConfig.label}`}</Heading>
        <div className="activity-panel-tabs">
          <ActivityDropdown
            tabs={ACTIVITY_PANEL_DROPDOWN_TABS}
            activeTab={activeTab}
            activeTabConfig={activeTabConfig}
            isOpen={showMobileTabMenu}
            onToggle={() => setShowMobileTabMenu((open) => !open)}
            onSelect={handleTabSelect}
            wrapperRef={mobileTabMenuRef}
          />
          <span className="activity-panel-close-slot">
            <button
              type="button"
              className="btn btn-accent btn-sm activity-panel-action-btn"
              onClick={handleToggleChat}
              aria-label={chatHidden ? "Show chat" : "Hide chat"}
              title={chatHidden ? "Show chat" : "Hide chat"}
            >
              <PanelIcon visible={!chatHidden} />
              <span className="activity-panel-action-btn__label">
                {chatHidden ? "Show Chat" : "Hide Chat"}
              </span>
            </button>
          </span>
        </div>

        {/* Tab content */}
        <div className="activity-panel-content">{tabContent()}</div>
      </aside>
    </>
  );
}
