import type { TerminalCore } from "@wterm/core";

/** DECSET mouse tracking modes that wterm's wheel encoder will honor if non-zero. */
export type MouseTrackingMode = 0 | 1000 | 1002 | 1003;

const ESC = "\u001b";
const CSI_PRIVATE_MODE = new RegExp(`${ESC}\\[\\?([\\d;]+)([hl])`, "g");
const INCOMPLETE_CSI_PRIVATE = new RegExp(`${ESC}(?:\\[\\??[\\d;]*)?$`);

function isTrackingMode(param: number): param is 1000 | 1002 | 1003 {
  return param === 1000 || param === 1002 || param === 1003;
}

export interface MouseModeProbe {
  feed(text: string): void;
  tracking(): MouseTrackingMode;
  sgr(): boolean;
}

/**
 * Parse DEC private-mode set/reset (`CSI ? Pm h/l`) out of a VT write stream.
 *
 * Ghostty WASM's `mouse_tracking` export returns only 1000 or 1002, so Grok's
 * any-event mode (`1003`) is invisible to `@wterm/dom` unless we recover it
 * from the bytes ourselves.
 */
export function createMouseModeProbe(): MouseModeProbe {
  const enabled = {
    1000: false,
    1002: false,
    1003: false,
    1006: false,
  };
  let pending = "";

  const apply = (params: number[], set: boolean): void => {
    for (const param of params) {
      if (param === 1006) {
        enabled[1006] = set;
      } else if (isTrackingMode(param)) {
        enabled[param] = set;
      }
    }
  };

  return {
    feed(text: string): void {
      const data = pending + text;
      pending = "";
      CSI_PRIVATE_MODE.lastIndex = 0;
      let consumed = 0;
      let match: RegExpExecArray | null;
      while ((match = CSI_PRIVATE_MODE.exec(data)) !== null) {
        const params = match[1]
          .split(";")
          .map((part) => Number(part))
          .filter((n) => n > 0);
        apply(params, match[2] === "h");
        consumed = match.index + match[0].length;
      }
      const rest = data.slice(consumed);
      const incomplete = rest.match(INCOMPLETE_CSI_PRIVATE);
      pending = incomplete ? incomplete[0] : "";
    },
    tracking(): MouseTrackingMode {
      if (enabled[1003]) return 1003;
      if (enabled[1002]) return 1002;
      if (enabled[1000]) return 1000;
      return 0;
    },
    sgr(): boolean {
      return enabled[1006];
    },
  };
}

/**
 * Patch a terminal core so `mouseTracking()` reports DECSET 1003.
 *
 * `@wterm/dom` forwards wheel as SGR reports only when tracking is non-zero
 * and SGR (1006) is on. Returning 1003 is enough for that branch; hover-move
 * while 1003 is still dropped by wterm's `tracking !== 1002` guard.
 */
export function withAnyMotionMouseTracking<T extends TerminalCore>(core: T): T {
  const probe = createMouseModeProbe();
  const writeString = core.writeString.bind(core);
  const writeRaw = core.writeRaw.bind(core);
  const nativeTracking = core.mouseTracking?.bind(core);
  const decoder = new TextDecoder();

  core.writeString = (str: string, afterChunk?: () => void) => {
    probe.feed(str);
    writeString(str, afterChunk);
  };
  core.writeRaw = (data: Uint8Array, afterChunk?: () => void) => {
    probe.feed(decoder.decode(data));
    writeRaw(data, afterChunk);
  };
  core.mouseTracking = () => {
    const probed = probe.tracking();
    if (probed !== 0) return probed as 0 | 1000 | 1002;
    return nativeTracking?.() ?? 0;
  };
  return core;
}
