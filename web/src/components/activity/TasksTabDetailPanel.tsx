import { Markdown } from "../chat/Markdown";
import type { GobbyTask } from "../../hooks/useTasks";
import {
  getCanonicalTaskState,
  getTaskBucket,
  TASK_BUCKET_LABELS,
} from "../../lib/taskState";

export interface GobbyTaskDetail extends GobbyTask {
  description: string | null;
  category: string | null;
  validation_criteria: string | null;
  closed_at: string | null;
}

interface TasksTabDetailPanelProps {
  task: GobbyTaskDetail;
}

function formatTaskDetailDate(iso: string | null | undefined): string {
  if (!iso) {
    return "—";
  }

  const parsed = new Date(iso);
  if (Number.isNaN(parsed.getTime())) {
    return "—";
  }

  return `${parsed.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  })} ${parsed.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
  })}`;
}

export function TasksTabDetailPanel({ task }: TasksTabDetailPanelProps) {
  const taskState = getCanonicalTaskState(task);
  const ownerLabel = task.agent_name ?? taskState.owner_session_id ?? "Unassigned";
  const ownerMono = !task.agent_name && Boolean(taskState.owner_session_id);
  const stateLabel = TASK_BUCKET_LABELS[getTaskBucket(task)];
  const categoryLabel = task.category ?? task.task_type;

  return (
    <div className="activity-task-detail-card">
      <div className="activity-task-detail-meta">
        <TaskDetailMetaRow
          label="Claimed by"
          value={ownerLabel}
          mono={ownerMono}
          title="Agent or session currently holding this task's claim"
        />
        <TaskDetailMetaRow label="State" value={stateLabel} />
        <TaskDetailMetaRow label="Created" value={formatTaskDetailDate(task.created_at)} />
        <TaskDetailMetaRow label="Updated" value={formatTaskDetailDate(task.updated_at)} />
        <TaskDetailMetaRow label="Category" value={categoryLabel} />
        {task.path_cache && <TaskDetailMetaRow label="Path" value={task.path_cache} mono />}
        {task.closed_at && (
          <TaskDetailMetaRow
            label="Closed"
            value={formatTaskDetailDate(task.closed_at)}
          />
        )}
      </div>

      {task.description && (
        <div className="activity-task-detail-section">
          <div className="activity-task-detail-section-title">Description</div>
          <div className="activity-task-detail-markdown message-content">
            <Markdown content={task.description} id={`task-desc-${task.id}`} />
          </div>
        </div>
      )}

      {task.validation_criteria && (
        <div className="activity-task-detail-section">
          <div className="activity-task-detail-section-title">Validation</div>
          <div className="activity-task-detail-markdown message-content">
            <Markdown
              content={task.validation_criteria}
              id={`task-vc-${task.id}`}
            />
          </div>
        </div>
      )}
    </div>
  );
}

function TaskDetailMetaRow({
  label,
  value,
  mono = false,
  title,
}: {
  label: string;
  value: string;
  mono?: boolean;
  title?: string;
}) {
  return (
    <div className="activity-task-detail-meta-row" title={title}>
      <span className="activity-task-detail-meta-label">{label}</span>
      <span
        className={`activity-task-detail-meta-value${
          mono ? " activity-task-detail-meta-value--mono" : ""
        }`}
      >
        {value}
      </span>
    </div>
  );
}
