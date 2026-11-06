/**
 * Session labels are derived at read time from four inputs: the session ref,
 * its agent definition, the task it claims, and its status line. Nothing
 * stores a name. The attention roster supplies the last three, and gclient's
 * sidebar renders the same layout (`crates/gclient/src/ui/sidebar_rows.rs`).
 */
export interface SessionRosterTask {
  /** Task reference such as `#25767`. */
  ref: string;
  title: string | null;
  /** True only for the seat that holds the task's claim. */
  claimHolder: boolean;
}

export interface SessionRosterLabel {
  definition: string | null;
  task: SessionRosterTask | null;
  statusLine: string | null;
}

export interface SessionLabelLines {
  /** The ref, then the definition name when the roster knows one. */
  head: string;
  /** Present only while the session holds a task. */
  taskLine: string | null;
  /** Shown only while no task is held. */
  statusLine: string | null;
}

/**
 * `gobby#12856` reads as `#12856` inside its own project — the panel already
 * scopes to it, so the name is noise. Refs from other projects keep theirs.
 */
export function getSessionRefLabel(ref: string, trimProject: boolean): string {
  return trimProject ? ref.replace(/^[^#]*#/, "#") : ref;
}

function taskLineFor(task: SessionRosterTask): string {
  const number = task.ref.slice(task.ref.lastIndexOf("#") + 1);
  const mark = task.claimHolder ? " claim" : "";
  const title = task.title?.trim();
  return `Working task ${number}${mark}${title ? ` ${title}` : ""}`;
}

export function getSessionLabelLines(
  session: { ref: string },
  roster: SessionRosterLabel | null | undefined,
  trimProject = false,
): SessionLabelLines {
  const ref = getSessionRefLabel(session.ref, trimProject);
  const definition = roster?.definition?.trim();
  const task = roster?.task ?? null;
  const statusText = roster?.statusLine?.trim();
  return {
    head: definition ? `${ref}: ${definition}` : ref,
    taskLine: task ? taskLineFor(task) : null,
    statusLine: task || !statusText ? null : statusText,
  };
}
