import { describe, expect, it } from "vitest";

import { handleArtifactTransportEvent } from "../useChat/transportArtifactEvents";
import type { UseChatTransportParams } from "../useChat/transportTypes";

type ArtifactCall = [string, string, string | undefined, string | undefined];

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
    handleArtifactTransportEvent({ event: "other", artifact_type: "code", content: "x" }, ctx);
    expect(calls).toEqual([]);
  });

  it("ignores show_file events missing required fields", () => {
    const { ctx, calls } = makeContext();
    handleArtifactTransportEvent({ event: "show_file", artifact_type: "code" }, ctx);
    handleArtifactTransportEvent({ event: "show_file", content: "x" }, ctx);
    expect(calls).toEqual([]);
  });
});
