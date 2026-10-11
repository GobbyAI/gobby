import type { GobbyTask } from "../../types/tasks";
import {
  getCanonicalTaskState,
  getTaskDisplayState,
} from "../../lib/taskState";
import { QuickMenu, type QuickMenuItem } from "./QuickMenu";

export type TaskMenuAction = "assign" | "releaseClaim" | "close" | "reopen";

export interface ActiveTaskAction {
  taskId: string;
  action: TaskMenuAction;
}

export interface TaskContextMenu {
  x: number;
  y: number;
  width?: number;
  height?: number;
  task: GobbyTask;
}

interface TaskQuickMenuProps {
  menu: TaskContextMenu;
  chatSessionId?: string | null;
  activeAction: ActiveTaskAction | null;
  onClose: () => void;
  onAssignToMainChat: () => void;
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
  onReleaseClaim,
  onCloseTask,
  onReopenTask,
}: TaskQuickMenuProps) {
  const task = menu.task;
  const busy = activeAction?.taskId === task.id;
  const isClosed = getTaskDisplayState(task) === "closed";
  const isClaimed = getCanonicalTaskState(task).is_claimed;
  const items: QuickMenuItem[] = [
    {
      label: "Assign to Main Chat",
      onSelect: onAssignToMainChat,
      disabled: !chatSessionId || busy,
    },
  ];

  if (isClaimed) {
    items.push(
      { type: "separator" },
      {
        label: "Release Claim",
        onSelect: onReleaseClaim,
        disabled: isClosed || busy,
      },
    );
  }

  items.push(
    { type: "separator" },
    isClosed
      ? { label: "Reopen", onSelect: onReopenTask, disabled: busy }
      : {
          label: "Close...",
          onSelect: onCloseTask,
          disabled: busy,
          destructive: true,
        },
  );

  return (
    <QuickMenu
      anchor={{
        x: menu.x,
        y: menu.y,
        width: menu.width ?? 0,
        height: menu.height ?? 0,
      }}
      menuLabel="Task actions"
      items={items}
      onClose={onClose}
    />
  );
}
