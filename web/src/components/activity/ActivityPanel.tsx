import {
  useState,
  useEffect,
  useRef,
  type ReactNode,
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
import type { Artifact } from "../../types/artifacts";
import type { GobbySession } from "../../types/sessions";
import type { CanvasPanelState } from "../canvas/hooks/useCanvasPanel";

export type ActivityTab =
  | "sessions"
  | "pipelines"
  | "tasks"
  | "files"
  | "plans"
  | "artifacts"
  | "changes"
  | "canvas";

const iconProps = {
  width: 16,
  height: 16,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
};

const TABS: Array<{ id: ActivityTab; label: string; icon: ReactNode }> = [
  {
    id: "sessions",
    label: "Sessions",
    icon: (
      <svg {...iconProps}>
        <rect x="2" y="3" width="20" height="14" rx="2" ry="2" />
        <line x1="8" y1="21" x2="16" y2="21" />
        <line x1="12" y1="17" x2="12" y2="21" />
      </svg>
    ),
  },
  {
    id: "tasks",
    label: "Tasks",
    icon: (
      <svg {...iconProps}>
        <path d="M9 11l3 3L22 4" />
        <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" />
      </svg>
    ),
  },
  {
    id: "plans",
    label: "Plans",
    icon: (
      <svg {...iconProps}>
        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
        <polyline points="14 2 14 8 20 8" />
        <line x1="16" y1="13" x2="8" y2="13" />
        <line x1="16" y1="17" x2="8" y2="17" />
        <polyline points="10 9 9 9 8 9" />
      </svg>
    ),
  },
  {
    id: "artifacts",
    label: "Artifacts",
    icon: (
      <svg {...iconProps}>
        <path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" />
        <polyline points="3.27 6.96 12 12.01 20.73 6.96" />
        <line x1="12" y1="22.08" x2="12" y2="12" />
      </svg>
    ),
  },
  {
    id: "changes",
    label: "Changes",
    icon: (
      <svg {...iconProps}>
        <path d="M12 20h9" />
        <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
      </svg>
    ),
  },
  {
    id: "files",
    label: "Files",
    icon: (
      <svg {...iconProps}>
        <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
      </svg>
    ),
  },
  {
    id: "canvas",
    label: "A2UI Canvas",
    icon: (
      <svg {...iconProps}>
        <path d="M12 19l7-7 3 3-7 7-3-3z" />
        <path d="M18 13l-1.5-7.5L2 2l3.5 14.5L13 18l5-5z" />
        <path d="M2 2l7.586 7.586" />
        <circle cx="11" cy="11" r="2" />
      </svg>
    ),
  },
  {
    id: "pipelines",
    label: "Pipelines",
    icon: (
      <svg {...iconProps}>
        <line x1="6" y1="3" x2="6" y2="15" />
        <circle cx="18" cy="6" r="3" />
        <circle cx="6" cy="18" r="3" />
        <path d="M18 9a9 9 0 0 1-9 9" />
      </svg>
    ),
  },
];

const noopFetchDiff = async (): Promise<string> => "";

// Width constants shared between the inline style, ResizeHandle, and the
// `useActivityPanel` localStorage validator. CHAT_MIN_WIDTH mirrors the
// `min-w-[400px]` on the chat column in ChatPage.tsx so the maxWidth calc
// always leaves enough room for the chat to keep its floor.
const PANEL_MIN_WIDTH = 280;
const PANEL_MAX_WIDTH = 1200;
const CHAT_MIN_WIDTH = 400;
const LAYOUT_BUFFER = 24;

type ActivityTabConfig = (typeof TABS)[number];

interface ActivityPanelProps {
  isPinned: boolean;
  onPinnedChange: (pinned: boolean) => void;
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
  sessionsFilters?: import("./sessionsFilters").SessionsFilters;
  onSessionsFiltersChange?: (
    filters: import("./sessionsFilters").SessionsFilters,
  ) => void;
  // Files tab
  onAddFileToChat?: (filePath: string) => void;
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
  isPinned,
  onPinnedChange,
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
  onKillAgent,
  onExpireSession,
  chatSessionId,
  focusSessionId,
  onFocusSessionHandled,
  onSwapSession,
  onResumeSession,
  isMobile = false,
}: ActivityPanelProps) {
  // Use overlay mode when viewport is too narrow for side-by-side layout
  const [narrowViewport, setNarrowViewport] = useState(
    () => window.innerWidth < 1100,
  );
  const [showMobileTabMenu, setShowMobileTabMenu] = useState(false);
  const mobileTabMenuRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const handleResize = () => setNarrowViewport(window.innerWidth < 1100);
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, []);
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
  const useOverlay = isMobile || narrowViewport;
  const activeTabConfig = TABS.find((tab) => tab.id === activeTab) ?? TABS[0];

  if (!isPinned) return null;

  // Mobile: close handler
  const handleClose = () => {
    setShowMobileTabMenu(false);
    onPinnedChange(false);
  };
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
        <div className="activity-panel">
          <div className="activity-panel-tabs">
            <ActivityDropdown
              tabs={TABS}
              activeTab={activeTab}
              activeTabConfig={activeTabConfig}
              isOpen={showMobileTabMenu}
              onToggle={() => setShowMobileTabMenu((open) => !open)}
              onSelect={handleTabSelect}
              wrapperRef={mobileTabMenuRef}
            />
            <button
              className="activity-panel-close"
              onClick={handleClose}
              title="Close panel"
            >
              {"\u2715"}
            </button>
          </div>

          {/* Tab content */}
          <div className="activity-panel-content">{tabContent()}</div>
        </div>
      </div>
    );
  }

  return (
    <>
      <ResizeHandle
        onResize={onWidthChange}
        panelWidth={panelWidth}
        minWidth={PANEL_MIN_WIDTH}
        maxWidth={PANEL_MAX_WIDTH}
      />
      <div
        className="activity-panel"
        style={{
          width: panelWidth,
          minWidth: PANEL_MIN_WIDTH,
          maxWidth: `calc(100vw - ${CHAT_MIN_WIDTH + LAYOUT_BUFFER}px)`,
          flexShrink: 1,
        }}
      >
        <div className="activity-panel-tabs">
          <ActivityDropdown
            tabs={TABS}
            activeTab={activeTab}
            activeTabConfig={activeTabConfig}
            isOpen={showMobileTabMenu}
            onToggle={() => setShowMobileTabMenu((open) => !open)}
            onSelect={handleTabSelect}
            wrapperRef={mobileTabMenuRef}
          />
          <button
            className="activity-panel-pin"
            onClick={() => {
              setShowMobileTabMenu(false);
              onPinnedChange(!isPinned);
            }}
            title={isPinned ? "Unpin panel" : "Pin panel"}
          >
            <PinIcon pinned={isPinned} />
          </button>
        </div>

        {/* Tab content */}
        <div className="activity-panel-content">{tabContent()}</div>
      </div>
    </>
  );
}

function PinIcon({ pinned }: { pinned: boolean }) {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      style={{ transform: pinned ? "rotate(45deg)" : undefined }}
    >
      <line x1="12" y1="17" x2="12" y2="22" />
      <path d="M5 17h14v-1.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1v4.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24Z" />
    </svg>
  );
}
