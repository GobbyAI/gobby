import type { MouseEvent } from "react";

import type { GobbyTask } from "../../hooks/useTasks";
import { ActivityPanelEmpty, TasksEmptyIcon } from "./ActivityPanelEmpty";
import { TaskTreeRow } from "./TaskTreeRow";
import type { VisibleTaskRow } from "./TasksTabModel";

interface TasksTabListProps {
  visibleRows: VisibleTaskRow[];
  /** Filtered set is empty (vs. no tasks at all) drives the empty copy. */
  isEmpty: boolean;
  hasAnyTasks: boolean;
  selectedTaskId: string | null;
  activeTaskActionId: string | null;
  onSelect: (taskId: string) => void;
  onToggleOpen: (taskId: string) => void;
  onMenuButtonClick: (
    event: MouseEvent<HTMLButtonElement>,
    task: GobbyTask,
  ) => void;
}

/**
 * D6 — the List view: the existing dependency-hierarchy tree, extracted from
 * TasksTab so the tab stays a thin composer. Selection is owned by the host
 * and shared with the Board view, so both feed the same D5 detail pane. Each
 * row keeps its quick-menu affordance (claim/release/stage), which is the
 * tap-reachable action surface on touch.
 */
export function TasksTabList({
  visibleRows,
  isEmpty,
  hasAnyTasks,
  selectedTaskId,
  activeTaskActionId,
  onSelect,
  onToggleOpen,
  onMenuButtonClick,
}: TasksTabListProps) {
  return (
    <div
      className="activity-tasks-pane min-h-0 h-full overflow-y-auto"
      role={isEmpty ? undefined : "tree"}
      aria-label={isEmpty ? undefined : "Tasks"}
      data-testid={isEmpty ? undefined : "task-tree"}
    >
      {isEmpty ? (
        <ActivityPanelEmpty
          icon={<TasksEmptyIcon />}
          heading="Tasks"
          body={
            hasAnyTasks
              ? "Tasks exist, but none match the current filters"
              : "Tasks appear here as they are created"
          }
        />
      ) : (
        visibleRows.map((row) => {
          const task = row.node.task;
          return (
            <TaskTreeRow
              key={task.id}
              row={row}
              isSelected={selectedTaskId === task.id}
              isBusy={activeTaskActionId === task.id}
              onSelect={onSelect}
              onToggleOpen={onToggleOpen}
              onMenuButtonClick={onMenuButtonClick}
            />
          );
        })
      )}
    </div>
  );
}
