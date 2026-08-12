/**
 * Sessions-tab filter dropdown — type, defaults, serializers.
 *
 * The URL serializer is the contract between the dropdown UI and the
 * useSessionCatalog hook (which forwards everything as query params on
 * /api/sessions). The storage serializer is the contract between the
 * dropdown and localStorage (Sets are not JSON-serializable, so we
 * round-trip through arrays).
 */

export type SessionMode = "interactive" | "auto";
export type TaskRefRole = "claimed" | "created" | "closed";
export type DatePreset = "24h" | "7d" | "30d" | "all" | "custom";
export type SessionStatus = "active" | "paused" | "expired";

const ALL_MODES: readonly SessionMode[] = ["interactive", "auto"];
const ALL_TASK_REF_ROLES: readonly TaskRefRole[] = [
  "claimed",
  "created",
  "closed",
];
const ALL_DATE_PRESETS: readonly DatePreset[] = [
  "24h",
  "7d",
  "30d",
  "all",
  "custom",
];
const ALL_STATUSES: readonly SessionStatus[] = ["active", "paused", "expired"];

/** Default Live set — the SegmentedControl's "Live" option resolves here. */
export const DEFAULT_LIVE_STATUSES: readonly SessionStatus[] = [
  "active",
  "paused",
];

export interface SessionsFilters {
  modes: Set<SessionMode>;
  providers: Set<string>;
  sessionRefMin: number | null;
  sessionRefMax: number | null;
  taskRefMin: number | null;
  taskRefMax: number | null;
  taskRefRoles: Set<TaskRefRole>;
  datePreset: DatePreset;
  dateCustomFrom: string | null; // YYYY-MM-DD
  dateCustomTo: string | null;
  statuses: Set<SessionStatus>;
  blockedOnly: boolean;
}

export function defaultSessionsFilters(): SessionsFilters {
  return {
    modes: new Set<SessionMode>(),
    providers: new Set<string>(),
    sessionRefMin: null,
    sessionRefMax: null,
    taskRefMin: null,
    taskRefMax: null,
    taskRefRoles: new Set<TaskRefRole>(),
    datePreset: "all",
    dateCustomFrom: null,
    dateCustomTo: null,
    statuses: new Set<SessionStatus>(DEFAULT_LIVE_STATUSES),
    blockedOnly: false,
  };
}

/** Number of filter sections that have a non-default value. Drives the badge on the funnel button. */
export function countActiveFilters(filters: SessionsFilters): number {
  let count = 0;
  if (filters.modes.size > 0 && filters.modes.size < ALL_MODES.length)
    count += 1;
  if (filters.providers.size > 0) count += 1;
  if (filters.sessionRefMin !== null || filters.sessionRefMax !== null)
    count += 1;
  if (filters.taskRefMin !== null || filters.taskRefMax !== null) count += 1;
  if (filters.datePreset !== "all") count += 1;
  if (filters.blockedOnly) count += 1;
  return count;
}

function parseLocalDate(value: string | null): Date | null {
  if (value === null) return null;

  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (match === null) return null;

  const [, yearText, monthText, dayText] = match;
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  const date = new Date(year, month - 1, day);
  if (year < 100) date.setFullYear(year);

  return Number.isFinite(date.getTime()) &&
    date.getFullYear() === year &&
    date.getMonth() === month - 1 &&
    date.getDate() === day
    ? date
    : null;
}

function isValidLocalDate(value: unknown): value is string {
  return typeof value === "string" && parseLocalDate(value) !== null;
}

/**
 * Resolve a date preset to (after, before) inclusive-after / exclusive-before
 * timestamps. Returns null for either bound when the preset doesn't apply.
 */
