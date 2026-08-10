import { useEffect, useState } from "react";

import type { GobbyTaskDetail } from "../../../types/tasks";
import { Button } from "../../ui/Button";
import { relativeTime } from "../../../utils/formatTime";
import { cn } from "../../../lib/utils";
import type { TaskDetailComputed } from "./taskDetailFormat";

/**
 * D5 §2 — one status line: stage · owner · claim. Absorbs the old
 * `TaskStatusStrip` (the separate strip block is gone). The owner is shown
 * exactly once across the whole pane, as a friendly ref, never a raw UUID.
 * Owner/claim are action controls; state/stage is read-only here (the header
 * chips own state truth, the stage PATCH route owns stage changes).
 */
export function TaskDetailStatusLine({
  task,
  computed,
  onClaim,
  onRelease,
  claimBusy = false,
}: {
  task: GobbyTaskDetail;
  computed: TaskDetailComputed;
  onClaim?: () => void;
  onRelease?: () => void;
  claimBusy?: boolean;
}) {
  const { taskState, displayState, stageLabel, stageVariant, owner } = computed;
  const isActive =
    !taskState.is_closed &&
    (taskState.is_claimed || displayState === "in_progress");

  const [timeSnapshot, setTimeSnapshot] = useState(() => ({
    updatedAt: task.updated_at,
    label: relativeTime(task.updated_at),
  }));
  const timeLabel =
    timeSnapshot.updatedAt === task.updated_at
      ? timeSnapshot.label
      : relativeTime(task.updated_at);

  useEffect(() => {
    if (!isActive) return;
    const interval = window.setInterval(
      () =>
        setTimeSnapshot({
          updatedAt: task.updated_at,
          label: relativeTime(task.updated_at),
        }),
      30000,
    );
    return () => window.clearInterval(interval);
  }, [isActive, task.updated_at]);

  const claimed = taskState.is_claimed && !taskState.is_closed;
  const showRelease = claimed && Boolean(onRelease);
  const showClaim = !claimed && !taskState.is_closed && Boolean(onClaim);

  return (
    <div className="flex flex-wrap items-center gap-[0.45rem] border-b border-border bg-[var(--bg-primary)] px-4 py-[0.55rem] text-[length:var(--text-sm)] text-[var(--text-muted)]">
      {isActive && (
        <span
          className="size-[0.45rem] shrink-0 rounded-full bg-accent"
          aria-hidden="true"
        />
      )}
      <span
        className={cn(
          "font-[var(--font-weight-medium)] text-[var(--text-primary)]",
          stageVariant === "active" && "text-accent",
          (stageVariant === "blocked" || stageVariant === "escalated") &&
            "text-[var(--color-error)]",
          stageVariant === "closed" && "text-[var(--color-inactive)]",
        )}
      >
        {stageLabel}
      </span>
      <span className="text-[var(--text-muted)]" aria-hidden="true">
        ·
      </span>
      {owner.label ? (
        <span
          className="inline-flex min-w-0 items-baseline gap-[0.35rem]"
          data-task-owner
        >
          <span
            className={cn(
              "text-[var(--text-secondary)]",
              owner.mono && "font-mono",
            )}
            data-task-owner-ref
          >
            {owner.label}
          </span>
          {owner.source && (
            <span className="text-[length:var(--text-xs)] text-[var(--text-muted)]">
              {owner.source}
            </span>
          )}
        </span>
      ) : (
        <span
          className="inline-flex min-w-0 items-baseline gap-[0.35rem] italic text-[var(--text-muted)]"
          data-task-owner
        >
          Unassigned
        </span>
      )}
      <span className="text-[length:var(--text-xs)] text-[var(--text-muted)]">
        {timeLabel}
      </span>
      {(showClaim || showRelease) && (
        <Button
          type="button"
          variant="accent"
          size="sm"
          className="ml-auto shrink-0"
          disabled={claimBusy}
          onClick={showRelease ? onRelease : onClaim}
        >
          {showRelease ? "Release" : "Claim"}
        </Button>
      )}
    </div>
  );
}
