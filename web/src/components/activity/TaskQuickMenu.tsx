import type { CSSProperties } from "react";
import type { GobbyTask } from "../../hooks/useTasks";
import { getCanonicalTaskState, getTaskDisplayState } from "../../lib/taskState";

export type TaskMenuAction =
  | "assign"
  | "build"
  | "buildQuick"
  | "stopBuild"
  | "resumeBuild"
  | "releaseClaim"
  | "close"
  | "reopen";

export interface ActiveTaskAction {
  taskId: string;
  action: TaskMenuAction;
}

export interface TaskContextMenu {
  x: number;
  y: number;
  task: GobbyTask;
}

interface TaskQuickMenuProps {
  menu: TaskContextMenu;
  chatSessionId?: string | null;
  activeAction: ActiveTaskAction | null;
  onClose: () => void;
  onAssignToMainChat: () => void;
  onBuild: () => void;
  onBuildQuick: () => void;
  onStopBuild: () => void;
  onResumeBuild: () => void;
  onReleaseClaim: () => void;
  onCloseTask: () => void;
  onReopenTask: () => void;
}

function hasBuildEvidence(task: GobbyTask): boolean {
  const hasStageEvidence =
    Boolean(task.current_stage) ||
    Boolean(task.state?.current_stage) ||
    (task.stages?.length ?? 0) > 0;
  const hasBuildConfig =
    Boolean(task.assigned_agent) ||
    (task.additional_skills?.length ?? 0) > 0 ||
    Boolean(task.yolo) ||
    Boolean(task.isolation && task.isolation !== "none");
  return hasStageEvidence || hasBuildConfig || (task.dispatch_failure_count ?? 0) > 0;
}

export function TaskQuickMenu({
  menu,
  chatSessionId,
  activeAction,
  onClose,
  onAssignToMainChat,
  onBuild,
  onBuildQuick,
  onStopBuild,
  onResumeBuild,
  onReleaseClaim,
  onCloseTask,
  onReopenTask,
}: TaskQuickMenuProps) {
  const task = menu.task;
  const busy = activeAction?.taskId === task.id;
  const isClosed = getTaskDisplayState(task) === "closed";
  const isClaimed = getCanonicalTaskState(task).is_claimed;
  const buildRunning = task.allow_automation === true;
  const buildPaused = !isClosed && !buildRunning && hasBuildEvidence(task);
  const showStartBuild = !isClosed && !buildRunning && !buildPaused;
  const showStopBuild = !isClosed && buildRunning;
  const showResumeBuild = !isClosed && buildPaused;
  const showBuildControls = showStartBuild || showStopBuild || showResumeBuild;
  const menuStyle: CSSProperties = {
    position: "fixed",
    left: menu.x,
    top: menu.y,
  };

  return (
    <>
      <div className="session-ctx-backdrop" onClick={onClose} />
      <div className="session-ctx-menu" style={menuStyle}>
        <button
          className="session-ctx-item"
          onClick={() => {
            void onAssignToMainChat();
          }}
          disabled={!chatSessionId || busy}
        >
          Assign to Main Chat
        </button>
        {showBuildControls && (
          <>
            <div className="session-ctx-divider" />
            {showStartBuild && (
              <>
                <button className="session-ctx-item" onClick={onBuild} disabled={busy}>
                  Build
                </button>
                <button
                  className="session-ctx-item"
                  onClick={onBuildQuick}
                  disabled={busy}
                >
                  Build Quick
                </button>
              </>
            )}
            {showStopBuild && (
              <button className="session-ctx-item" onClick={onStopBuild} disabled={busy}>
                Stop Build
              </button>
            )}
            {showResumeBuild && (
              <button className="session-ctx-item" onClick={onResumeBuild} disabled={busy}>
                Resume Build
              </button>
            )}
          </>
        )}
        {isClaimed && (
          <>
            <div className="session-ctx-divider" />
            <button
              className="session-ctx-item"
              onClick={onReleaseClaim}
              disabled={isClosed || busy}
            >
              Release Claim
            </button>
          </>
        )}
        <div className="session-ctx-divider" />
        {isClosed ? (
          <button className="session-ctx-item" onClick={onReopenTask} disabled={busy}>
            Reopen
          </button>
        ) : (
          <button
            className="session-ctx-item session-ctx-item--destructive"
            onClick={onCloseTask}
            disabled={busy}
          >
            Close...
          </button>
        )}
      </div>
    </>
  );
}
