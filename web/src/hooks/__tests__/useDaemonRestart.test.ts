import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { requestDaemonRestart } from "../../lib/api";
import { useDaemonRestart } from "../useDaemonRestart";

vi.mock("../../lib/api", () => ({
  requestDaemonRestart: vi.fn(),
}));

describe("useDaemonRestart", () => {
  beforeEach(() => {
    vi.mocked(requestDaemonRestart).mockReset();
  });

  it("uses Error.message directly for restart failures", async () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
    vi.mocked(requestDaemonRestart).mockRejectedValue(new Error(""));
    try {
      const { result } = renderHook(() => useDaemonRestart());

      await act(async () => {
        await result.current.restartDaemon();
      });

      expect(result.current.restartError).toBe("");
    } finally {
      consoleError.mockRestore();
    }
  });
});
