import { useCallback, useLayoutEffect, useMemo, useRef } from "react";
import type { KeyboardEvent, MouseEvent } from "react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";

import type { GobbyTask } from "../../types/tasks";
import {
  useTreeKeyboardNavigation,
  type TreeNavItem,
} from "../../hooks/useTreeKeyboardNavigation";
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

const TASK_TREE_WINDOWING_THRESHOLD = 100;
const TASK_TREE_INITIAL_ITEM_COUNT = 20;

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
  const navItems = useMemo<TreeNavItem[]>(
    () =>
      visibleRows.map((row) => ({
        id: row.node.task.id,
        depth: row.depth,
        isExpandable: row.isInternal,
        isExpanded: row.isOpen,
      })),
    [visibleRows],
  );

  const rowIndexById = useMemo(
    () => new Map(visibleRows.map((row, index) => [row.node.task.id, index])),
    [visibleRows],
  );
  const virtuosoRef = useRef<VirtuosoHandle>(null);
  const onFocusRequest = useCallback(
    (taskId: string) => {
      const index = rowIndexById.get(taskId);
      if (index !== undefined && visibleRows.length >= TASK_TREE_WINDOWING_THRESHOLD) {
        virtuosoRef.current?.scrollToIndex({ index, align: "center" });
      }
    },
    [rowIndexById, visibleRows.length],
  );

  const { setRowRef, handleKeyDown, getTabIndex } = useTreeKeyboardNavigation({
    items: navItems,
    selectedId: selectedTaskId,
    onSelect,
    onToggle: onToggleOpen,
    onFocusRequest,
  });

  const callbacksRef = useRef({ onSelect, onToggleOpen, onMenuButtonClick });
  useLayoutEffect(() => {
    callbacksRef.current = { onSelect, onToggleOpen, onMenuButtonClick };
  }, [onMenuButtonClick, onSelect, onToggleOpen]);
  const stableOnSelect = useCallback(
    (taskId: string) => callbacksRef.current.onSelect(taskId),
    [],
  );
  const stableOnToggleOpen = useCallback(
    (taskId: string) => callbacksRef.current.onToggleOpen(taskId),
    [],
  );
  const stableOnMenuButtonClick = useCallback(
    (event: MouseEvent<HTMLButtonElement>, task: GobbyTask) =>
      callbacksRef.current.onMenuButtonClick(event, task),
    [],
  );
  const navigationRef = useRef({ setRowRef, handleKeyDown });
  useLayoutEffect(() => {
    navigationRef.current = { setRowRef, handleKeyDown };
  }, [handleKeyDown, setRowRef]);
  const stableSetRowRef = useCallback(
    (taskId: string, node: HTMLDivElement | null) =>
      navigationRef.current.setRowRef(taskId, node),
    [],
  );
  const stableOnRowKeyDown = useCallback(
    (taskId: string, event: KeyboardEvent<HTMLDivElement>) =>
      navigationRef.current.handleKeyDown(taskId, event),
    [],
  );

  const renderRow = useCallback(
    (row: VisibleTaskRow) => {
      const task = row.node.task;
      return (
        <TaskTreeRow
          key={task.id}
          row={row}
          isSelected={selectedTaskId === task.id}
          isBusy={activeTaskActionId === task.id}
          onSelect={stableOnSelect}
          onToggleOpen={stableOnToggleOpen}
          onMenuButtonClick={stableOnMenuButtonClick}
          setRowRef={stableSetRowRef}
          tabIndex={getTabIndex(task.id)}
          onRowKeyDown={stableOnRowKeyDown}
        />
      );
    },
    [
      activeTaskActionId,
      getTabIndex,
      selectedTaskId,
      stableOnMenuButtonClick,
      stableOnRowKeyDown,
      stableOnSelect,
      stableOnToggleOpen,
      stableSetRowRef,
    ],
  );
  const renderVirtualRow = useCallback(
    (_index: number, row: VisibleTaskRow) => renderRow(row),
    [renderRow],
  );

  const treeProps = {
    role: "tree",
    "aria-label": "Tasks",
    "data-testid": "task-tree",
    "aria-live": "polite" as const,
    "aria-busy": isLoading || activeTaskActionId !== null,
  };

  if (!isEmpty && visibleRows.length >= TASK_TREE_WINDOWING_THRESHOLD) {
    return (
      <Virtuoso
        {...treeProps}
        ref={virtuosoRef}
        className="activity-tasks-pane min-h-0 h-full"
        data={visibleRows}
        computeItemKey={(_index, row) => row.node.task.id}
        initialItemCount={Math.min(TASK_TREE_INITIAL_ITEM_COUNT, visibleRows.length)}
        increaseViewportBy={200}
        itemContent={renderVirtualRow}
      />
    );
  }

  return (
    <div
      {...treeProps}
      className="activity-tasks-pane min-h-0 h-full overflow-y-auto"
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
        visibleRows.map(renderRow)
      )}
    </div>
  );
}
