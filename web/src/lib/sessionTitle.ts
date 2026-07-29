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

function getSessionRef(session: SessionTitleLike): string | null {
  if (session.seq_num != null) {
    return `#${session.seq_num}`;
  }
  return session.ref?.trim() || null;
}

function stripMatchingSessionRef(title: string, ref: string): string | null {
  if (title === ref) {
    return "";
  }
  if (title.startsWith(`${ref}:`)) {
    return title.slice(ref.length + 1).trimStart();
  }
  if (title.startsWith(`${ref} `)) {
    return title.slice(ref.length).trimStart();
  }
  return null;
}

export function getSessionDisplayTitle(
  session: SessionTitleLike,
): string {
  const titleText = getSessionTitleText(session.title);
  const ref = getSessionRef(session);
  if (!ref) {
    return titleText;
  }
  const titleWithoutRef = stripMatchingSessionRef(titleText, ref) ?? titleText;
  return `${ref}: ${getSessionTitleText(titleWithoutRef)}`;
}
