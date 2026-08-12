/**
 * Edit/Save/Cancel buttons for the read-only-first editing pattern; state
 * lives in `useEditableContent` (editableContent.ts).
 */

import { Button } from "../ui/Button";

export function EditIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M12 20h9" />
      <path d="M16.5 3.5a2.121 2.121 0 1 1 3 3L7 19l-4 1 1-4 12.5-12.5z" />
    </svg>
  );
}

export function CheckIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

export function XIcon() {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </svg>
  );
}

interface EditableViewActionsProps {
  isEditing: boolean;
  onEdit: () => void;
  onSave: () => void;
  onCancel: () => void;
  /** Blocks entering edit mode (loading, load error, deleted resource). */
  editDisabled?: boolean;
  saveDisabled?: boolean;
  saving?: boolean;
  buttonClassName?: string;
  labelClassName?: string;
}

export function EditableViewActions({
  isEditing,
  onEdit,
  onSave,
  onCancel,
  editDisabled,
  saveDisabled,
  saving,
  buttonClassName,
  labelClassName,
}: EditableViewActionsProps) {
  if (isEditing) {
    return (
      <>
        <Button
          variant="accent"
          size="sm"
          className={buttonClassName}
          onClick={onSave}
          disabled={saveDisabled || saving}
          aria-label="Save"
          title="Save"
        >
          <CheckIcon />
          <span className={labelClassName}>
            {saving ? "Saving..." : "Save"}
          </span>
        </Button>
        <Button
          variant="accent"
          size="sm"
          className={buttonClassName}
          onClick={onCancel}
          disabled={saving}
          aria-label="Cancel"
          title="Cancel"
        >
          <XIcon />
          <span className={labelClassName}>Cancel</span>
        </Button>
      </>
    );
  }
  return (
    <Button
      variant="accent"
      size="sm"
      className={buttonClassName}
      onClick={onEdit}
      disabled={editDisabled}
      aria-label="Edit"
      title="Edit"
    >
      <EditIcon />
      <span className={labelClassName}>Edit</span>
    </Button>
  );
}
