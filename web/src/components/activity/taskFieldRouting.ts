/**
 * D4 — edit routing by endpoint family.
 *
 * The PATCH /api/tasks/{id} route (src/gobby/servers/routes/tasks.py
 * update_task) accepts metadata fields only. Routing every editable field
 * through this classifier is the structural guarantee that only `patch`-family
 * fields are sent to PATCH; state/stage and terminal actions resolve to their
 * dedicated endpoints instead.
 */

export const PATCH_EDITABLE_FIELDS = [
  "title",
  "description",
  "priority",
  "task_type",
  "category",
  "labels",
  "validation_criteria",
] as const;

export type PatchEditableField = (typeof PATCH_EDITABLE_FIELDS)[number];

export type EditableFieldFamily = "patch" | "stage" | "terminal";

const PATCH_FIELD_SET: ReadonlySet<string> = new Set(PATCH_EDITABLE_FIELDS);

const TERMINAL_FIELDS: ReadonlySet<string> = new Set([
  "close",
  "reopen",
  "escalate",
  "de-escalate",
]);

export function isPatchEditableField(
  field: string,
): field is PatchEditableField {
  return PATCH_FIELD_SET.has(field);
}

/**
 * Classify an editable field/action into the endpoint family that may
 * service it. Returns null for anything not editable through the task UI;
 * callers must treat null as "not editable", never as "PATCH it anyway".
 *
 * - `patch`    → PATCH /api/tasks/{id} (title/description/priority/task_type/
 *                category/labels/validation_criteria)
 * - `stage`    → PATCH /api/tasks/{id}/stages/{stage} (state/stage moves)
 * - `terminal` → POST /close, /reopen, /escalate, /de-escalate
 */
export function classifyEditableField(
  field: string,
): EditableFieldFamily | null {
  if (PATCH_FIELD_SET.has(field)) return "patch";
  if (field === "state" || field === "stage") return "stage";
  if (TERMINAL_FIELDS.has(field)) return "terminal";
  return null;
}
