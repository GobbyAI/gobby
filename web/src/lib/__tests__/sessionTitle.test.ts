import { describe, expect, it } from "vitest";

import {
  getSessionDisplayTitle,
  stripSessionTitlePrefix,
} from "../sessionTitle";

describe("session title display", () => {
  it("drops the parenthesised provenance prefix from persisted titles", () => {
    expect(getSessionDisplayTitle({ title: "(gobby): S#9829" })).toBe("S#9829");
    expect(
      getSessionDisplayTitle({
        title: "(gobby-S#11155): Task #42 - Implement structured handoffs",
      }),
    ).toBe("Task #42 - Implement structured handoffs");
    expect(
      stripSessionTitlePrefix("(gobby-S#11155): Task #42 (second pass)"),
    ).toBe("Task #42 (second pass)");
  });

  it("renders manual titles verbatim", () => {
    expect(getSessionDisplayTitle({ title: "My session" })).toBe("My session");
    expect(stripSessionTitlePrefix("My session")).toBe("My session");
  });

  it("uses the empty-state title when persisted title is blank", () => {
    expect(getSessionDisplayTitle({ title: "  " })).toBe("New Session");
    expect(getSessionDisplayTitle({ title: "(gobby): " })).toBe("New Session");
    expect(stripSessionTitlePrefix(null)).toBe("");
  });
});
