import { Markdown } from "../chat/Markdown";
import type { DependencyTree, GobbyTask } from "../../hooks/useTasks";
import {
  getCanonicalTaskState,
  getTaskBucket,
  TASK_BUCKET_LABELS,
  type TaskBucket,
} from "../../lib/taskState";

export interface GobbyTaskDetail extends GobbyTask {
  description: string | null;
  category: string | null;
  validation_criteria: string | null;
  closed_at: string | null;
  assigned_agent?: string | null;
  labels?: string[] | null;
  commits?: string[] | null;
}

export interface ParentTaskRef {
  id: string;
  ref: string;
  title: string;
}

interface TasksTabDetailPanelProps {
  task: GobbyTaskDetail;
  parentTask?: ParentTaskRef | null;
  onSelectTask?: (id: string) => void;
  dependencies?: DependencyTree | null;
  subtasks?: GobbyTask[];
}

const SUBTASK_BUCKET_ORDER: TaskBucket[] = [
  "ready",
  "in_progress",
  "review",
  "merge_ready",
  "blocked",
  "closed",
];

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

export function TasksTabDetailPanel({
  task,
  parentTask,
  onSelectTask,
  dependencies,
  subtasks,
}: TasksTabDetailPanelProps) {
  const taskState = getCanonicalTaskState(task);
  const ownerLabel = task.agent_name ?? taskState.owner_session_id ?? "Unassigned";
  const ownerMono = !task.agent_name && Boolean(taskState.owner_session_id);
  const stateLabel = TASK_BUCKET_LABELS[getTaskBucket(task)];
  const categoryLabel = task.category ?? task.task_type;
  const labels = task.labels?.filter(Boolean) ?? [];
  const blockerCount = dependencies?.blockers?.length ?? 0;
  const blockingCount = dependencies?.blocking?.length ?? 0;
  const commits = task.commits?.filter(Boolean) ?? [];

  const subtaskBuckets: Partial<Record<TaskBucket, number>> = {};
  for (const child of subtasks ?? []) {
    const bucket = getTaskBucket(child);
    subtaskBuckets[bucket] = (subtaskBuckets[bucket] ?? 0) + 1;
  }
  const subtaskTotal = subtasks?.length ?? 0;

  return (
    <div className="activity-task-detail-card">
      <div className="activity-task-detail-meta">
        <TaskDetailMetaRow
          label="Claimed by"
          value={ownerLabel}
          mono={ownerMono}
          title="Agent or session currently holding this task's claim"
        />
        {task.assigned_agent && (
          <TaskDetailMetaRow
            label="Agent"
            value={task.assigned_agent}
            mono
            title="Agent role assigned to drive this task"
          />
        )}
        <TaskDetailMetaRow label="State" value={stateLabel} />
        <TaskDetailMetaRow label="Created" value={formatTaskDetailDate(task.created_at)} />
        <TaskDetailMetaRow label="Updated" value={formatTaskDetailDate(task.updated_at)} />
        <TaskDetailMetaRow label="Category" value={categoryLabel} />
        {parentTask && (
          <TaskDetailParentRow
            parent={parentTask}
            onSelect={onSelectTask}
          />
        )}
        {task.path_cache && <TaskDetailMetaRow label="Path" value={task.path_cache} mono />}
        {task.closed_at && (
          <TaskDetailMetaRow
            label="Closed"
            value={formatTaskDetailDate(task.closed_at)}
          />
        )}
      </div>

      {labels.length > 0 && (
        <div className="activity-task-detail-labels">
          {labels.map((label) => (
            <span key={label} className="activity-task-detail-label">
              {label}
            </span>
          ))}
        </div>
      )}

      {(blockerCount > 0 || blockingCount > 0) && (
        <div className="activity-task-detail-section">
          <div className="activity-task-detail-section-title">Dependencies</div>
          <div className="activity-task-detail-pillrow">
            {blockerCount > 0 && (
              <span
                className="activity-task-detail-pill activity-task-detail-pill--blocked"
                title="Tasks this task depends on"
              >
                Blocked by <strong>{blockerCount}</strong>
              </span>
            )}
            {blockingCount > 0 && (
              <span
                className="activity-task-detail-pill"
                title="Tasks waiting on this task"
              >
                Blocks <strong>{blockingCount}</strong>
              </span>
            )}
          </div>
        </div>
      )}

      {subtaskTotal > 0 && (
        <div className="activity-task-detail-section">
          <div className="activity-task-detail-section-title">
            Subtasks <span className="activity-task-detail-section-count">{subtaskTotal}</span>
          </div>
          <div className="activity-task-detail-pillrow">
            {SUBTASK_BUCKET_ORDER.map((bucket) => {
              const count = subtaskBuckets[bucket] ?? 0;
              if (count === 0) return null;
              return (
                <span
                  key={bucket}
                  className="activity-task-detail-pill"
                  title={`${TASK_BUCKET_LABELS[bucket]} subtasks`}
                >
                  <strong>{count}</strong> {TASK_BUCKET_LABELS[bucket].toLowerCase()}
                </span>
              );
            })}
          </div>
        </div>
      )}

      {commits.length > 0 && (
        <div className="activity-task-detail-section">
          <div className="activity-task-detail-section-title">
            Commits
            {commits.length > 3 && (
              <span className="activity-task-detail-section-count">{commits.length}</span>
            )}
          </div>
          <div className="activity-task-detail-pillrow">
            {commits.slice(0, 3).map((sha) => (
              <span
                key={sha}
                className="activity-task-detail-pill activity-task-detail-pill--mono"
                title={sha}
              >
                {sha.slice(0, 7)}
              </span>
            ))}
            {commits.length > 3 && (
              <span className="activity-task-detail-pill activity-task-detail-pill--mono activity-task-detail-pill--more">
                +{commits.length - 3}
              </span>
            )}
          </div>
        </div>
      )}

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

function TaskDetailParentRow({
  parent,
  onSelect,
}: {
  parent: ParentTaskRef;
  onSelect?: (id: string) => void;
}) {
  const handleClick = onSelect ? () => onSelect(parent.id) : undefined;
  return (
    <div className="activity-task-detail-meta-row" title="Parent task">
      <span className="activity-task-detail-meta-label">Parent</span>
      <span className="activity-task-detail-meta-value">
        {handleClick ? (
          <button
            type="button"
            className="activity-task-detail-parent-link"
            onClick={handleClick}
          >
            <span className="activity-task-detail-parent-ref">{parent.ref}</span>
            <span className="activity-task-detail-parent-title">{parent.title}</span>
          </button>
        ) : (
          <>
            <span className="activity-task-detail-parent-ref">{parent.ref}</span>
            {" "}
            <span className="activity-task-detail-parent-title">{parent.title}</span>
          </>
        )}
      </span>
    </div>
  );
}
