/**
 * Bounds one attachment's restored scrollback before it reaches the renderer.
 *
 * The daemon already caps what it captures (`tmux_attach_history_lines`,
 * `tmux_attach_history_max_bytes`), but those are the host's caps, not this
 * client's: a native backend, a different host config, or a future capture path
 * can each hand the browser more than it can afford to paint. This is the
 * renderer's own ceiling, and it is absolute — a single line carrying no
 * newline is trimmed too, or the byte ceiling is only a suggestion.
 *
 * Splitting and rejoining on "\n" is lossless for the daemon's "\r\n" history:
 * each line keeps its own trailing "\r", so the separator survives the round
 * trip and an SGR run that arrived intact is never re-encoded.
 */

export const TERMINAL_HISTORY_MAX_LINES = 2_000;
export const TERMINAL_HISTORY_MAX_BYTES = 256 * 1024;

export interface BoundedHistory {
  text: string;
  /** Lines dropped from the front. Non-zero means the pane must say so. */
  droppedLines: number;
}

const encoder = new TextEncoder();

const byteLength = (value: string): number => encoder.encode(value).length;

/**
 * Drop leading characters until the remainder fits, on a character boundary.
 * Only an oversized single line reaches this; the line walk handles the rest.
 */
function trimHeadToBytes(value: string, maxBytes: number): string {
  if (byteLength(value) <= maxBytes) return value;

  let low = 0;
  let high = value.length;
  while (low < high) {
    const mid = Math.floor((low + high) / 2);
    if (byteLength(value.slice(mid)) <= maxBytes) high = mid;
    else low = mid + 1;
  }

  // Landing on a trailing surrogate would hand the renderer half a character.
  const code = value.charCodeAt(low);
  return value.slice(code >= 0xdc00 && code <= 0xdfff ? low + 1 : low);
}

export function boundAttachHistory(
  text: string,
  maxLines: number = TERMINAL_HISTORY_MAX_LINES,
  maxBytes: number = TERMINAL_HISTORY_MAX_BYTES,
): BoundedHistory {
  if (text === "") return { text, droppedLines: 0 };

  const lines = text.split("\n");
  const lineBytes = lines.map(byteLength);

  let start = Math.max(0, lines.length - maxLines);
  // Measured once and carried, rather than rejoining the tail on every step:
  // this runs against a quarter-megabyte payload on every attach.
  let total = 0;
  for (let index = start; index < lines.length; index += 1) {
    total += (lineBytes[index] ?? 0) + (index > start ? 1 : 0);
  }
  while (total > maxBytes && start < lines.length - 1) {
    total -= (lineBytes[start] ?? 0) + 1;
    start += 1;
  }

  return {
    text: trimHeadToBytes(lines.slice(start).join("\n"), maxBytes),
    droppedLines: start,
  };
}