export function resolveDateRange(
  filters: SessionsFilters,
  now: Date,
): { after: string | null; before: string | null } {
  switch (filters.datePreset) {
    case "all":
      return { after: null, before: null };
    case "24h":
      return {
        after: new Date(now.getTime() - 24 * 60 * 60 * 1000).toISOString(),
        before: null,
      };
    case "7d":
      return {
        after: new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000).toISOString(),
        before: null,
      };
    case "30d":
      return {
        after: new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000).toISOString(),
        before: null,
      };
    case "custom": {
      const afterDate = parseLocalDate(filters.dateCustomFrom);
      const beforeDate = parseLocalDate(filters.dateCustomTo);
      const after = afterDate?.toISOString() ?? null;
      // The next local midnight keeps the end date inclusive while preserving
      // the backend's exclusive-before semantics across DST transitions.
      const before = beforeDate
        ? new Date(
            beforeDate.getFullYear(),
            beforeDate.getMonth(),
            beforeDate.getDate() + 1,
          ).toISOString()
        : null;
      return { after, before };
    }
  }
}

interface FilterableSession {
  source: string;
  status: string;
  model: string | null;
  agent_depth: number;
  seq_num: number | null;
  created_at: string;
  claimed_task_refs?: number[];
  created_task_refs?: number[];
  closed_task_refs?: number[];
  blockedCount?: number;
}

/**
 * Client-side equivalent of the backend filter predicates. Used while the
 * dropdown lives in SessionsTab and filters narrow the loaded session window.
 * Once filters are pushed up to App-level useSessionCatalog, this becomes a
 * fallback for the remaining client-only predicates.
 */
export function matchesSessionsFilters(
  session: FilterableSession,
  filters: SessionsFilters,
  now: Date,
): boolean {
  if (filters.blockedOnly && (session.blockedCount ?? 0) === 0) {
    return false;
  }

  if (
    filters.statuses.size > 0 &&
    !filters.statuses.has(session.status as SessionStatus)
  ) {
    return false;
  }

  if (filters.modes.size > 0) {
    const isInteractive = session.agent_depth === 0;
    const isAuto = session.agent_depth >= 1;
    const matchesMode =
      (filters.modes.has("interactive") && isInteractive) ||
      (filters.modes.has("auto") && isAuto);
    if (!matchesMode) return false;
  }

  if (filters.providers.size > 0 && !filters.providers.has(session.source)) {
    return false;
  }

  if (filters.sessionRefMin !== null) {
    if (session.seq_num === null || session.seq_num < filters.sessionRefMin)
      return false;
  }
  if (filters.sessionRefMax !== null) {
    if (session.seq_num === null || session.seq_num > filters.sessionRefMax)
      return false;
  }

  if (filters.taskRefMin !== null || filters.taskRefMax !== null) {
    const min = filters.taskRefMin ?? -Infinity;
    const max = filters.taskRefMax ?? Infinity;
    const roleColumns: Record<TaskRefRole, number[]> = {
      claimed: session.claimed_task_refs ?? [],
      created: session.created_task_refs ?? [],
      closed: session.closed_task_refs ?? [],
    };
    const roles: ReadonlySet<TaskRefRole> | readonly TaskRefRole[] =
      filters.taskRefRoles.size > 0 ? filters.taskRefRoles : ["claimed"];
    let anyMatch = false;
    for (const role of roles) {
      if (roleColumns[role].some((ref) => ref >= min && ref <= max)) {
        anyMatch = true;
        break;
      }
    }
    if (!anyMatch) return false;
  }

  const { after, before } = resolveDateRange(filters, now);
  if (after) {
    if (session.created_at < after) return false;
  }
  if (before) {
    if (session.created_at >= before) return false;
  }

  return true;
}

/**
 * Build URLSearchParams matching the backend /api/sessions filter contract.
 * Empty Sets and null bounds drop out (the server treats absence as "no filter").
 */
export function serializeSessionsFilters(
  filters: SessionsFilters,
  now: Date,
): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.modes.size > 0 && filters.modes.size < ALL_MODES.length) {
    for (const mode of filters.modes) params.append("mode", mode);
  }
  for (const provider of filters.providers) params.append("sources", provider);
  for (const status of filters.statuses) params.append("status_in", status);
  if (filters.sessionRefMin !== null)
    params.set("session_seq_min", String(filters.sessionRefMin));
  if (filters.sessionRefMax !== null)
    params.set("session_seq_max", String(filters.sessionRefMax));
  if (filters.taskRefMin !== null)
    params.set("task_ref_min", String(filters.taskRefMin));
  if (filters.taskRefMax !== null)
    params.set("task_ref_max", String(filters.taskRefMax));
  if (filters.taskRefMin !== null || filters.taskRefMax !== null) {
    for (const role of filters.taskRefRoles)
      params.append("task_ref_role", role);
  }
  const { after, before } = resolveDateRange(filters, now);
  if (after) params.set("created_after", after);
  if (before) params.set("created_before", before);
  return params;
}

