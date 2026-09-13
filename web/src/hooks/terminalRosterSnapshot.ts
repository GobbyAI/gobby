/**
 * Roster derivation for the terminals list.
 *
 * The daemon answers `terminal_list` in pages. Cursor state is keyed to one
 * walk (a fresh init/refresh listing plus its continuation pages); a superseded
 * walk's pages must not merge rows or steer the cursor, or a refresh started
 * mid-pagination under-fetches. Row identity is `terminal_id` and nothing here
 * derives identity from list position, so a reordered roster is the same roster.
 */

export interface TmuxSession {
  terminal_id: string;
  backend: string;
  ownership: string;
  state: string;
  title: string | null;
  session_id: string | null;
  agent_run_id: string | null;
  dims: { rows: number; cols: number } | null;
  name: string;
  socket: string;
  pane_pid: number | null;
  pane_dead: boolean;
  pane_title: string | null;
  pane_command: string | null;
  pane_path: string | null;
  window_name: string | null;
  session_title: string | null;
  gobby_session_id: string | null;
  agent_managed: boolean;
  attached_bridge: string | null;
}

export interface TmuxTarget {
  terminal_id: string;
}

export interface CreatedTmuxSession {
  terminal_id: string;
}

export interface RosterWalk {
  id: string;
  cursor: string | null;
}

export interface RosterPage {
  walk: RosterWalk;
  /** A fresh listing replaces the table so vanished terminals drop out. */
  replace: boolean;
  rows: TmuxSession[];
  /** The cursor to request next, or null when this walk is done here. */
  nextCursor: string | null;
  /** The page carried no cursor at all, so the roster is complete. */
  lastPage: boolean;
}

export function asSession(
  row: Partial<TmuxSession> & { terminal_id?: string },
): TmuxSession {
  const terminalId = row.terminal_id ?? "";
  return {
    terminal_id: terminalId,
    backend: row.backend ?? "tmux",
    ownership: row.ownership ?? "gobby",
    state: row.state ?? "live",
    title: row.title ?? row.name ?? null,
    session_id: row.session_id ?? row.gobby_session_id ?? null,
    agent_run_id: row.agent_run_id ?? null,
    dims: row.dims ?? null,
    name: row.name ?? row.title ?? terminalId,
    socket: row.socket ?? row.backend ?? "tmux",
    pane_pid: row.pane_pid ?? null,
    pane_dead: row.pane_dead ?? false,
    pane_title: row.pane_title ?? row.title ?? null,
    pane_command: row.pane_command ?? null,
    pane_path: row.pane_path ?? null,
    window_name: row.window_name ?? null,
    session_title: row.session_title ?? row.title ?? null,
    gobby_session_id: row.gobby_session_id ?? row.session_id ?? null,
    agent_managed: row.agent_managed ?? row.ownership === "gobby",
    attached_bridge: row.attached_bridge ?? null,
  };
}

/** The request id a continuation page of `walkId` is sent and answered under. */
export function rosterPageRequestId(walkId: string): string {
  return `page:${walkId}`;
}

/**
 * Fold one `terminal_list` page into the walk, or return null when the page
 * belongs to a walk that has been superseded and must not be applied.
 */
export function applyRosterPage(
  walk: RosterWalk | null,
  message: Record<string, unknown>,
): RosterPage | null {
  const requestId =
    typeof message.request_id === "string" ? message.request_id : "";
  const isFreshWalk = requestId === "init" || requestId.startsWith("refresh");
  const current = isFreshWalk ? { id: requestId, cursor: null } : walk;
  // A superseded walk's page: merging would resurrect rows the fresh listing
  // dropped, and its cursor would truncate the fresh walk.
  if (
    current === null ||
    (!isFreshWalk && requestId !== rosterPageRequestId(current.id))
  ) {
    return null;
  }
  const rows = ((message.items as TmuxSession[] | undefined) ?? []).map(
    asSession,
  );
  const cursor = message.next_cursor;
  const nextCursor =
    typeof cursor === "string" && cursor && cursor !== current.cursor
      ? cursor
      : null;
  return {
    walk: nextCursor === null ? current : { id: current.id, cursor: nextCursor },
    replace: isFreshWalk,
    rows,
    nextCursor,
    lastPage: cursor === null || cursor === undefined,
  };
}

/** Continuation pages append rows the table does not already carry. */
export function mergeRosterRows(
  current: readonly TmuxSession[],
  page: readonly TmuxSession[],
): TmuxSession[] {
  const seen = new Set(current.map((row) => row.terminal_id));
  const merged = [...current];
  for (const row of page) {
    if (!seen.has(row.terminal_id)) merged.push(row);
  }
  return merged;
}
