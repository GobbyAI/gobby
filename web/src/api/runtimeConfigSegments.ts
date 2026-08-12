/**
 * Dynamic config segment codec — the browser third of the cross-language
 * contract pinned by `scripts/generate_runtime_config_contract.py`. The Python
 * registry (`gobby.config.registry`) is the authority; the Rust decoder
 * (`gcore::config::runtime_contract`) and this module must match it
 * byte-for-byte. Conformance is enforced by the generated vector fixture in
 * `runtimeConfigCodecVectors.gen.ts` and its vitest suite.
 *
 * Dynamic map keys (skill hub names, context-window model matches, …) are
 * stored in their canonical encoded form so a key containing `.` can never be
 * confused with a dotted-path separator. UIs decode for display and re-encode
 * on write.
 */

const SAFE_SEGMENT_CHARS = new Set(
  Array.from(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_~",
    (char) => char.charCodeAt(0),
  ),
);

const UPPER_HEX = new Set("0123456789ABCDEF");

export class DynamicSegmentError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "DynamicSegmentError";
  }
}

/** Encode one logical dynamic segment into its canonical UTF-8 form. */
export function encodeDynamicSegment(value: string): string {
  if (value === "") {
    throw new DynamicSegmentError("Dynamic config segment must not be empty");
  }
  for (let index = 0; index < value.length; index += 1) {
    const codeUnit = value.charCodeAt(index);
    if (codeUnit >= 0xd800 && codeUnit <= 0xdbff) {
      const next = value.charCodeAt(index + 1);
      if (index + 1 >= value.length || next < 0xdc00 || next > 0xdfff) {
        throw new DynamicSegmentError(
          "Dynamic config segment must be valid Unicode",
        );
      }
      index += 1;
    } else if (codeUnit >= 0xdc00 && codeUnit <= 0xdfff) {
      throw new DynamicSegmentError(
        "Dynamic config segment must be valid Unicode",
      );
    }
  }
  const bytes = new TextEncoder().encode(value);
  let encoded = "";
  for (const byte of bytes) {
    encoded += SAFE_SEGMENT_CHARS.has(byte)
      ? String.fromCharCode(byte)
      : `%${byte.toString(16).toUpperCase().padStart(2, "0")}`;
  }
  return encoded;
}

/** Decode a canonical segment, rejecting alternate or malformed spellings. */
export function decodeDynamicSegment(value: string): string {
  if (value === "") {
    throw new DynamicSegmentError("Dynamic config segment must not be empty");
  }
  const bytes: number[] = [];
  let index = 0;
  while (index < value.length) {
    const char = value[index];
    if (char === "%") {
      if (index + 2 >= value.length) {
        throw new DynamicSegmentError(
          "Truncated percent escape in dynamic config segment",
        );
      }
      const digits = value.slice(index + 1, index + 3);
      if (!UPPER_HEX.has(digits[0]) || !UPPER_HEX.has(digits[1])) {
        throw new DynamicSegmentError(
          "Percent escapes must use uppercase hexadecimal digits",
        );
      }
      bytes.push(Number.parseInt(digits, 16));
      index += 3;
      continue;
    }
    const code = value.charCodeAt(index);
    if (!SAFE_SEGMENT_CHARS.has(code)) {
      throw new DynamicSegmentError(
        "Dynamic config segment is not canonically encoded",
      );
    }
    bytes.push(code);
    index += 1;
  }
  let decoded: string;
  try {
    decoded = new TextDecoder("utf-8", { fatal: true }).decode(
      new Uint8Array(bytes),
    );
  } catch {
    throw new DynamicSegmentError("Dynamic config segment is not valid UTF-8");
  }
  if (encodeDynamicSegment(decoded) !== value) {
    throw new DynamicSegmentError(
      "Dynamic config segment uses a noncanonical escape",
    );
  }
  return decoded;
}

/**
 * Decode a stored map key for display, keeping a malformed key as-is so a
 * hand-edited store never bricks its editor. Writing the entry back re-encodes
 * the displayed key, canonicalizing it.
 */
export function decodeDynamicSegmentLenient(value: string): string {
  try {
    return decodeDynamicSegment(value);
  } catch {
    return value;
  }
}
