import type { CanvasPanelState } from "../../components/canvas/hooks/useCanvasPanel";
import type { A2UISurfaceState } from "../../components/canvas/types";
import type { UseChatTransportParams } from "./transportTypes";

function toA2UISurfaceState(ev: Record<string, unknown>): A2UISurfaceState {
  return {
    canvasId: ev.canvas_id as string,
    conversationId: ev.conversation_id as string,
    mode: ev.mode as string,
    surface: ev.surface as A2UISurfaceState["surface"],
    dataModel: ev.data_model as A2UISurfaceState["dataModel"],
    rootComponentId: ev.root_component_id as string | null,
    completed: ev.completed as boolean,
  };
}

export function handleCanvasTransportEvent(
  data: Record<string, unknown>,
  ctx: UseChatTransportParams,
) {
  const ev = data;
  if (ev.event === "surface_update") {
    ctx.setCanvasSurfaces((prev: Map<string, A2UISurfaceState>) => {
      const next = new Map(prev);
      next.set(ev.canvas_id as string, toA2UISurfaceState(ev));
      return next;
    });
  } else if (
    ev.event === "interaction_confirmed" ||
    ev.event === "close_canvas"
  ) {
    ctx.setCanvasSurfaces((prev: Map<string, A2UISurfaceState>) => {
      const next = new Map(prev);
      const s = next.get(ev.canvas_id as string);
      if (s) {
        next.set(ev.canvas_id as string, { ...s, completed: true });
      }
      return next;
    });
    if (ev.event === "close_canvas") {
      ctx.setCanvasPanel((prev) =>
        prev?.canvasId === ev.canvas_id ? null : prev,
      );
    }
  } else if (ev.event === "panel_present") {
    ctx.setCanvasPanel((prev: CanvasPanelState | null) => ({
      ...prev,
      canvasId: ev.canvas_id as string,
      title: ev.title as string | undefined,
      url: ev.html_url as string,
      width: (ev.width as number | undefined) || prev?.width,
      height: (ev.height as number | undefined) || prev?.height,
    }));
  } else if (ev.event === "canvas_rehydrate") {
    ctx.setCanvasSurfaces((prev: Map<string, A2UISurfaceState>) => {
      const next = new Map(prev);
      for (const s of (ev.surfaces as Record<string, unknown>[] | undefined) || []) {
        if (s.mode === "a2ui") {
          next.set(s.canvas_id as string, toA2UISurfaceState(s));
        } else if (s.mode === "html" && !s.completed) {
          ctx.setCanvasPanel({
            canvasId: s.canvas_id as string,
            title: s.title as string | undefined,
            url: s.html_url as string,
          });
        }
      }
      return next;
    });
  }
}

export function handleArtifactTransportEvent(
  data: Record<string, unknown>,
  ctx: UseChatTransportParams,
) {
  if (data.event === "show_file") {
    ctx.onArtifactEventRef.current?.(
      data.artifact_type as string,
      data.content as string,
      data.language as string | undefined,
      data.title as string | undefined,
    );
  }
}
