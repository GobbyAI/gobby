import { useEffect, useState } from "react";

import type { GobbyTaskDetail } from "../../../hooks/useTasks";
import { relativeTime } from "../../../utils/formatTime";
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

  // Prop-sync via the render-phase "adjust state when a prop changes"
  // pattern (no effect). The effect only re-ticks an active task's label
  // from inside the interval callback.
  const [timeLabel, setTimeLabel] = useState(() =>
    relativeTime(task.updated_at),
  );
  const [syncedAt, setSyncedAt] = useState(task.updated_at);
  if (syncedAt !== task.updated_at) {
    setSyncedAt(task.updated_at);
    setTimeLabel(relativeTime(task.updated_at));
  }
  useEffect(() => {
    if (!isActive) return;
    const interval = window.setInterval(
      () => setTimeLabel(relativeTime(task.updated_at)),
      30000,
    );
    return () => window.clearInterval(interval);
  }, [isActive, task.updated_at]);

  const claimed = taskState.is_claimed && !taskState.is_closed;
  const showRelease = claimed && Boolean(onRelease);
  const showClaim = !claimed && !taskState.is_closed && Boolean(onClaim);

  return (
    <div className="activity-task-detail-statusline">
      {isActive && (
        <span
          className="activity-task-detail-statusline__pulse"
          aria-hidden="true"
        />
      )}
      <span
        className={`activity-task-detail-statusline__stage activity-task-detail-statusline__stage--${stageVariant}`}
      >
        {stageLabel}
      </span>
      <span className="activity-task-detail-statusline__sep" aria-hidden="true">
        ·
      </span>
      {owner.label ? (
        <span className="activity-task-detail-statusline__owner">
          <span
            className={
              owner.mono
                ? "activity-task-detail-statusline__owner-ref activity-task-detail-statusline__owner-ref--mono"
                : "activity-task-detail-statusline__owner-ref"
            }
          >
            {owner.label}
          </span>
          {owner.source && (
            <span className="activity-task-detail-statusline__owner-source">
              {owner.source}
            </span>
          )}
        </span>
      ) : (
        <span className="activity-task-detail-statusline__owner activity-task-detail-statusline__owner--unassigned">
          Unassigned
        </span>
      )}
      <span className="activity-task-detail-statusline__time">{timeLabel}</span>
      {(showClaim || showRelease) && (
        <button
          type="button"
          className="btn btn-sm activity-task-detail-statusline__action"
          disabled={claimBusy}
          onClick={showRelease ? onRelease : onClaim}
        >
          {showRelease ? "Release" : "Claim"}
        </button>
      )}
    </div>
  );
}
