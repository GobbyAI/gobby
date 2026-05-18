import type { GobbyTask, GobbyTaskDetail } from "../../../hooks/useTasks";
import {
  getCanonicalTaskState,
  getTaskDisplayState,
  TASK_STATE_LABELS,
  type CanonicalTaskState,
  type TaskDisplayState,
} from "../../../lib/taskState";
import type { PatchEditableField } from "../taskFieldRouting";
import type { PatchFieldValue } from "../useTaskInlineEdit";

/**
 * D5 — pure formatting helpers for the redesigned task detail pane.
 *
 * No JSX, no hooks: every function here is deterministic and unit-tested so
 * the section components stay declarative. The owner helpers enforce the
 * one-rule of this redesign — a session is shown as a friendly `#<ref>`,
 * exactly once, and the raw `claimed_by_session_id` UUID is never rendered.
 */

export type StageVariant =
  | "active"
  | "blocked"
  | "closed"
  | "escalated"
  | "default";

export function formatTaskDetailDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) return "—";
  return `${parsed.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  })} ${parsed.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  })}`;
}

export function stageVariant(
  isEscalated: boolean,
  displayState: TaskDisplayState,
): StageVariant {
  if (isEscalated) return "escalated";
  if (displayState === "blocked") return "blocked";
  if (displayState === "closed") return "closed";
  if (displayState === "in_progress" || displayState === "review_approved") {
    return "active";
  }
  return "default";
}

export function stageLabel(
  task: GobbyTaskDetail,
  taskState: CanonicalTaskState,
  displayState: TaskDisplayState,
  isEscalated: boolean,
): string {
  if (isEscalated) return "Escalated";
  if (taskState.is_closed) return "Closed";
  if (taskState.current_stage) return taskState.current_stage.display_name;
  if (task.expansion_status === "in_progress") return "Expanding";
  return TASK_STATE_LABELS[displayState];
}

export interface OwnerDisplay {
  /** Friendly label — agent name or `#<ref>`. `null` => render "Unassigned". */
  label: string | null;
  /** Render in mono (a session ref) rather than a human agent name. */
  mono: boolean;
  /** Originating CLI/source for the owning session, when known. */
  source: string | null;
}

/**
 * Resolve the single owner label for a task. Precedence: a human-readable
 * agent name, then the backend-resolved friendly session ref, then a short
 * hash of the owner id (resilience for older payloads) — never the full UUID.
 */
export function ownerDisplay(task: GobbyTask): OwnerDisplay {
  const agentName = task.agent_name?.trim() || null;
  if (agentName) {
    return { label: agentName, mono: false, source: null };
  }
  const state = getCanonicalTaskState(task);
  const ref = state.owner_session_ref;
  if (ref?.ref) {
    return { label: ref.ref, mono: true, source: ref.source };
  }
  const ownerId = state.owner_session_id?.trim() || null;
  if (ownerId) {
    return { label: ownerId.slice(0, 8), mono: true, source: null };
  }
  return { label: null, mono: false, source: null };
}

export interface TaskDetailComputed {
  taskState: CanonicalTaskState;
  displayState: TaskDisplayState;
  isEscalated: boolean;
  stageVariant: StageVariant;
  stageLabel: string;
  owner: OwnerDisplay;
}

/** One-shot derivation shared by the header and status line. */
export function computeTaskDetail(task: GobbyTaskDetail): TaskDetailComputed {
  const taskState = getCanonicalTaskState(task);
  const displayState = getTaskDisplayState(task);
  const isEscalated = Boolean(task.escalated_at) && !taskState.is_closed;
  return {
    taskState,
    displayState,
    isEscalated,
    stageVariant: stageVariant(isEscalated, displayState),
    stageLabel: stageLabel(task, taskState, displayState, isEscalated),
    owner: ownerDisplay(task),
  };
}

/**
 * The subset of {@link useTaskInlineEdit} the detail sections depend on.
 * Structural by design so tests can inject a trivial stub and the panel
 * never imports the hook just for a type.
 */
export interface TaskInlineEditApi {
  commitField: (args: {
    task: GobbyTask;
    field: PatchEditableField;
    value: PatchFieldValue;
  }) => Promise<void> | void;
  isFieldPending: (taskId: string, field: string) => boolean;
  errorFor: (taskId: string) => string | null;
  clearError: (taskId: string) => void;
}
