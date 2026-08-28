export const DEFAULT_SESSION_TITLE = "New Session";

interface SessionTitleLike {
  title?: string | null;
}

// Persisted titles carry a parenthesised provenance prefix ("(gobby-S#11155): ")
// that the tmux window name needs and the UI already shows as the session ref.
const PARENTHESISED_PREFIX = /^\s*\([^)]*\)\s*:?\s*/;

export function stripSessionTitlePrefix(title?: string | null): string {
  return (title ?? "").replace(PARENTHESISED_PREFIX, "").trim();
}

export function getSessionTitleText(title?: string | null): string {
  const stripped = stripSessionTitlePrefix(title);
  return stripped ? stripped : DEFAULT_SESSION_TITLE;
}

export function getSessionDisplayTitle(session: SessionTitleLike): string {
  return getSessionTitleText(session.title);
}
