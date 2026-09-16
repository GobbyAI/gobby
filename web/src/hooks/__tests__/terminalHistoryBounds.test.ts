import { describe, expect, it } from "vitest";

import {
  TERMINAL_HISTORY_MAX_BYTES,
  TERMINAL_HISTORY_MAX_LINES,
  boundAttachHistory,
} from "../terminalHistoryBounds";

const byteLength = (value: string) => new TextEncoder().encode(value).length;

describe("terminalHistoryBounds", () => {
  it("keeps the newest history within both ceilings", () => {
    // History that already fits is handed through untouched. Re-encoding it
    // would be the only way to corrupt an SGR run that arrived intact.
    const small = ["one", "two", "three"].join("\r\n");
    expect(boundAttachHistory(small)).toEqual({
      text: small,
      droppedLines: 0,
    });

    // Over the line ceiling the tail survives, because the newest output is
    // the output the user came back for.
    const many = Array.from(
      { length: TERMINAL_HISTORY_MAX_LINES + 500 },
      (_, index) => `line-${index + 1}`,
    ).join("\n");
    const lineBounded = boundAttachHistory(many);
    expect(lineBounded.droppedLines).toBe(500);
    expect(lineBounded.text.split("\n")).toHaveLength(
      TERMINAL_HISTORY_MAX_LINES,
    );
    expect(lineBounded.text.startsWith("line-501\n")).toBe(true);
    expect(
      lineBounded.text.endsWith(`line-${TERMINAL_HISTORY_MAX_LINES + 500}`),
    ).toBe(true);
  });

  it("trims by bytes even when the line count is legal", () => {
    // 400 lines is well under the line ceiling, but they are fat lines.
    const fat = Array.from({ length: 400 }, (_, index) =>
      `${index}`.padEnd(1024, "x"),
    ).join("\n");
    expect(byteLength(fat)).toBeGreaterThan(TERMINAL_HISTORY_MAX_BYTES);

    const bounded = boundAttachHistory(fat);
    expect(byteLength(bounded.text)).toBeLessThanOrEqual(
      TERMINAL_HISTORY_MAX_BYTES,
    );
    expect(bounded.droppedLines).toBeGreaterThan(0);
    // Still the tail: the last line is intact and is the newest one.
    expect(bounded.text.endsWith("399".padEnd(1024, "x"))).toBe(true);
  });

  it("trims a single oversized line rather than letting the ceiling lapse", () => {
    // No newline anywhere, so there is nothing to drop from the front. A
    // ceiling that gives up here is not a ceiling.
    const wall = "y".repeat(TERMINAL_HISTORY_MAX_BYTES + 4096);
    const bounded = boundAttachHistory(wall);

    expect(byteLength(bounded.text)).toBeLessThanOrEqual(
      TERMINAL_HISTORY_MAX_BYTES,
    );
    expect(bounded.text.endsWith("yyyy")).toBe(true);
    expect(bounded.droppedLines).toBe(0);
  });

  it("never splits a multi-byte character", () => {
    // Trimming by byte offset is the obvious implementation and the wrong one:
    // it can cut an emoji in half and hand the renderer a lone surrogate.
    const emoji = "\u{1F600}";
    const bounded = boundAttachHistory(emoji.repeat(64), 10, 32);

    expect(byteLength(bounded.text)).toBeLessThanOrEqual(32);
    expect(bounded.text).toBe(emoji.repeat(8));
    expect([...bounded.text].every((char) => char === emoji)).toBe(true);
  });

  it("passes empty history through", () => {
    expect(boundAttachHistory("")).toEqual({ text: "", droppedLines: 0 });
  });
});
