import { useState } from "react";
import type { GobbyTask } from "../../types/tasks";
import { Button } from "../ui/Button";
import { coarseHitAreaCls } from "../ui/controlStyles";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "../ui/Dialog";
import { FormField } from "../ui/FormField";
import { Textarea } from "../ui/Textarea";

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
  const [draft, setDraft] = useState(() => ({
    taskId: currentTaskId,
    isClosed,
    reason: "",
    showReasonError: false,
  }));

  const matchesCurrentTask = draft.taskId === currentTaskId && draft.isClosed === isClosed;
  const reason = matchesCurrentTask ? draft.reason : "";
  const showReasonError = matchesCurrentTask && draft.showReasonError;

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
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !isSubmitting) {
          onCancel();
        }
      }}
    >
      <DialogContent
        className="activity-task-close-dialog"
        {...(showReasonError
          ? { "aria-describedby": "activity-task-close-error" }
          : {})}
      >
        <DialogTitle>Close task</DialogTitle>
        <DialogDescription>{task.ref ?? task.id}</DialogDescription>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            event.stopPropagation();
            submit();
          }}
        >
          <FormField label="Reason">
            {({ id }) => (
              <>
                <Textarea
                  id={id}
                  className="activity-task-close-reason"
                  value={reason}
                  error={showReasonError}
                  aria-describedby={
                    showReasonError ? "activity-task-close-error" : undefined
                  }
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
                  <div
                    id="activity-task-close-error"
                    className="activity-task-close-error"
                    role="alert"
                  >
                    Reason is required.
                  </div>
                )}
              </>
            )}
          </FormField>
          <div className="activity-task-close-actions">
            <Button
              type="button"
              size="sm"
              className={coarseHitAreaCls}
              onClick={onCancel}
              disabled={isSubmitting}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              size="sm"
              className={`${coarseHitAreaCls} activity-task-close-submit`}
              disabled={isSubmitting}
            >
              Close
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
