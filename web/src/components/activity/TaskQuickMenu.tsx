import { useCallback, useEffect, useRef } from "react";
import type { CSSProperties, KeyboardEvent } from "react";
import type { BuildState, GobbyTask } from "../../hooks/useTasks";
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
  const menuRef = useRef<HTMLDivElement>(null);
  const enabledItemsRef = useRef<HTMLButtonElement[]>([]);
  const busy = activeAction?.taskId === task.id;
  const isClosed = getTaskDisplayState(task) === "closed";
  const isClaimed = getCanonicalTaskState(task).is_claimed;
  const buildState: BuildState = task.build_state ?? "never_started";
  const showStartBuild = !isClosed && buildState === "never_started";
  const showStopBuild = !isClosed && buildState === "running";
  const showResumeBuild = !isClosed && buildState === "paused";
  const showBuildControls = showStartBuild || showStopBuild || showResumeBuild;
  const menuStyle: CSSProperties = {
    position: "fixed",
    left: menu.x,
    top: menu.y,
  };
  const refreshEnabledMenuItems = useCallback(() => {
    enabledItemsRef.current = Array.from(
      menuRef.current?.querySelectorAll<HTMLButtonElement>(
        '[role="menuitem"]:not(:disabled)',
      ) ?? [],
    );
    return enabledItemsRef.current;
  }, []);

  const focusMenuItem = (index: number) => {
    const items = enabledItemsRef.current;
    if (!items.length) return;
    const nextIndex = (index + items.length) % items.length;
    items[nextIndex]?.focus();
  };

  useEffect(() => {
    const items = refreshEnabledMenuItems();
    items[0]?.focus();
  }, [
    busy,
    chatSessionId,
    isClaimed,
    isClosed,
    menu.task.id,
    refreshEnabledMenuItems,
    showBuildControls,
    showResumeBuild,
    showStartBuild,
    showStopBuild,
  ]);

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const items = enabledItemsRef.current;
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (!items.length) return;
    const currentIndex = items.indexOf(document.activeElement as HTMLButtonElement);
    if (event.key === "ArrowDown") {
      event.preventDefault();
      focusMenuItem(currentIndex === -1 ? 0 : currentIndex + 1);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      focusMenuItem(currentIndex === -1 ? items.length - 1 : currentIndex - 1);
    } else if (event.key === "Home") {
      event.preventDefault();
      focusMenuItem(0);
    } else if (event.key === "End") {
      event.preventDefault();
      focusMenuItem(items.length - 1);
    }
  };

  return (
    <>
      <div className="session-ctx-backdrop" onClick={onClose} />
      <div
        ref={menuRef}
        className="session-ctx-menu"
        style={menuStyle}
        role="menu"
        aria-label="Task actions"
        tabIndex={-1}
        onKeyDown={handleKeyDown}
      >
        <button
          className="session-ctx-item"
          role="menuitem"
          onClick={onAssignToMainChat}
          disabled={!chatSessionId || busy}
        >
          Assign to Main Chat
        </button>
        {showBuildControls && (
          <>
            <div className="session-ctx-divider" role="separator" />
            {showStartBuild && (
              <>
                <button className="session-ctx-item" role="menuitem" onClick={onBuild} disabled={busy}>
                  Build
                </button>
                <button
                  className="session-ctx-item"
                  role="menuitem"
                  onClick={onBuildQuick}
                  disabled={busy}
                >
                  Build Quick
                </button>
              </>
            )}
            {showStopBuild && (
              <button className="session-ctx-item" role="menuitem" onClick={onStopBuild} disabled={busy}>
                Stop Build
              </button>
            )}
            {showResumeBuild && (
              <button className="session-ctx-item" role="menuitem" onClick={onResumeBuild} disabled={busy}>
                Resume Build
              </button>
            )}
          </>
        )}
        {isClaimed && (
          <>
            <div className="session-ctx-divider" role="separator" />
            <button
              className="session-ctx-item"
              role="menuitem"
              onClick={onReleaseClaim}
              disabled={isClosed || busy}
            >
              Release Claim
            </button>
          </>
        )}
        <div className="session-ctx-divider" role="separator" />
        {isClosed ? (
          <button className="session-ctx-item" role="menuitem" onClick={onReopenTask} disabled={busy}>
            Reopen
          </button>
        ) : (
          <button
            className="session-ctx-item session-ctx-item--destructive"
            role="menuitem"
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
