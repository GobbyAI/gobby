import { useEffect, useRef, useState } from "react";
import { draggable } from "@atlaskit/pragmatic-drag-and-drop/element/adapter";

import type { GobbyTask } from "../../hooks/useTasks";
import { cn } from "../../lib/utils";
import { PriorityGlyph, StatusDot, TypeBadge } from "../tasks/TaskBadges";

interface TasksBoardCardProps {
  task: GobbyTask;
  isSelected: boolean;
  onSelectTask: (id: string) => void;
}

/**
 * D6 — a Jira-style board card: summary on top, a metadata footer underneath
 * (type · key · priority · state). Deutan-safe by construction — priority is a
 * shape glyph, state is the StatusDot shape, type is a text badge; color only
 * reinforces. Draggable; dropping on a stage column moves the task's stage.
 */
export function TasksBoardCard({
  task,
  isSelected,
  onSelectTask,
}: TasksBoardCardProps) {
  const ref = useRef<HTMLButtonElement | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const isBlocked = Boolean(task.is_blocked ?? task.state?.is_blocked);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    return draggable({
      element: el,
      getInitialData: () => ({ type: "activity-board-card", taskId: task.id }),
      onDragStart: () => setIsDragging(true),
      onDrop: () => setIsDragging(false),
    });
  }, [task.id]);

  return (
    <button
      ref={ref}
      type="button"
      data-task-id={task.id}
      onClick={() => onSelectTask(task.id)}
      className={cn(
        "flex w-full flex-col gap-2 rounded-md border bg-[var(--bg-primary)] px-3 py-2.5 text-left shadow-sm",
        "transition-colors motion-reduce:transition-none",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent",
        "cursor-grab active:cursor-grabbing",
        isSelected
          ? "border-accent bg-[var(--accent-tint)]"
          : "border-border hover:border-[var(--text-muted)]",
        isDragging && "opacity-50",
      )}
    >
      <span className="text-[length:var(--text-base)] leading-snug text-foreground">
        {task.title}
      </span>
      <span className="flex items-center gap-2 text-[length:var(--text-sm)] text-muted-foreground">
        <TypeBadge type={task.task_type} />
        <span className="font-mono text-[length:var(--text-xs)]">
          {task.ref ?? `#${task.seq_num ?? ""}`}
        </span>
        <PriorityGlyph priority={task.priority ?? 4} />
        <StatusDot task={task} />
        {isBlocked && (
          <span
            className="ml-auto rounded-full bg-[var(--color-error-soft)] px-1.5 text-[length:var(--text-2xs)] font-semibold text-[var(--color-error)]"
            aria-label="Blocked"
          >
            Blocked
          </span>
        )}
      </span>
    </button>
  );
}
