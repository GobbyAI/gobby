export const DEFAULT_SESSION_TITLE = "New Session";

interface SessionTitleLike {
  title?: string | null;
}

// The UI already shows the session reference separately from the title.
const SESSION_PREFIX = /^\s*(?:[^\s:()]+#\d+\s*:|\([^)]*\)\s*:?)\s*/;

export function stripSessionTitlePrefix(title?: string | null): string {
  return (title ?? "").replace(SESSION_PREFIX, "").trim();
}

export function getSessionTitleText(title?: string | null): string {
  const stripped = stripSessionTitlePrefix(title);
  return stripped ? stripped : DEFAULT_SESSION_TITLE;
}

export function getSessionDisplayTitle(session: SessionTitleLike): string {
  return getSessionTitleText(session.title);
}

/**
 * `gobby#12856` reads as `#12856` inside its own project — the panel already
 * scopes to it, so the name is noise. Refs from other projects keep theirs.
 */
export function getSessionRefLabel(ref: string, trimProject: boolean): string {
  return trimProject ? ref.replace(/^[^#]*#/, "#") : ref;
}

export interface ActivitySessionTitleParts {
  ref: string;
  /** Null while the title is provisional or empty: the ref stands alone. */
  title: string | null;
}

export function getActivitySessionTitleParts(
  session: SessionTitleLike & {
    ref: string;
    title_source?: string | null;
  },
  trimProject = false,
): ActivitySessionTitleParts {
  const title = stripSessionTitlePrefix(session.title);
  return {
    ref: getSessionRefLabel(session.ref, trimProject),
    title: session.title_source === "provisional" || !title ? null : title,
  };
}

// Activity panels show the provider icon alongside this label.
export function getActivitySessionTitle(
  session: SessionTitleLike & {
    ref: string;
    title_source?: string | null;
  },
  trimProject = false,
): string {
  const { ref, title } = getActivitySessionTitleParts(session, trimProject);
  return title === null ? ref : `${ref}: ${title}`;
}
