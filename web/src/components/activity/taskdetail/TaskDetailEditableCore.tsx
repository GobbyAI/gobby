import { useState } from "react";
import type { GobbyTaskDetail } from "../../../types/tasks";
import {
  TASK_CATEGORY_OPTIONS,
  DEFAULT_TASK_PRIORITY,
  TASK_PRIORITY_OPTIONS,
} from "../../../lib/taskOptions";
import {
  TaskSelectField,
  TaskTagsField,
  TaskTextAreaField,
} from "../TaskFieldEditors";
import type { TaskInlineEditApi } from "./taskDetailFormat";
import { markdownBodyClassName } from "../../shared/MarkdownBody";

/**
 * D5 §3 — the editable core. PATCH-family fields only (category, priority,
 * labels, description, validation criteria); every commit routes through the
 * D4 inline-edit hook. State / owner / stage are deliberately absent — they
 * are action controls elsewhere, never free-text PATCH inputs here.
 */

const PRIORITY_SELECT_OPTIONS = TASK_PRIORITY_OPTIONS.map(option => ({
  value: String(option.value),
  label: option.label,
}));

function StaticBlock({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-[0.3rem]">
      <div className="text-[length:var(--text-sm)] font-[var(--font-weight-medium)] text-[var(--text-muted)]">
        {label}
      </div>
      <div
        className={`message-content text-[length:var(--text-base)] leading-[1.6] text-[var(--text-secondary)] ${markdownBodyClassName}`}
      >
        {value}
      </div>
    </div>
  );
}

export function TaskDetailEditableCore({
  task,
  edit,
}: {
  task: GobbyTaskDetail;
  edit?: TaskInlineEditApi;
}) {
  const labels = task.labels?.filter(Boolean) ?? [];
  const description = task.description ?? "";
  const validationCriteria = task.validation_criteria ?? "";
  const [priorityError, setPriorityError] = useState<string | null>(null);

  if (!edit) {
    const hasBody = Boolean(description) || Boolean(validationCriteria);
    if (!hasBody) return null;
    return (
      <section
        className="flex flex-col gap-[0.7rem] border-b border-border bg-[var(--bg-primary)] px-4 pb-4 pt-[0.55rem]"
        data-task-detail-core
      >
        <h3 className="mb-[0.35rem] mt-[0.55rem] text-[length:var(--text-2xs)] font-[var(--font-weight-semibold)] uppercase tracking-[0.08em] text-[var(--text-muted)]">
          Details
        </h3>
        {labels.length > 0 && (
          <div className="grid grid-cols-[minmax(0,6.5rem)_minmax(0,1fr)] items-center gap-[0.85rem]">
            <span className="text-[length:var(--text-sm)] font-[var(--font-weight-medium)] text-[var(--text-muted)]">
              Labels
            </span>
            <div className="flex flex-wrap gap-[0.4rem]">
              {labels.map((label) => (
                <span
                  key={label}
                  className="inline-flex h-5 items-center whitespace-nowrap rounded-full border border-border bg-[var(--bg-tertiary)] px-2 font-mono text-[length:var(--text-2xs)] font-medium tracking-[0.02em] text-[var(--text-secondary)]"
                >
                  {label}
                </span>
              ))}
            </div>
          </div>
        )}
        {description && (
          <StaticBlock label="Description" value={description} />
        )}
        {validationCriteria && (
          <StaticBlock label="Validation criteria" value={validationCriteria} />
        )}
      </section>
    );
  }

  return (
    <section
      className="flex flex-col gap-[0.7rem] border-b border-border bg-[var(--bg-primary)] px-4 pb-4 pt-[0.55rem]"
      data-task-detail-core
    >
      <h3 className="mb-[0.35rem] mt-[0.55rem] text-[length:var(--text-2xs)] font-[var(--font-weight-semibold)] uppercase tracking-[0.08em] text-[var(--text-muted)]">
        Details
      </h3>

      <div className="grid grid-cols-[minmax(0,6.5rem)_minmax(0,1fr)] items-center gap-[0.85rem]">
        <span className="text-[length:var(--text-sm)] font-[var(--font-weight-medium)] text-[var(--text-muted)]">
          Category
        </span>
        <TaskSelectField
          value={task.category ?? ""}
          ariaLabel="Category"
          options={[...TASK_CATEGORY_OPTIONS]}
          disabled={edit.isFieldPending(task.id, "category")}
          onCommit={(value) =>
            edit.commitField({ task, field: "category", value })
          }
        />
      </div>

      <div className="grid grid-cols-[minmax(0,6.5rem)_minmax(0,1fr)] items-center gap-[0.85rem]">
        <span className="text-[length:var(--text-sm)] font-[var(--font-weight-medium)] text-[var(--text-muted)]">
          Priority
        </span>
        <TaskSelectField
          value={String(task.priority ?? DEFAULT_TASK_PRIORITY)}
          ariaLabel="Priority"
          options={PRIORITY_SELECT_OPTIONS}
          disabled={edit.isFieldPending(task.id, "priority")}
          onCommit={(value) => {
            const priority = Number(value);
            if (!value.trim() || !Number.isInteger(priority)) {
              setPriorityError(`Invalid priority value: ${value}`);
              return;
            }
            setPriorityError(null);
            edit.commitField({ task, field: "priority", value: priority });
          }}
        />
      </div>
      {priorityError && (
        <div
          className="flex items-start gap-2 border-b border-[color-mix(in_srgb,var(--color-error)_35%,transparent)] bg-[var(--color-error-soft)] px-4 py-[0.55rem] text-[length:var(--text-sm)] text-[var(--color-error)] [&_span]:min-w-0 [&_span]:flex-auto"
          role="alert"
        >
          <span>{priorityError}</span>
        </div>
      )}

      <div className="grid grid-cols-[minmax(0,6.5rem)_minmax(0,1fr)] items-center gap-[0.85rem]">
        <span className="text-[length:var(--text-sm)] font-[var(--font-weight-medium)] text-[var(--text-muted)]">
          Labels
        </span>
        <TaskTagsField
          value={labels}
          ariaLabel="Labels"
          disabled={edit.isFieldPending(task.id, "labels")}
          onCommit={(value) =>
            edit.commitField({ task, field: "labels", value })
          }
        />
      </div>

      <div className="flex flex-col gap-[0.3rem]">
        <div className="text-[length:var(--text-sm)] font-[var(--font-weight-medium)] text-[var(--text-muted)]">
          Description
        </div>
        <TaskTextAreaField
          value={description}
          ariaLabel="Description"
          placeholder="No description"
          rows={5}
          disabled={edit.isFieldPending(task.id, "description")}
          onCommit={(value) =>
            edit.commitField({ task, field: "description", value })
          }
        />
      </div>

      <div className="flex flex-col gap-[0.3rem]">
        <div className="text-[length:var(--text-sm)] font-[var(--font-weight-medium)] text-[var(--text-muted)]">
          Validation criteria
        </div>
        <TaskTextAreaField
          value={validationCriteria}
          ariaLabel="Validation criteria"
          placeholder="No validation criteria"
          rows={4}
          disabled={edit.isFieldPending(task.id, "validation_criteria")}
          onCommit={(value) =>
            edit.commitField({
              task,
              field: "validation_criteria",
              value,
            })
          }
        />
      </div>
    </section>
  );
}
