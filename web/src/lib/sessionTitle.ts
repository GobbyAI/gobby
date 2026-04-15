export const DEFAULT_SESSION_TITLE = "New Session";

interface SessionTitleLike {
  title?: string | null;
  seq_num?: number | null;
  ref?: string | null;
}

export function getSessionTitleText(title?: string | null): string {
  const trimmed = title?.trim();
  return trimmed ? trimmed : DEFAULT_SESSION_TITLE;
}

export function getSessionDisplayTitle(session: SessionTitleLike): string {
  const titleText = getSessionTitleText(session.title);
  if (session.seq_num != null) {
    return `#${session.seq_num}: ${titleText}`;
  }
  if (session.ref) {
    return `${session.ref}: ${titleText}`;
  }
  return titleText;
}
