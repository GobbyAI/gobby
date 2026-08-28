import { describe, expect, it } from "vitest";

import { getSessionDisplayTitle } from "../sessionTitle";

describe("session title display", () => {
  it("renders persisted deterministic titles verbatim", () => {
    expect(getSessionDisplayTitle({ title: "(gobby): S#9829" })).toBe(
      "(gobby): S#9829",
    );
    expect(
      getSessionDisplayTitle({
        title: "(gobby): Task #42 - Implement structured handoffs",
      }),
    ).toBe("(gobby): Task #42 - Implement structured handoffs");
  });

  it("renders manual titles verbatim", () => {
    expect(getSessionDisplayTitle({ title: "My session" })).toBe("My session");
  });

  it("uses the empty-state title when persisted title is blank", () => {
    expect(getSessionDisplayTitle({ title: "  " })).toBe("New Session");
  });
});
