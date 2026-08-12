import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useAuth } from "./useAuth";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useAuth", () => {
  it("surfaces whether web credentials are configured", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve({
          ok: true,
          json: async () => ({
            auth_required: true,
            authenticated: false,
            credentials_configured: false,
          }),
        } as Response),
      ),
    );

    const { result } = renderHook(() => useAuth());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current).toMatchObject({
      authRequired: true,
      authenticated: false,
      credentialsConfigured: false,
    });
  });

  it.each([
    ["a non-OK response", () => Promise.resolve({ ok: false } as Response)],
    ["a network error", () => Promise.reject(new Error("offline"))],
  ])("fails closed after %s", async (_case, fetchResult) => {
    vi.stubGlobal("fetch", vi.fn(fetchResult));

    const { result } = renderHook(() => useAuth());

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current).toMatchObject({
      authRequired: true,
      authenticated: false,
      credentialsConfigured: true,
    });
  });
});
