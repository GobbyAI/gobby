import { describe, expect, it } from "vitest";

import { getSessionDisplayTitle } from "../sessionTitle";

describe("session title display", () => {
  it("does not duplicate a provisional title that already owns its session ref", () => {
    const session = { seq_num: 9829, title: "#9829 Codex" };

    expect(getSessionDisplayTitle(session)).toBe("#9829: Codex");
  });

  it("adds the session ref to a synthesized title", () => {
    const session = { seq_num: 202, title: "Paused Terminal" };

    expect(getSessionDisplayTitle(session)).toBe("#202: Paused Terminal");
  });

  it("recognizes an existing colon-delimited ref", () => {
    expect(
      getSessionDisplayTitle({
        ref: "#44",
        title: "#44: Manually named session",
      }),
    ).toBe("#44: Manually named session");
  });

  it("keeps the canonical row shape when the stored title is only the ref", () => {
    expect(getSessionDisplayTitle({ seq_num: 44, title: "#44" })).toBe(
      "#44: New Session",
    );
  });
});
