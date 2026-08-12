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

  const matchesCurrentTask =
    draft.taskId === currentTaskId && draft.isClosed === isClosed;
  const reason = matchesCurrentTask ? draft.reason : "";
  const showReasonError = matchesCurrentTask && draft.showReasonError;

  if (!task) return null;

  const submit = () => {
    const trimmed = reason.trim();
    if (!trimmed) {
      setDraft({
        taskId: currentTaskId,
        isClosed,
        reason,
        showReasonError: true,
      });
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
        className="w-[min(24rem,100%)] border-border bg-[var(--bg-secondary)] p-4 text-[var(--text-primary)] shadow-lg [&_h2]:m-0 [&_h2]:text-[length:var(--text-lg)] [&_h2]:font-semibold [&_label]:mb-[0.35rem] [&_label]:block [&_label]:text-[length:var(--text-xs)] [&_label]:font-semibold [&_label]:text-[var(--text-secondary)] [&_p]:mt-1 [&_p]:mb-3 [&_p]:text-[length:var(--text-sm)] [&_p]:text-[var(--text-secondary)]"
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
                  className="min-h-[5.5rem] resize-y bg-[var(--bg-primary)] p-2 text-[length:var(--text-sm)] text-[var(--text-primary)] focus:border-accent focus:outline-none"
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
                    className="mt-[0.4rem] text-[length:var(--text-xs)] text-[var(--color-error)]"
                    role="alert"
                  >
                    Reason is required.
                  </div>
                )}
              </>
            )}
          </FormField>
          <div className="mt-3 flex justify-end gap-2">
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
              className={`${coarseHitAreaCls} text-[var(--color-error)]`}
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
