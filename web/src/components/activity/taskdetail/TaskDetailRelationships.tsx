import { useMemo } from "react";

import type { DependencyTree, GobbyTask } from "../../../hooks/useTasks";
import {
  getTaskDisplayState,
  TASK_STATE_LABELS,
  type TaskDisplayState,
} from "../../../lib/taskState";
import { MetaKVRow, ParentKVRow, type ParentTaskRef } from "./TaskDetailKV";

const SUBTASK_STATE_ORDER: TaskDisplayState[] = [
  "ready",
  "in_progress",
  "needs_review",
  "review_approved",
  "blocked",
  "closed",
];

function depLabel(node: DependencyTree): { ref: string; title: string } {
  return {
    ref: node.ref ?? `#${node.id.slice(0, 8)}`,
    title: node.title ?? "(unresolved task)",
  };
}

function DepList({
  nodes,
  onSelectTask,
}: {
  nodes: DependencyTree[];
  onSelectTask?: (id: string) => void;
}) {
  return (
    <div className="activity-task-detail-deplist">
      {nodes.map((node) => {
        const { ref, title } = depLabel(node);
        const select = onSelectTask
          ? () => onSelectTask(node.id)
          : undefined;
        return select ? (
          <button
            key={node.id}
            type="button"
            className="activity-task-detail-parent-link activity-task-detail-deplist__item"
            onClick={select}
            title={title}
          >
            <span className="activity-task-detail-parent-ref">{ref}</span>
            <span className="activity-task-detail-parent-title">{title}</span>
          </button>
        ) : (
          <span
            key={node.id}
            className="activity-task-detail-deplist__item"
            title={title}
          >
            <span className="activity-task-detail-parent-ref">{ref}</span>{" "}
            <span className="activity-task-detail-parent-title">{title}</span>
          </span>
        );
      })}
    </div>
  );
}

/**
 * D5 §4 — relationships. Parent and dependencies show the actual tasks
 * (clickable ref + title), not bare counts. Subtasks stay a compact
 * by-state roll-up (they can be numerous; the tree is the place to drill in).
 */
export function TaskDetailRelationships({
  parentTask,
  onSelectTask,
  dependencies,
  subtasks,
}: {
  parentTask?: ParentTaskRef | null;
  onSelectTask?: (id: string) => void;
  dependencies?: DependencyTree | null;
  subtasks?: GobbyTask[];
}) {
  const blockers = dependencies?.blockers ?? [];
  const blocking = dependencies?.blocking ?? [];

  const { subtaskBuckets, subtaskTotal } = useMemo(() => {
    const states: Partial<Record<TaskDisplayState, number>> = {};
    for (const child of subtasks ?? []) {
      const childState = getTaskDisplayState(child);
      states[childState] = (states[childState] ?? 0) + 1;
    }
    return { subtaskBuckets: states, subtaskTotal: subtasks?.length ?? 0 };
  }, [subtasks]);

  const hasRelationships =
    Boolean(parentTask) ||
    blockers.length > 0 ||
    blocking.length > 0 ||
    subtaskTotal > 0;
  if (!hasRelationships) return null;

  return (
    <section className="activity-task-detail-kv">
      <h3 className="activity-task-detail-kv__title">Relationships</h3>
      {parentTask && (
        <ParentKVRow parent={parentTask} onSelect={onSelectTask} />
      )}
      {blockers.length > 0 && (
        <MetaKVRow label={`Blocked by (${blockers.length})`}>
          <DepList nodes={blockers} onSelectTask={onSelectTask} />
        </MetaKVRow>
      )}
      {blocking.length > 0 && (
        <MetaKVRow label={`Blocks (${blocking.length})`}>
          <DepList nodes={blocking} onSelectTask={onSelectTask} />
        </MetaKVRow>
      )}
      {subtaskTotal > 0 && (
        <MetaKVRow label={`Subtasks (${subtaskTotal})`}>
          <div className="activity-task-detail-pillrow">
            {SUBTASK_STATE_ORDER.map((s) => {
              const count = subtaskBuckets[s] ?? 0;
              if (count === 0) return null;
              return (
                <span
                  key={s}
                  className="activity-task-detail-pill"
                  title={`${TASK_STATE_LABELS[s]} subtasks`}
                >
                  <strong>{count}</strong> {TASK_STATE_LABELS[s].toLowerCase()}
                </span>
              );
            })}
          </div>
        </MetaKVRow>
      )}
    </section>
  );
}
