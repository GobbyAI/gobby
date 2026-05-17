import type { GobbyTaskDetail } from "../../../hooks/useTasks";
import {
  TaskSelectField,
  TaskTagsField,
  TaskTextAreaField,
  type TaskSelectOption,
} from "../TaskFieldEditors";
import type { TaskInlineEditApi } from "./taskDetailFormat";

/**
 * D5 §3 — the editable core. PATCH-family fields only (category, priority,
 * labels, description, validation criteria); every commit routes through the
 * D4 inline-edit hook. State / owner / stage are deliberately absent — they
 * are action controls elsewhere, never free-text PATCH inputs here.
 */

// Mirrors backend VALID_CATEGORIES (storage/tasks/_models.py). "" clears it.
const CATEGORY_OPTIONS: TaskSelectOption[] = [
  { value: "", label: "Uncategorized" },
  { value: "code", label: "Code" },
  { value: "config", label: "Config" },
  { value: "docs", label: "Docs" },
  { value: "refactor", label: "Refactor" },
  { value: "test", label: "Test" },
  { value: "research", label: "Research" },
  { value: "planning", label: "Planning" },
  { value: "manual", label: "Manual" },
];

// Mirrors PRIORITY_STYLES in TaskBadges.
const PRIORITY_OPTIONS: TaskSelectOption[] = [
  { value: "0", label: "Critical" },
  { value: "1", label: "High" },
  { value: "2", label: "Medium" },
  { value: "3", label: "Low" },
  { value: "4", label: "Backlog" },
];

function StaticBlock({ label, value }: { label: string; value: string }) {
  return (
    <div className="activity-task-detail-core-block">
      <div className="activity-task-detail-core-block__label">{label}</div>
      <div className="activity-task-detail-markdown message-content">
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

  if (!edit) {
    const hasBody = Boolean(description) || Boolean(validationCriteria);
    if (!hasBody && labels.length === 0) return null;
    return (
      <section className="activity-task-detail-core">
        <h3 className="activity-task-detail-kv__title">Details</h3>
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
    <section className="activity-task-detail-core">
      <h3 className="activity-task-detail-kv__title">Details</h3>

      <div className="activity-task-detail-core-row">
        <span className="activity-task-detail-core-row__label">Category</span>
        <TaskSelectField
          value={task.category ?? ""}
          ariaLabel="Category"
          options={CATEGORY_OPTIONS}
          disabled={edit.isFieldPending(task.id, "category")}
          onCommit={(value) =>
            edit.commitField({ task, field: "category", value })
          }
        />
      </div>

      <div className="activity-task-detail-core-row">
        <span className="activity-task-detail-core-row__label">Priority</span>
        <TaskSelectField
          value={String(task.priority ?? 4)}
          ariaLabel="Priority"
          options={PRIORITY_OPTIONS}
          disabled={edit.isFieldPending(task.id, "priority")}
          onCommit={(value) =>
            edit.commitField({ task, field: "priority", value: Number(value) })
          }
        />
      </div>

      <div className="activity-task-detail-core-row">
        <span className="activity-task-detail-core-row__label">Labels</span>
        <TaskTagsField
          value={labels}
          ariaLabel="Labels"
          disabled={edit.isFieldPending(task.id, "labels")}
          onCommit={(value) =>
            edit.commitField({ task, field: "labels", value })
          }
        />
      </div>

      <div className="activity-task-detail-core-block">
        <div className="activity-task-detail-core-block__label">
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

      <div className="activity-task-detail-core-block">
        <div className="activity-task-detail-core-block__label">
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
