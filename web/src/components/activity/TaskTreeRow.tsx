import type { MouseEvent } from "react";
import type { GobbyTask } from "../../hooks/useTasks";
import { PriorityGlyph, StatusDot, TypeBadge } from "../tasks/TaskBadges";
import {
  getCanonicalTaskState,
  getTaskDisplayState,
  getTaskStateSummary,
} from "../../lib/taskState";
import {
  getStageStateColor,
  PRIORITY_TEXT_COLORS,
  PRIORITY_TEXT_WEIGHTS,
  type VisibleTaskRow,
} from "./TasksTabModel";
import { DEFAULT_TASK_PRIORITY } from "../../lib/taskOptions";

interface TaskTreeRowProps {
  row: VisibleTaskRow;
  isSelected: boolean;
  isBusy: boolean;
  onSelect: (taskId: string) => void;
  onToggleOpen: (taskId: string) => void;
  onNavigate: (taskId: string, key: "ArrowDown" | "ArrowUp" | "ArrowLeft" | "ArrowRight") => void;
  onMenuButtonClick: (event: MouseEvent<HTMLButtonElement>, task: GobbyTask) => void;
  rowRef: (node: HTMLDivElement | null) => void;
}

const TASK_ROW_BASE_INDENT_REM = 0.75;
const TASK_ROW_DEPTH_INDENT_REM = 1.25;

export function TaskTreeRow({
  row,
  isSelected,
  isBusy,
  onSelect,
  onToggleOpen,
  onNavigate,
  onMenuButtonClick,
  rowRef,
}: TaskTreeRowProps) {
  const task = row.node.task;
  const taskState = getCanonicalTaskState(task);
  const currentStage = taskState.current_stage;
  const stateSummary = getTaskStateSummary(task);
  const priority = task.priority ?? DEFAULT_TASK_PRIORITY;
  const textColor =
    PRIORITY_TEXT_COLORS[priority] ?? "var(--text-secondary)";
  const textWeight =
    PRIORITY_TEXT_WEIGHTS[priority] ?? "var(--font-weight-normal)";
  const ref = task.seq_num != null ? `#${task.seq_num}` : null;
  const labelRef = ref ?? task.ref ?? task.id;
  const labelTitle = task.title || "Untitled task";
  const taskRowClass = [
    "activity-task-row",
    isSelected && "activity-task-row--selected",
    getTaskDisplayState(task) === "closed" && "activity-task-row--closed",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div
      ref={rowRef}
      style={{
        paddingLeft: `${row.depth * TASK_ROW_DEPTH_INDENT_REM + TASK_ROW_BASE_INDENT_REM}rem`,
      }}
      className={taskRowClass}
      role="treeitem"
      tabIndex={isSelected ? 0 : -1}
      aria-level={row.depth + 1}
      aria-expanded={row.isInternal ? row.isOpen : undefined}
      aria-label={`${labelRef} ${labelTitle}: ${stateSummary}`}
      title={stateSummary}
      onClick={() => onSelect(task.id)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onSelect(task.id);
        } else if (
          event.key === "ArrowDown" ||
          event.key === "ArrowUp" ||
          event.key === "ArrowLeft" ||
          event.key === "ArrowRight"
        ) {
          event.preventDefault();
          onNavigate(task.id, event.key);
        }
      }}
    >
      {row.isInternal ? (
        <button
          className="activity-task-row-toggle"
          onClick={(event) => {
            event.stopPropagation();
            onToggleOpen(task.id);
          }}
          aria-label={`${row.isOpen ? "Collapse" : "Expand"} subtasks for ${labelTitle}`}
          title={row.isOpen ? "Collapse subtasks" : "Expand subtasks"}
        >
          <span
            className={`activity-task-row-toggle-icon${
              row.isOpen ? " activity-task-row-toggle-icon--open" : ""
            }`}
            aria-hidden="true"
          >
            <svg viewBox="0 0 12 12" fill="none">
              <path
                d="M4 2.5L8 6L4 9.5"
                stroke="currentColor"
                strokeWidth="1.9"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </span>
        </button>
      ) : (
        <span className="activity-task-row-toggle-spacer" aria-hidden="true" />
      )}
      <PriorityGlyph priority={priority} />
      <StatusDot task={task} />
      {ref && <span className="activity-task-row-ref">{ref}</span>}
      <span
        className="activity-task-row-title"
        style={{ color: textColor, fontWeight: textWeight }}
      >
        {task.title}
      </span>
      <span className="activity-task-row-chips">
        <TypeBadge type={task.task_type} />
      </span>
      {currentStage && (
        <span className="activity-task-row-stage" title={stateSummary}>
          <span
            className="activity-task-row-stage-pip"
            style={{ backgroundColor: getStageStateColor(currentStage.state) }}
            aria-hidden="true"
          />
          <span className="activity-task-row-stage-label">
            {currentStage.display_name}
          </span>
        </span>
      )}
      <button
        type="button"
        className="task-more-btn"
        onClick={(event) => onMenuButtonClick(event, task)}
        title="Task actions"
        aria-label="Task actions"
        disabled={isBusy}
      >
        <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
          <circle cx="12" cy="5" r="2" />
          <circle cx="12" cy="12" r="2" />
          <circle cx="12" cy="19" r="2" />
        </svg>
      </button>
    </div>
  );
}