interface StoredSessionsFilters {
  modes: SessionMode[];
  providers: string[];
  sessionRefMin: number | null;
  sessionRefMax: number | null;
  taskRefMin: number | null;
  taskRefMax: number | null;
  taskRefRoles: TaskRefRole[];
  datePreset: DatePreset;
  dateCustomFrom: string | null;
  dateCustomTo: string | null;
  statuses: SessionStatus[];
  blockedOnly: boolean;
}

/** Round-trip through arrays for JSON.stringify-friendly localStorage payloads. */
export function serializeForStorage(
  filters: SessionsFilters,
): StoredSessionsFilters {
  return {
    modes: [...filters.modes],
    providers: [...filters.providers],
    sessionRefMin: filters.sessionRefMin,
    sessionRefMax: filters.sessionRefMax,
    taskRefMin: filters.taskRefMin,
    taskRefMax: filters.taskRefMax,
    taskRefRoles: [...filters.taskRefRoles],
    datePreset: filters.datePreset,
    dateCustomFrom: filters.dateCustomFrom,
    dateCustomTo: filters.dateCustomTo,
    statuses: [...filters.statuses],
    blockedOnly: filters.blockedOnly,
  };
}

/**
 * Hydrate a SessionsFilters from a localStorage payload. Tolerates malformed
 * input (returns defaults on any parse error) — a corrupt storage entry should
 * never wedge the app.
 */
export function deserializeFromStorage(raw: string | null): SessionsFilters {
  if (raw === null) return defaultSessionsFilters();
  try {
    const parsed = JSON.parse(raw) as Partial<StoredSessionsFilters>;
    const base = defaultSessionsFilters();

    const modes = new Set<SessionMode>(
      Array.isArray(parsed.modes)
        ? parsed.modes.filter((m): m is SessionMode => ALL_MODES.includes(m))
        : [],
    );
    const providers = new Set<string>(
      Array.isArray(parsed.providers)
        ? parsed.providers.filter((p) => typeof p === "string")
        : [],
    );
    const taskRefRoles = new Set<TaskRefRole>(
      Array.isArray(parsed.taskRefRoles)
        ? parsed.taskRefRoles.filter((r): r is TaskRefRole =>
            ALL_TASK_REF_ROLES.includes(r),
          )
        : base.taskRefRoles,
    );
    const datePreset = ALL_DATE_PRESETS.includes(
      parsed.datePreset as DatePreset,
    )
      ? (parsed.datePreset as DatePreset)
      : base.datePreset;
    // Missing or non-array statuses → fall back to default Live (mirrors today's
    // initial state). Unknown values are stripped silently.
    const statuses = new Set<SessionStatus>(
      Array.isArray(parsed.statuses)
        ? parsed.statuses.filter((s): s is SessionStatus =>
            ALL_STATUSES.includes(s),
          )
        : base.statuses,
    );

    return {
      modes,
      providers,
      sessionRefMin:
        typeof parsed.sessionRefMin === "number" ? parsed.sessionRefMin : null,
      sessionRefMax:
        typeof parsed.sessionRefMax === "number" ? parsed.sessionRefMax : null,
      taskRefMin:
        typeof parsed.taskRefMin === "number" ? parsed.taskRefMin : null,
      taskRefMax:
        typeof parsed.taskRefMax === "number" ? parsed.taskRefMax : null,
      taskRefRoles,
      datePreset,
      dateCustomFrom: isValidLocalDate(parsed.dateCustomFrom)
        ? parsed.dateCustomFrom
        : null,
      dateCustomTo: isValidLocalDate(parsed.dateCustomTo)
        ? parsed.dateCustomTo
        : null,
      statuses,
      blockedOnly: parsed.blockedOnly === true,
    };
  } catch {
    return defaultSessionsFilters();
  }
}
