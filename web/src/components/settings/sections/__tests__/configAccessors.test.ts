import { describe, expect, it } from "vitest";

import { encodeDynamicMapRows } from "../configAccessors";

describe("encodeDynamicMapRows", () => {
  it("rejects a new row whose encoded key collides", () => {
    expect(() =>
      encodeDynamicMapRows([
        { storedKey: "alpha", displayKey: "alpha", value: 1 },
        { storedKey: "", displayKey: "alpha", value: 2 },
      ]),
    ).toThrow("Dynamic map rows collide at stored key alpha");
  });

  it("rejects a renamed row whose encoded key collides", () => {
    expect(() =>
      encodeDynamicMapRows([
        { storedKey: "alpha", displayKey: "alpha", value: 1 },
        { storedKey: "beta", displayKey: "alpha", value: 2 },
      ]),
    ).toThrow("Dynamic map rows collide at stored key alpha");
  });

  it("preserves distinct literal and encoded stored keys", () => {
    expect(
      encodeDynamicMapRows([
        { storedKey: "a.b", displayKey: "a.b", value: "literal" },
        { storedKey: "a%2Eb", displayKey: "a.b", value: "encoded" },
      ]),
    ).toEqual({ "a.b": "literal", "a%2Eb": "encoded" });
  });
});
