import {
  useState,
  useCallback,
  useEffect,
  useRef,
  type ReactNode,
} from "react";
import {
  TooltipProvider,
  Tooltip,
  TooltipTrigger,
  TooltipContent,
} from "../chat/ui/Tooltip";
import { ResizeHandle } from "../chat/artifacts/ResizeHandle";
import { PlansTab } from "./PlansTab";
import { FileChangesTab } from "./FileChangesTab";
import { CanvasTab } from "./CanvasTab";
import { SessionsTab } from "./SessionsTab";
import { PipelinesTab } from "./PipelinesTab";
import { TasksTab } from "./TasksTab";
import { FilesTab } from "./FilesTab";
import type { Artifact } from "../../types/artifacts";
import type { CanvasPanelState } from "../canvas/hooks/useCanvasPanel";

export type ActivityTab =
  | "sessions"
  | "pipelines"
  | "tasks"
  | "files"
  | "plans"
  | "artifacts"
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
    id: "files",
    label: "Files",
    icon: (
      <svg {...iconProps}>
        <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />
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
    label: "Changes",
    icon: (
      <svg {...iconProps}>
        <path d="M12 20h9" />
        <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z" />
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
];

const noopFetchDiff = async (): Promise<string> => "";

const STORAGE_KEY_PINNED = "gobby-activity-panel-pinned";
const STORAGE_KEY_WIDTH = "gobby-activity-panel-width";
const STORAGE_KEY_TAB = "gobby-activity-panel-tab";

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
  // Files tab
  onAddFileToChat?: (filePath: string) => void;
  // Sessions tab
  onKillAgent?: (runId: string) => void;
  onExpireSession?: (sessionId: string) => void;
  chatSessionId?: string | null;
  focusSessionId?: string | null;
  onFocusSessionHandled?: () => void;
  watchingSessionIds?: Set<string>;
  onUnwatchSession?: (sessionId: string) => void;
  onAttachSession?: (sessionId: string) => void;
  isMobile?: boolean;
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
  onAddFileToChat,
  onKillAgent,
  onExpireSession,
  chatSessionId,
  focusSessionId,
  onFocusSessionHandled,
  watchingSessionIds,
  onUnwatchSession,
  onAttachSession,
  isMobile = false,
}: ActivityPanelProps) {
  // Use overlay mode when viewport is too narrow for side-by-side layout
  const [narrowViewport, setNarrowViewport] = useState(
    () => window.innerWidth < 1100,
  );
  useEffect(() => {
    const handleResize = () => setNarrowViewport(window.innerWidth < 1100);
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, []);
  const useOverlay = isMobile || narrowViewport;

  if (!isPinned) return null;

  // Mobile: close handler
  const handleClose = () => onPinnedChange(false);

  const tabContent = () => {
    switch (activeTab) {
      case "sessions":
        return (
          <SessionsTab
            projectId={projectId}
            onKillAgent={onKillAgent}
            onExpireSession={onExpireSession}
            chatSessionId={chatSessionId ?? undefined}
            focusSessionId={focusSessionId}
            onFocusHandled={onFocusSessionHandled}
            watchingSessionIds={watchingSessionIds}
            onUnwatch={onUnwatchSession}
            onAttachSession={onAttachSession}
            isMobile={useOverlay}
          />
        );
      case "pipelines":
        return <PipelinesTab projectId={projectId} />;
      case "tasks":
        return <TasksTab projectId={projectId} />;
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
          {/* Icon tab strip with close button */}
          <div className="activity-panel-tabs">
            <div className="activity-panel-tab-strip" role="tablist">
              {TABS.map((tab) => (
                <button
                  key={tab.id}
                  role="tab"
                  aria-selected={activeTab === tab.id}
                  className={`activity-panel-tab${activeTab === tab.id ? " active" : ""}`}
                  onClick={() => onTabChange(tab.id)}
                  title={tab.label}
                >
                  <span className="activity-panel-tab-icon">{tab.icon}</span>
                  {activeTab === tab.id && (
                    <span className="activity-panel-tab-label">
                      {tab.label}
                    </span>
                  )}
                </button>
              ))}
            </div>
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
        minWidth={280}
        maxWidth={1200}
      />
      <div
        className="activity-panel"
        style={{ width: panelWidth, flexShrink: 0 }}
      >
        {/* Tab strip */}
        <div className="activity-panel-tabs">
          <TooltipProvider delayDuration={200}>
            <div className="activity-panel-tab-strip" role="tablist">
              {TABS.map((tab) => (
                <Tooltip key={tab.id}>
                  <TooltipTrigger asChild>
                    <button
                      role="tab"
                      aria-selected={activeTab === tab.id}
                      className={`activity-panel-tab${activeTab === tab.id ? " active" : ""}`}
                      onClick={() => onTabChange(tab.id)}
                    >
                      <span className="activity-panel-tab-icon">
                        {tab.icon}
                      </span>
                      {activeTab === tab.id && (
                        <span className="activity-panel-tab-label">
                          {tab.label}
                        </span>
                      )}
                    </button>
                  </TooltipTrigger>
                  <TooltipContent side="bottom">{tab.label}</TooltipContent>
                </Tooltip>
              ))}
            </div>
          </TooltipProvider>
          <button
            className="activity-panel-pin"
            onClick={() => onPinnedChange(!isPinned)}
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

// Hooks for persisting panel state
export function useActivityPanel() {
  const [isPinned, setIsPinned] = useState(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY_PINNED);
      if (stored !== null) return stored === "true";
    } catch {
      /* ignore */
    }
    // Default: pinned on desktop
    return window.innerWidth >= 1100;
  });

  const [panelWidth, setPanelWidth] = useState(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY_WIDTH);
      if (stored) {
        const w = parseInt(stored, 10);
        if (w >= 280 && w <= 1200) return w;
      }
    } catch {
      /* ignore */
    }
    return 360;
  });

  const [activeTab, setActiveTab] = useState<ActivityTab>(() => {
    try {
      const stored = localStorage.getItem(
        STORAGE_KEY_TAB,
      ) as ActivityTab | null;
      if (stored && TABS.some((t) => t.id === stored)) return stored;
    } catch {
      /* ignore */
    }
    return "tasks";
  });

  // Persist
  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY_PINNED, String(isPinned));
    } catch {
      /* ignore */
    }
  }, [isPinned]);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY_WIDTH, String(panelWidth));
    } catch {
      /* ignore */
    }
  }, [panelWidth]);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY_TAB, activeTab);
    } catch {
      /* ignore */
    }
  }, [activeTab]);

  // No auto-collapse on narrow viewport — the panel switches to overlay
  // mode when the viewport is too narrow for side-by-side layout.

  // Track whether the panel was opened programmatically (via showTab)
  // vs manually by the user, so we can auto-close it when the artifact
  // or canvas that triggered the open is dismissed.
  const autoOpenedRef = useRef(false);

  const showTab = useCallback(
    (tab: ActivityTab) => {
      setActiveTab(tab);
      if (!isPinned) {
        setIsPinned(true);
        autoOpenedRef.current = true;
      }
    },
    [isPinned],
  );

  const closeIfAutoOpened = useCallback(() => {
    if (autoOpenedRef.current) {
      setIsPinned(false);
      autoOpenedRef.current = false;
    }
  }, []);

  const togglePanel = useCallback(() => {
    autoOpenedRef.current = false;
    setIsPinned((prev) => !prev);
  }, []);

  // Clear auto-opened flag when user manually changes tab
  const handleTabChange = useCallback((tab: ActivityTab) => {
    autoOpenedRef.current = false;
    setActiveTab(tab);
  }, []);

  return {
    isPinned,
    setIsPinned,
    panelWidth,
    setPanelWidth,
    activeTab,
    setActiveTab: handleTabChange,
    showTab,
    closeIfAutoOpened,
    togglePanel,
  };
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
