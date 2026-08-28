export const DEFAULT_SESSION_TITLE = "New Session";

interface SessionTitleLike {
  title?: string | null;
}

export function getSessionTitleText(title?: string | null): string {
  const trimmed = title?.trim();
  return trimmed ? trimmed : DEFAULT_SESSION_TITLE;
}

export function getSessionDisplayTitle(session: SessionTitleLike): string {
  return getSessionTitleText(session.title);
}
