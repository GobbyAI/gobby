import { useEffect, useRef, useState } from "react";
import { dropTargetForElements } from "@atlaskit/pragmatic-drag-and-drop/element/adapter";

import type { GobbyTask } from "../../hooks/useTasks";
import { cn } from "../../lib/utils";
import { TasksBoardCard } from "./TasksBoardCard";

interface TasksBoardColumnProps {
  /** Stage name the column drops onto (`null` = the Unstaged lane). */
  stageName: string | null;
  title: string;
  tasks: GobbyTask[];
  selectedTaskId: string | null;
  onSelectTask: (id: string) => void;
}

/**
 * D6 — a Jira-style lane: recessed column panel, sticky header with a count
 * pill, scrollable card stack. The whole lane is a drop target; dropping a
 * card here moves that task to this stage (the Unstaged lane is inert).
 */
export function TasksBoardColumn({
  stageName,
  title,
  tasks,
  selectedTaskId,
  onSelectTask,
}: TasksBoardColumnProps) {
  const ref = useRef<HTMLElement | null>(null);
  const [isOver, setIsOver] = useState(false);

  useEffect(() => {
    const el = ref.current;
    if (!el || stageName === null) return;
    return dropTargetForElements({
      element: el,
      getData: () => ({ type: "activity-board-column", stageName }),
      canDrop: ({ source }) => source.data.type === "activity-board-card",
      onDragEnter: () => setIsOver(true),
      onDragLeave: () => setIsOver(false),
      onDrop: () => setIsOver(false),
    });
  }, [stageName]);

  return (
    <section
      ref={ref}
      data-stage-name={stageName ?? "__unstaged__"}
      aria-label={title}
      className={cn(
        "flex w-72 shrink-0 flex-col rounded-lg border bg-[var(--bg-secondary)]",
        isOver ? "border-accent" : "border-border",
      )}
    >
      <header className="flex items-center justify-between gap-2 border-b border-border px-3 py-2">
        <span className="truncate text-[length:var(--text-sm)] font-semibold uppercase tracking-wide text-muted-foreground">
          {title}
        </span>
        <span className="inline-flex min-w-6 items-center justify-center rounded-full bg-[var(--bg-tertiary)] px-1.5 text-[length:var(--text-xs)] font-medium text-muted-foreground">
          {tasks.length}
        </span>
      </header>
      <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto p-2">
        {tasks.length === 0 ? (
          <p className="px-1 py-6 text-center text-[length:var(--text-sm)] text-muted-foreground">
            No tasks
          </p>
        ) : (
          tasks.map((task) => (
            <TasksBoardCard
              key={task.id}
              task={task}
              isSelected={selectedTaskId === task.id}
              onSelectTask={onSelectTask}
            />
          ))
        )}
      </div>
    </section>
  );
}
