import type { CanvasPanelState } from "../../components/canvas/hooks/useCanvasPanel";
import type { A2UISurfaceState } from "../../components/canvas/types";
import type { UseChatTransportParams } from "./transportTypes";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function asOptionalNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function toA2UISurfaceState(
  ev: Record<string, unknown>,
): A2UISurfaceState | null {
  if (
    typeof ev.canvas_id !== "string" ||
    typeof ev.conversation_id !== "string" ||
    typeof ev.mode !== "string" ||
    !isRecord(ev.surface) ||
    !isRecord(ev.data_model)
  ) {
    return null;
  }
  return {
    canvasId: ev.canvas_id,
    conversationId: ev.conversation_id,
    mode: ev.mode,
    surface: ev.surface as A2UISurfaceState["surface"],
    dataModel: ev.data_model as A2UISurfaceState["dataModel"],
    rootComponentId:
      typeof ev.root_component_id === "string" ? ev.root_component_id : null,
    completed: ev.completed === true,
  };
}

export function handleCanvasTransportEvent(
  data: Record<string, unknown>,
  ctx: UseChatTransportParams,
) {
  const ev = data;
  if (ev.event === "surface_update") {
    const surface = toA2UISurfaceState(ev);
    if (!surface) return;
    ctx.setCanvasSurfaces((prev: Map<string, A2UISurfaceState>) => {
      const next = new Map(prev);
      next.set(surface.canvasId, surface);
      return next;
    });
  } else if (
    ev.event === "interaction_confirmed" ||
    ev.event === "close_canvas"
  ) {
    if (typeof ev.canvas_id !== "string") return;
    const canvasId = ev.canvas_id;
    ctx.setCanvasSurfaces((prev: Map<string, A2UISurfaceState>) => {
      const next = new Map(prev);
      const s = next.get(canvasId);
      if (s) {
        next.set(canvasId, { ...s, completed: true });
      }
      return next;
    });
    if (ev.event === "close_canvas") {
      ctx.setCanvasPanel((prev) =>
        prev?.canvasId === canvasId ? null : prev,
      );
    }
  } else if (ev.event === "panel_present") {
    const url = typeof ev.url === "string" ? ev.url : ev.html_url;
    if (typeof ev.canvas_id !== "string" || typeof url !== "string") return;
    const canvasId = ev.canvas_id;
    ctx.setCanvasPanel((prev: CanvasPanelState | null) => ({
      ...prev,
      canvasId,
      title: typeof ev.title === "string" ? ev.title : undefined,
      url,
      width: asOptionalNumber(ev.width) || prev?.width,
      height: asOptionalNumber(ev.height) || prev?.height,
    }));
  } else if (ev.event === "canvas_rehydrate") {
    if (!Array.isArray(ev.surfaces)) return;
    const surfaces = ev.surfaces;
    ctx.setCanvasSurfaces((prev: Map<string, A2UISurfaceState>) => {
      const next = new Map(prev);
      for (const s of surfaces) {
        if (!isRecord(s)) continue;
        if (s.mode === "a2ui") {
          const surface = toA2UISurfaceState(s);
          if (surface) {
            next.set(surface.canvasId, surface);
          }
        } else if (s.mode === "html" && !s.completed) {
          const url = typeof s.url === "string" ? s.url : s.html_url;
          if (typeof s.canvas_id !== "string" || typeof url !== "string") {
            continue;
          }
          ctx.setCanvasPanel({
            canvasId: s.canvas_id,
            title: typeof s.title === "string" ? s.title : undefined,
            url,
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
    if (typeof data.artifact_type !== "string" || typeof data.content !== "string") {
      return;
    }
    ctx.onArtifactEventRef.current?.(
      data.artifact_type,
      data.content,
      typeof data.language === "string" ? data.language : undefined,
      typeof data.title === "string" ? data.title : undefined,
    );
  }
}
