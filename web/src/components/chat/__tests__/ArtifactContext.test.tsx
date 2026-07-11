import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useArtifactContext } from "../artifacts/ArtifactContext";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

describe("useArtifactContext", () => {
  it("warns once, not per consumer, when no provider is mounted", () => {
    vi.stubEnv("NODE_ENV", "development");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});

    // A wiki page full of code blocks mounts dozens of provider-less
    // consumers; the missing-provider diagnostic must not flood the console.
    const { result } = renderHook(() => useArtifactContext());
    renderHook(() => useArtifactContext());
    renderHook(() => useArtifactContext());

    const providerWarnings = warn.mock.calls.filter(([message]) =>
      String(message).includes("no ArtifactContext provider"),
    );
    expect(providerWarnings.length).toBeLessThanOrEqual(1);
    expect(result.current.openCodeAsArtifact).toBeTypeOf("function");
    expect(result.current.openFileAsArtifact).toBeTypeOf("function");
  });
});
