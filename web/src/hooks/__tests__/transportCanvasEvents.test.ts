import { describe, expect, it } from "vitest";

import type { CanvasPanelState } from "../../components/canvas/hooks/useCanvasPanel";
import type { A2UISurfaceState } from "../../components/canvas/types";
import { handleCanvasTransportEvent } from "../useChat/transportCanvasEvents";
import type { UseChatTransportParams } from "../useChat/transportTypes";

function makeContext() {
  let surfaces = new Map<string, A2UISurfaceState>();
  let panel: CanvasPanelState | null = null;

  const setCanvasSurfaces: UseChatTransportParams["setCanvasSurfaces"] = (
    value,
  ) => {
    surfaces = typeof value === "function" ? value(surfaces) : value;
  };
  const setCanvasPanel: UseChatTransportParams["setCanvasPanel"] = (value) => {
    panel = typeof value === "function" ? value(panel) : value;
  };

  return {
    ctx: { setCanvasSurfaces, setCanvasPanel } as UseChatTransportParams,
    getPanel: () => panel,
    getSurfaces: () => surfaces,
  };
}

function validSurfaceEvent(overrides: Record<string, unknown> = {}) {
  return {
    type: "canvas_event",
    event: "surface_update",
    canvas_id: "canvas-1",
    conversation_id: "conv-1",
    mode: "a2ui",
    surface: {
      root: {
        type: "Text",
        text: { literalString: "Ready" },
      },
    },
    data_model: { count: 1 },
    root_component_id: "root",
    ...overrides,
  };
}

describe("handleCanvasTransportEvent", () => {
  it("hydrates valid A2UI surface payloads", () => {
    const { ctx, getSurfaces } = makeContext();

    handleCanvasTransportEvent(validSurfaceEvent(), ctx);

    expect(getSurfaces().get("canvas-1")).toMatchObject({
      canvasId: "canvas-1",
      conversationId: "conv-1",
      rootComponentId: "root",
      completed: false,
    });
  });

  it("rejects invalid A2UI surface components", () => {
    const { ctx, getSurfaces } = makeContext();

    handleCanvasTransportEvent(
      validSurfaceEvent({
        surface: {
          root: {
            label: { literalString: "Missing type" },
          },
        },
      }),
      ctx,
    );

    expect(getSurfaces().size).toBe(0);
  });

  it("rejects invalid A2UI data models", () => {
    const { ctx, getSurfaces } = makeContext();

    handleCanvasTransportEvent(
      validSurfaceEvent({
        data_model: { count: Number.NaN },
      }),
      ctx,
    );

    expect(getSurfaces().size).toBe(0);
  });

  it("rehydrates HTML panels unless completed is exactly true", () => {
    const { ctx, getPanel } = makeContext();

    handleCanvasTransportEvent(
      {
        type: "canvas_event",
        event: "canvas_rehydrate",
        surfaces: [
          {
            mode: "html",
            canvas_id: "skipped",
            url: "/skipped.html",
            completed: true,
          },
          {
            mode: "html",
            canvas_id: "rehydrated",
            url: "/rehydrated.html",
            title: "Panel",
            completed: "false",
          },
        ],
      },
      ctx,
    );

    expect(getPanel()).toEqual({
      canvasId: "rehydrated",
      title: "Panel",
      url: "/rehydrated.html",
    });
  });
});
