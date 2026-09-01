import { describe, expect, it } from "vitest";

import {
  isNoCheckoutErrorBody,
  isNoCheckoutErrorText,
  responseReportsNoCheckout,
} from "../projectCheckout";

const notFoundBody = {
  detail: {
    error: "CheckoutNotFoundError",
    message: "no checkout for machine m-1 project p-1",
  },
};

describe("projectCheckout detection", () => {
  it("recognises the structured 409 body the files API sends", () => {
    expect(isNoCheckoutErrorBody(notFoundBody)).toBe(true);
  });

  it("recognises a plain-string detail and rejects unrelated errors", () => {
    expect(
      isNoCheckoutErrorBody({ detail: "No checkout for this project" }),
    ).toBe(true);
    expect(
      isNoCheckoutErrorBody({
        detail: { error: "CheckoutRootTakenError", message: "root taken" },
      }),
    ).toBe(false);
    expect(isNoCheckoutErrorBody({ detail: "dirty worktree" })).toBe(false);
    expect(isNoCheckoutErrorBody(null)).toBe(false);
    expect(isNoCheckoutErrorBody("no checkout")).toBe(false);
  });

  it("parses raw text bodies as JSON when possible", () => {
    expect(isNoCheckoutErrorText(JSON.stringify(notFoundBody))).toBe(true);
    expect(isNoCheckoutErrorText("no checkout for machine m-1")).toBe(true);
    expect(isNoCheckoutErrorText("")).toBe(false);
    expect(isNoCheckoutErrorText("{not json")).toBe(false);
    expect(
      isNoCheckoutErrorText(JSON.stringify({ detail: "dirty worktree" })),
    ).toBe(false);
  });

  it("reads a failed Response body and answers from it", async () => {
    await expect(
      responseReportsNoCheckout(
        new Response(JSON.stringify(notFoundBody), { status: 409 }),
      ),
    ).resolves.toBe(true);
    await expect(
      responseReportsNoCheckout(new Response("unavailable", { status: 500 })),
    ).resolves.toBe(false);
  });
});
