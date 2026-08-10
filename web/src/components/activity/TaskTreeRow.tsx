import { memo } from "react";
import type { KeyboardEvent, MouseEvent } from "react";
import type { GobbyTask } from "../../types/tasks";
import { PriorityGlyph, StatusDot, TypeBadge } from "../tasks/TaskBadges";
import {
  getCanonicalTaskState,
  getTaskDisplayState,
  getTaskStateSummary,
  type CanonicalTaskState,
  type TaskDisplayState,
} from "../../lib/taskState";
import {
  getStageStateColor,
  PRIORITY_TEXT_COLORS,
  PRIORITY_TEXT_WEIGHTS,
  type VisibleTaskRow,
} from "./TasksTabModel";
import { DEFAULT_TASK_PRIORITY } from "../../lib/taskOptions";
import { KebabIcon } from "./QuickMenu";
import { cn } from "../../lib/utils";
import { Button } from "../ui/Button";
import { coarseHitAreaCls } from "../ui/controlStyles";

interface TaskTreeRowProps {
  row: VisibleTaskRow;
  isSelected: boolean;
  isBusy: boolean;
  onSelect: (taskId: string) => void;
  onToggleOpen: (taskId: string) => void;
  onMenuButtonClick: (event: MouseEvent<HTMLButtonElement>, task: GobbyTask) => void;
  setRowRef: (taskId: string, node: HTMLDivElement | null) => void;
  tabIndex: number;
  onRowKeyDown: (taskId: string, event: KeyboardEvent<HTMLDivElement>) => void;
}

const TASK_ROW_BASE_INDENT_REM = 0.75;
const TASK_ROW_DEPTH_INDENT_REM = 1.25;

const FALLBACK_TASK_STATE: CanonicalTaskState = {
  owner_session_id: null,
  owner_session_ref: null,
  current_stage: null,
  is_claimed: false,
  is_closed: false,
  is_escalated: false,
  is_blocked: false,
  is_merge_ready: false,
  closed_at: null,
  closed_reason: null,
  closed_in_session_id: null,
  closed_commit_sha: null,
  escalated_at: null,
  escalation_reason: null,
};

function deriveSafeTaskState(task: GobbyTask): {
  taskState: CanonicalTaskState;
  displayState: TaskDisplayState;
  stateSummary: string;
  failed: boolean;
} {
  try {
    return {
      taskState: getCanonicalTaskState(task),
      displayState: getTaskDisplayState(task),
      stateSummary: getTaskStateSummary(task),
      failed: false,
    };
  } catch (error) {
    console.warn("Failed to derive task row state", { taskId: task.id, error });
    return {
      taskState: FALLBACK_TASK_STATE,
      displayState: "ready",
      stateSummary: "Ready",
      failed: true,
    };
  }
}

