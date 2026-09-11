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

// Activity panels show the provider icon alongside this label.
export function getActivitySessionTitle(
  session: SessionTitleLike & {
    ref: string;
    title_source?: string | null;
  },
): string {
  const title = stripSessionTitlePrefix(session.title);
  return session.title_source === "provisional" || !title
    ? session.ref
    : `${session.ref}: ${title}`;
}
