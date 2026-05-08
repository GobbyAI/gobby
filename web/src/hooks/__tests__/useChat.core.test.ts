import { describe, expect, it } from "vitest";

import { isChatProvider } from "../useChat/core";

describe("useChat core provider helpers", () => {
  it("accepts Droid as a chat provider", () => {
    expect(isChatProvider("droid")).toBe(true);
    expect(isChatProvider("unknown")).toBe(false);
  });
});
