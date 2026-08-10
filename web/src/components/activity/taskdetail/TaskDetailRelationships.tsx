import { useMemo } from "react";

import type { DependencyTree, GobbyTask } from "../../../types/tasks";
import { Button } from "../../ui/Button";
import { coarseHitAreaCls } from "../../ui/controlStyles";
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
    <div className="flex min-w-0 flex-col gap-[0.2rem]">
      {nodes.map((node) => {
        const { ref, title } = depLabel(node);
        const select = onSelectTask
          ? () => onSelectTask(node.id)
          : undefined;
        return select ? (
          <Button
            key={node.id}
            type="button"
            variant="ghost"
            size="sm"
            className={`inline-flex max-w-full min-w-0 items-baseline gap-[0.4rem] rounded p-0 text-left text-[var(--text-secondary)] transition-colors hover:text-[var(--text-primary)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent ${coarseHitAreaCls}`}
            onClick={select}
            title={title}
          >
            <span className="shrink-0 font-mono text-[length:var(--text-sm)] font-semibold text-accent">
              {ref}
            </span>
            <span className="whitespace-normal">{title}</span>
          </Button>
        ) : (
          <span
            key={node.id}
            className="inline-flex max-w-full min-w-0 items-baseline gap-[0.4rem]"
            title={title}
          >
            <span className="shrink-0 font-mono text-[length:var(--text-sm)] font-semibold text-accent">
              {ref}
            </span>{" "}
            <span className="whitespace-normal">{title}</span>
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
    <section className="flex flex-col border-b border-border bg-[var(--bg-primary)] px-4 pb-[0.95rem] pt-[0.4rem]">
      <h3 className="mb-[0.35rem] mt-[0.55rem] text-[length:var(--text-2xs)] font-[var(--font-weight-semibold)] uppercase tracking-[0.08em] text-[var(--text-muted)]">
        Relationships
      </h3>
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
          <div className="flex flex-wrap gap-[0.4rem]">
            {SUBTASK_STATE_ORDER.map((s) => {
              const count = subtaskBuckets[s] ?? 0;
              if (count === 0) return null;
              return (
                <span
                  key={s}
                  className="inline-flex h-6 items-center gap-[0.3rem] whitespace-nowrap rounded-full border border-border bg-[var(--bg-tertiary)] px-[0.55rem] text-[length:var(--text-2xs)] font-medium tracking-[0.02em] text-[var(--text-secondary)] [&_strong]:font-semibold [&_strong]:text-[var(--text-primary)]"
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
