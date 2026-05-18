import { useEffect, useRef, useState } from "react";
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
  const currentTaskId = task?.id ?? null;
  const isClosed = Boolean(task?.state?.is_closed) || task?.status === "closed";
  const [draft, setDraft] = useState({
    taskId: null as string | null,
    isClosed: false,
    reason: "",
    showReasonError: false,
  });
  if (draft.taskId !== currentTaskId || draft.isClosed !== isClosed) {
    setDraft({ taskId: currentTaskId, isClosed, reason: "", showReasonError: false });
  }
  const matchesCurrentTask = draft.taskId === currentTaskId && draft.isClosed === isClosed;
  const reason = matchesCurrentTask ? draft.reason : "";
  const showReasonError = matchesCurrentTask && draft.showReasonError;
  const dialogRef = useRef<HTMLFormElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const hasTask = task !== null;

  useEffect(() => {
    if (!hasTask) return;
    previousFocusRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !isSubmitting) {
        event.preventDefault();
        onCancel();
        return;
      }
      if (event.key !== "Tab") return;
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusable = Array.from(
        dialog.querySelectorAll<HTMLElement>(
          'button:not(:disabled), textarea:not(:disabled), [href], [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => !element.hasAttribute("disabled"));
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      if (previousFocusRef.current && document.contains(previousFocusRef.current)) {
        previousFocusRef.current.focus();
      }
      previousFocusRef.current = null;
    };
  }, [hasTask, isSubmitting, onCancel, task?.id]);

  if (!task) return null;

  const submit = () => {
    const trimmed = reason.trim();
    if (!trimmed) {
      setDraft({ taskId: currentTaskId, isClosed, reason, showReasonError: true });
      return;
    }
    onConfirm(trimmed);
  };

  return (
    <div
      className="activity-task-close-backdrop"
      role="presentation"
      onClick={(event) => {
        if (event.target === event.currentTarget && !isSubmitting) onCancel();
      }}
    >
      <form
        ref={dialogRef}
        className="activity-task-close-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="activity-task-close-title"
        aria-describedby={showReasonError ? "activity-task-close-error" : undefined}
        onClick={(event) => event.stopPropagation()}
        onSubmit={(event) => {
          event.preventDefault();
          event.stopPropagation();
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
            setDraft({
              taskId: currentTaskId,
              isClosed,
              reason: event.currentTarget.value,
              showReasonError: false,
            });
          }}
          autoFocus
        />
        {showReasonError && (
          <div id="activity-task-close-error" className="activity-task-close-error" role="alert">
            Reason is required.
          </div>
        )}
        <div className="activity-task-close-actions">
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={onCancel}
            disabled={isSubmitting}
          >
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
