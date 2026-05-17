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
        <div className="session-ctx-divider" />
        <button className="session-ctx-item" onClick={onBuild} disabled={isClosed || busy}>
          Build
        </button>
        <button
          className="session-ctx-item"
          onClick={onBuildQuick}
          disabled={isClosed || busy}
        >
          Build Quick
        </button>
        <button
          className="session-ctx-item"
          onClick={onStopBuild}
          disabled={isClosed || busy}
        >
          Stop Build
        </button>
        <button
          className="session-ctx-item"
          onClick={onResumeBuild}
          disabled={isClosed || busy}
        >
          Resume Build
        </button>
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
