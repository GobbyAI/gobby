import { useEffect, useState } from "react";
import type { GobbyTask } from "../../hooks/useTasks";

interface TaskCloseDialogProps {
  task: GobbyTask | null;
  isSubmitting: boolean;
  onCancel: () => void;
  onConfirm: (reason: string) => void;
}

export function TaskCloseDialog({
  task,
  isSubmitting,
  onCancel,
  onConfirm,
}: TaskCloseDialogProps) {
  const [reason, setReason] = useState("");
  const [showReasonError, setShowReasonError] = useState(false);

  useEffect(() => {
    setReason("");
    setShowReasonError(false);
  }, [task?.id]);

  if (!task) return null;

  const submit = () => {
    const trimmed = reason.trim();
    if (!trimmed) {
      setShowReasonError(true);
      return;
    }
    onConfirm(trimmed);
  };

  return (
    <div className="activity-task-close-backdrop" role="presentation">
      <form
        className="activity-task-close-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="activity-task-close-title"
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <h2 id="activity-task-close-title">Close task</h2>
        <p>{task.ref ?? task.id}</p>
        <label htmlFor="activity-task-close-reason">Reason</label>
        <textarea
          id="activity-task-close-reason"
          className="activity-task-close-reason"
          value={reason}
          onChange={(event) => {
            setReason(event.currentTarget.value);
            if (showReasonError) setShowReasonError(false);
          }}
          autoFocus
        />
        {showReasonError && (
          <div className="activity-task-close-error" role="alert">
            Reason is required.
          </div>
        )}
        <div className="activity-task-close-actions">
          <button type="button" className="btn btn-secondary btn-sm" onClick={onCancel}>
            Cancel
          </button>
          <button
            type="submit"
            className="btn btn-secondary btn-sm activity-task-close-submit"
            disabled={isSubmitting}
          >
            Close
          </button>
        </div>
      </form>
    </div>
  );
}
