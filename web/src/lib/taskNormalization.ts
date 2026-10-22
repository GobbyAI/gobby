import type { CanonicalTaskState, OwnerSessionRef } from "./taskState";
import { getCanonicalTaskState, getTaskDisplayState } from "./taskState";
import { DEFAULT_TASK_PRIORITY } from "./taskOptions";

const MAX_TASK_PAYLOAD_EXTRACT_DEPTH = 5;

export type RawTaskPayload = {
  id: string;
  ref?: string | null;
  title?: string | null;
  status?: string | null;
  priority?: number | null;
  task_type?: string | null;
  type?: string | null;
  parent_task_id?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  seq_num?: number | null;
  path_cache?: string | null;
  agent_name?: string | null;
  sequence_order?: number | null;
  start_date?: string | null;
  due_date?: string | null;
  project_id?: string | null;
  closed_at?: string | null;
  closed_in_session_id?: string | null;
  escalated_at?: string | null;
  pre_escalation_status?: string | null;
  category?: string | null;
  description?: string | null;
  labels?: string[] | null;
  validation_criteria?: string | null;
  owner_session_ref?: OwnerSessionRef | null;
  state?: Partial<CanonicalTaskState> | null;
  additional_skills?: string[] | null;
  assigned_agent?: string | null;
};

export type NormalizedTaskPayload = Omit<
  RawTaskPayload,
  "state"
> & {
  ref: string;
  title: string;
  status: string;
  priority: number;
  task_type: string;
  parent_task_id: string | null;
  created_at: string;
  updated_at: string;
  seq_num: number | null;
  path_cache: string | null;
  agent_name: string | null;
  sequence_order: number | null;
  start_date: string | null;
  due_date: string | null;
  project_id: string;
  state: CanonicalTaskState;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isOptionalString(value: unknown): boolean {
  return value === undefined || value === null || typeof value === "string";
}

function isOptionalNumber(value: unknown): boolean {
  return value === undefined || value === null || typeof value === "number";
}

function isOptionalRecord(value: unknown): boolean {
  return value === undefined || value === null || isRecord(value);
}

function isStringArrayOrNull(value: unknown): boolean {
  return (
    value === undefined ||
    value === null ||
    (Array.isArray(value) && value.every((item) => typeof item === "string"))
  );
}

function isOptionalOwnerSessionRef(value: unknown): boolean {
  if (value === undefined || value === null) return true;
  if (!isRecord(value)) return false;
  return (
    typeof value.session_id === "string" &&
    value.session_id.trim().length > 0 &&
    typeof value.ref === "string" &&
    value.ref.trim().length > 0 &&
    (value.source === undefined ||
      value.source === null ||
      typeof value.source === "string")
  );
}

type OptionalTaskFieldValidator = (value: unknown) => boolean;

const OPTIONAL_TASK_FIELD_VALIDATORS = {
  ref: isOptionalString,
  title: isOptionalString,
  status: isOptionalString,
  task_type: isOptionalString,
  type: isOptionalString,
  parent_task_id: isOptionalString,
  created_at: isOptionalString,
  updated_at: isOptionalString,
  path_cache: isOptionalString,
  agent_name: isOptionalString,
  start_date: isOptionalString,
  due_date: isOptionalString,
  project_id: isOptionalString,
  closed_at: isOptionalString,
  closed_in_session_id: isOptionalString,
  escalated_at: isOptionalString,
  pre_escalation_status: isOptionalString,
  category: isOptionalString,
  description: isOptionalString,
  validation_criteria: isOptionalString,
  assigned_agent: isOptionalString,
  priority: isOptionalNumber,
  seq_num: isOptionalNumber,
  sequence_order: isOptionalNumber,
  state: isOptionalRecord,
  owner_session_ref: isOptionalOwnerSessionRef,
  labels: isStringArrayOrNull,
  additional_skills: isStringArrayOrNull,
} satisfies Partial<Record<keyof RawTaskPayload, OptionalTaskFieldValidator>>;

function hasValidOptionalTaskFields(record: Record<string, unknown>): boolean {
  return (
    Object.entries(OPTIONAL_TASK_FIELD_VALIDATORS).every(([field, validate]) =>
      validate(record[field]),
    )
  );
}

export function isRawTaskPayload(value: unknown): value is RawTaskPayload {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    value.id.length > 0 &&
    hasValidOptionalTaskFields(value)
  );
}

export function extractTaskPayload(
  data: unknown,
  depth = 0,
  seen: WeakSet<object> = new WeakSet(),
): RawTaskPayload | null {
  if (depth >= MAX_TASK_PAYLOAD_EXTRACT_DEPTH) return null;
  if (isRawTaskPayload(data)) return data;
  if (isRecord(data)) {
    if (seen.has(data)) return null;
    seen.add(data);
    if (isRawTaskPayload(data.task)) return data.task;
    if (isRecord(data.data))
      return extractTaskPayload(data.data, depth + 1, seen);
  }
  return null;
}

export function normalizeTaskPayload<T extends RawTaskPayload>(
  task: T,
): T & NormalizedTaskPayload {
  const projected = {
    ...task,
    ref: task.ref ?? (task.seq_num != null ? `#${task.seq_num}` : task.id),
    title: task.title ?? "",
    priority: task.priority ?? DEFAULT_TASK_PRIORITY,
    task_type: task.task_type ?? task.type ?? "task",
    parent_task_id: task.parent_task_id ?? null,
    created_at: task.created_at ?? "",
    updated_at: task.updated_at ?? "",
    seq_num: task.seq_num ?? null,
    path_cache: task.path_cache ?? null,
    agent_name: task.agent_name ?? null,
    sequence_order: task.sequence_order ?? null,
    start_date: task.start_date ?? null,
    due_date: task.due_date ?? null,
    project_id: task.project_id ?? "",
  };
  const canonical = getCanonicalTaskState(projected);
  const status = getTaskDisplayState({ ...projected, state: canonical });

  return {
    ...projected,
    status,
    state: canonical,
  } as T & NormalizedTaskPayload;
}

export function normalizeTaskPayloads<T extends RawTaskPayload>(
  tasks: T[] | undefined,
): Array<T & NormalizedTaskPayload> {
  return (tasks ?? []).map((task) => normalizeTaskPayload(task));
}
