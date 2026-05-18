import { useCallback, useRef } from "react";
import type { MouseEvent } from "react";

import type { GobbyTask } from "../../hooks/useTasks";
import { ActivityPanelEmpty, TasksEmptyIcon } from "./ActivityPanelEmpty";
import { TaskTreeRow } from "./TaskTreeRow";
import type { VisibleTaskRow } from "./TasksTabModel";

interface TasksTabListProps {
  visibleRows: VisibleTaskRow[];
  /** Filtered set is empty (vs. no tasks at all) drives the empty copy. */
  isEmpty: boolean;
  isLoading: boolean;
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
 * Dependency-hierarchy tree extracted from TasksTab so the tab stays a thin
 * composer. Selection is owned by the host and feeds the detail pane. Each row
 * keeps its quick-menu affordance, which is the tap-reachable action surface on touch.
 */
export function TasksTabList({
  visibleRows,
  isEmpty,
  isLoading,
  hasAnyTasks,
  selectedTaskId,
  activeTaskActionId,
  onSelect,
  onToggleOpen,
  onMenuButtonClick,
}: TasksTabListProps) {
  const rowRefs = useRef<Map<string, HTMLDivElement>>(new Map());

  const setRowRef = useCallback((taskId: string, node: HTMLDivElement | null) => {
    if (node) {
      rowRefs.current.set(taskId, node);
    } else {
      rowRefs.current.delete(taskId);
    }
  }, []);

  const selectAndFocusTask = useCallback((taskId: string) => {
    onSelect(taskId);
    requestAnimationFrame(() => rowRefs.current.get(taskId)?.focus());
  }, [onSelect]);

  const handleNavigate = useCallback((
    taskId: string,
    key: "ArrowDown" | "ArrowUp" | "ArrowLeft" | "ArrowRight",
  ) => {
    const index = visibleRows.findIndex((row) => row.node.task.id === taskId);
    if (index === -1) return;
    const row = visibleRows[index];

    if (key === "ArrowDown") {
      const next = visibleRows[index + 1];
      if (next) selectAndFocusTask(next.node.task.id);
      return;
    }

    if (key === "ArrowUp") {
      const previous = visibleRows[index - 1];
      if (previous) selectAndFocusTask(previous.node.task.id);
      return;
    }

    if (key === "ArrowRight") {
      if (row.isInternal && !row.isOpen) {
        onToggleOpen(taskId);
        requestAnimationFrame(() => rowRefs.current.get(taskId)?.focus());
        return;
      }
      const child = visibleRows[index + 1];
      if (row.isInternal && row.isOpen && child && child.depth > row.depth) {
        selectAndFocusTask(child.node.task.id);
      }
      return;
    }

    if (row.isInternal && row.isOpen) {
      onToggleOpen(taskId);
      requestAnimationFrame(() => rowRefs.current.get(taskId)?.focus());
      return;
    }
    const parent = [...visibleRows.slice(0, index)]
      .reverse()
      .find((candidate) => candidate.depth === row.depth - 1);
    if (parent) selectAndFocusTask(parent.node.task.id);
  }, [onToggleOpen, selectAndFocusTask, visibleRows]);

  return (
    <div
      className="activity-tasks-pane min-h-0 h-full overflow-y-auto"
      role="tree"
      aria-label="Tasks"
      data-testid="task-tree"
      aria-live="polite"
      aria-busy={isLoading || activeTaskActionId !== null}
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
              onNavigate={handleNavigate}
              onMenuButtonClick={onMenuButtonClick}
              rowRef={(node) => setRowRef(task.id, node)}
            />
          );
        })
      )}
    </div>
  );
}
