import { afterEach, describe, expect, it, vi } from "vitest";

import { handleArtifactTransportEvent } from "../useChat/transportArtifactEvents";
import type { UseChatTransportParams } from "../useChat/transportTypes";

type ArtifactCall = [string, string, string | undefined, string | undefined];

afterEach(() => {
  vi.restoreAllMocks();
});

function makeContext() {
  const calls: ArtifactCall[] = [];
  const ctx = {
    onArtifactEventRef: {
      current: (
        artifactType: string,
        content: string,
        language?: string,
        title?: string,
      ) => {
        calls.push([artifactType, content, language, title]);
      },
    },
  } as unknown as UseChatTransportParams;
  return { ctx, calls };
}

describe("handleArtifactTransportEvent", () => {
  it("forwards a valid show_file event to the artifact callback", () => {
    const { ctx, calls } = makeContext();
    handleArtifactTransportEvent(
      {
        type: "artifact_event",
        event: "show_file",
        artifact_type: "code",
        content: "print('hi')",
        language: "python",
        title: "main.py",
      },
      ctx,
    );
    expect(calls).toEqual([["code", "print('hi')", "python", "main.py"]]);
  });

  it("passes undefined language and title when absent", () => {
    const { ctx, calls } = makeContext();
    handleArtifactTransportEvent(
      { event: "show_file", artifact_type: "image", content: "data:image/png;base64,AAAA" },
      ctx,
    );
    expect(calls).toEqual([["image", "data:image/png;base64,AAAA", undefined, undefined]]);
  });

  it("ignores events that are not show_file", () => {
    const { ctx, calls } = makeContext();
    const debug = vi.spyOn(console, "debug").mockImplementation(() => undefined);
    const data = { event: "other", artifact_type: "code", content: "x" };
    handleArtifactTransportEvent(data, ctx);
    expect(calls).toEqual([]);
    expect(debug).toHaveBeenCalledWith(
      "Ignoring artifact transport event",
      expect.objectContaining({
        data,
        event: "other",
        handlerRef: ctx.onArtifactEventRef,
        reason: "unsupported_event",
      }),
    );
  });

  it("ignores show_file events missing required fields", () => {
    const { ctx, calls } = makeContext();
    const debug = vi.spyOn(console, "debug").mockImplementation(() => undefined);
    const missingContent = { event: "show_file", artifact_type: "code" };
    const missingType = { event: "show_file", content: "x" };
    handleArtifactTransportEvent(missingContent, ctx);
    handleArtifactTransportEvent(missingType, ctx);
    expect(calls).toEqual([]);
    expect(debug).toHaveBeenNthCalledWith(
      1,
      "Ignoring artifact transport event",
      expect.objectContaining({
        data: missingContent,
        event: "show_file",
        handlerRef: ctx.onArtifactEventRef,
        reason: "invalid_show_file_payload",
      }),
    );
    expect(debug).toHaveBeenNthCalledWith(
      2,
      "Ignoring artifact transport event",
      expect.objectContaining({
        data: missingType,
        event: "show_file",
        handlerRef: ctx.onArtifactEventRef,
        reason: "invalid_show_file_payload",
      }),
    );
  });
});