function TaskTreeRowComponent({
  row,
  isSelected,
  isBusy,
  onSelect,
  onToggleOpen,
  onMenuButtonClick,
  setRowRef,
  tabIndex,
  onRowKeyDown,
}: TaskTreeRowProps) {
  const task = row.node.task;
  const { taskState, displayState, stateSummary, failed } = deriveSafeTaskState(task);
  const currentStage = taskState.current_stage;
  const priority = task.priority ?? DEFAULT_TASK_PRIORITY;
  const textColor =
    PRIORITY_TEXT_COLORS[priority] ?? "var(--text-secondary)";
  const textWeight =
    PRIORITY_TEXT_WEIGHTS[priority] ?? "var(--font-weight-normal)";
  const ref = task.seq_num != null ? `#${task.seq_num}` : null;
  const labelRef = ref ?? task.ref ?? task.id;
  const labelTitle = task.title || "Untitled task";

  return (
    <div
      ref={(node) => setRowRef(task.id, node)}
      style={{
        paddingLeft: `${row.depth * TASK_ROW_DEPTH_INDENT_REM + TASK_ROW_BASE_INDENT_REM}rem`,
      }}
      className={cn(
        "flex min-h-[var(--activity-panel-row-height)] w-full cursor-pointer items-center gap-[0.45rem] py-[0.35rem] pr-[0.35rem] text-left text-[length:var(--text-base)] text-inherit transition-colors hover:bg-[var(--bg-tertiary)] pointer-coarse:min-h-11 pointer-coarse:min-w-11",
        isSelected &&
          "bg-[color-mix(in_srgb,var(--accent)_8%,transparent)]",
        displayState === "closed" && "opacity-[0.72]",
      )}
      role="treeitem"
      tabIndex={tabIndex}
      aria-level={row.depth + 1}
      aria-expanded={row.isInternal ? row.isOpen : undefined}
      aria-selected={isSelected}
      aria-label={`${labelRef} ${labelTitle}: ${stateSummary}`}
      title={stateSummary}
      onClick={() => onSelect(task.id)}
      onKeyDown={(event) => onRowKeyDown(task.id, event)}
    >
      {row.isInternal ? (
        <Button
          variant="ghost"
          size="icon"
          dense
          className={cn(
            coarseHitAreaCls,
            "h-6 min-h-0 w-6 shrink-0 rounded-[0.35rem] p-0 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-primary)] hover:text-[var(--text-primary)] pointer-coarse:size-11 pointer-coarse:min-h-11 pointer-coarse:min-w-11",
          )}
          onClick={(event) => {
            event.stopPropagation();
            onToggleOpen(task.id);
          }}
          aria-label={`${row.isOpen ? "Collapse" : "Expand"} subtasks for ${labelTitle}`}
          title={row.isOpen ? "Collapse subtasks" : "Expand subtasks"}
        >
          <span
            className={cn(
              "flex size-4 transition-transform duration-150 ease-out [&_svg]:size-full",
              row.isOpen && "rotate-90",
            )}
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
        </Button>
      ) : (
        <span className="size-6 shrink-0" aria-hidden="true" />
      )}
      <PriorityGlyph priority={priority} />
      <StatusDot task={failed ? undefined : task} status={displayState} />
      {ref && (
        <span className="shrink-0 text-[length:var(--text-sm)] font-[var(--font-weight-normal)] tracking-normal text-[var(--text-muted)] tabular-nums">
          {ref}
        </span>
      )}
      <span
        className="min-w-0 flex-1 truncate text-[length:var(--text-base)] font-[var(--font-weight-medium)] leading-[1.3] text-[var(--text-primary)]"
        data-task-row-title
        style={{ color: textColor, fontWeight: textWeight }}
      >
        {task.title}
      </span>
      <span className="inline-flex shrink-0 items-center gap-[0.3rem]">
        <TypeBadge type={task.task_type} />
      </span>
      {currentStage && (
        <span
          className="inline-flex h-[1.15rem] min-w-0 max-w-[7.5rem] flex-[0_1_auto] items-center gap-1 rounded-[0.35rem] border border-border bg-[color-mix(in_srgb,var(--bg-secondary)_82%,transparent)] px-[0.35rem] text-[length:var(--text-2xs)] font-medium leading-none text-[var(--text-secondary)]"
          data-task-row-stage
          title={stateSummary}
        >
          <span
            className="size-[0.4rem] shrink-0 rounded-full"
            style={{ backgroundColor: getStageStateColor(currentStage.state) }}
            aria-hidden="true"
          />
          <span className="min-w-0 truncate">
            {currentStage.display_name}
          </span>
        </span>
      )}
      <Button
        type="button"
        variant="ghost"
        size="icon"
        dense
        className={cn(
          "size-7 min-h-7 min-w-7 shrink-0 p-0 text-[var(--text-muted)] transition-colors hover:bg-[var(--bg-tertiary)] hover:text-[var(--text-primary)] pointer-coarse:size-11 pointer-coarse:min-h-11 pointer-coarse:min-w-11",
          coarseHitAreaCls,
        )}
        onClick={(event) => onMenuButtonClick(event, task)}
        title="Task actions"
        aria-label="Task actions"
        disabled={isBusy}
      >
        <KebabIcon />
      </Button>
    </div>
  );
}

export const TaskTreeRow = memo(TaskTreeRowComponent, (previous, next) => {
  return (
    previous.row.node.task === next.row.node.task &&
    previous.row.depth === next.row.depth &&
    previous.row.isInternal === next.row.isInternal &&
    previous.row.isOpen === next.row.isOpen &&
    previous.isSelected === next.isSelected &&
    previous.isBusy === next.isBusy &&
    previous.tabIndex === next.tabIndex &&
    previous.onSelect === next.onSelect &&
    previous.onToggleOpen === next.onToggleOpen &&
    previous.onMenuButtonClick === next.onMenuButtonClick &&
    previous.setRowRef === next.setRowRef &&
    previous.onRowKeyDown === next.onRowKeyDown
  );
});
