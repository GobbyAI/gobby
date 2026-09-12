const CSI = "\x1b[";
const CSI_ARROW = /^(?:1;(\d))?([ABCD])$/;

/**
 * Fold a sticky Ctrl into one key's bytes. Letters and the C0 punctuation
 * (@ [ \ ] ^ _) become control codes, `?` becomes DEL, arrows gain the xterm
 * modifier parameter (5 = Ctrl, 6 = Ctrl+Shift). Anything else passes
 * through unchanged so the caller can tell whether the modifier was used.
 */
export function applyCtrlModifier(data: string): string {
  if (data.length === 1) {
    const upper = data.toUpperCase();
    if (/[A-Z@[\\\]^_]/.test(upper)) {
      return String.fromCharCode(upper.charCodeAt(0) & 0x1f);
    }
    return data === "?" ? "\x7f" : data;
  }
  if (!data.startsWith(CSI)) return data;
  const arrow = CSI_ARROW.exec(data.slice(CSI.length));
  if (arrow) {
    const modifier = arrow[1] === "2" ? 6 : 5;
    return `${CSI}1;${modifier}${arrow[2]}`;
  }
  return data;
}
