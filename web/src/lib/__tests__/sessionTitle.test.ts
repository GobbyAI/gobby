import { describe, expect, it } from "vitest";

import {
  getSessionDisplayTitle,
  getActivitySessionTitle,
  stripSessionTitlePrefix,
} from "../sessionTitle";

describe("session title display", () => {
  it("drops the parenthesised provenance prefix from persisted titles", () => {
    expect(getSessionDisplayTitle({ title: "(gobby): S#9829" })).toBe("S#9829");
    expect(
      getSessionDisplayTitle({
        title: "gobby#11155: Task #42 - Implement structured handoffs",
      }),
    ).toBe("Task #42 - Implement structured handoffs");
    expect(stripSessionTitlePrefix("gobby#11155: Task #42 (second pass)")).toBe(
      "Task #42 (second pass)",
    );
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

it("activity labels suppress only provisional suffixes and retain task and manual titles", () => {
  expect(
    getActivitySessionTitle({
      ref: "gobby#12",
      title: "gobby#12: Codex",
      title_source: "provisional",
    }),
  ).toBe("gobby#12");
  expect(
    getActivitySessionTitle({
      ref: "gobby#12",
      title: "(gobby#12): Task #42 - Fix",
      title_source: "task",
    }),
  ).toBe("gobby#12: Task #42 - Fix");
  expect(
    getActivitySessionTitle({
      ref: "gobby#12",
      title: "Codex",
      title_source: "manual",
    }),
  ).toBe("gobby#12: Codex");
});
