import type { UseChatTransportParams } from "./transportTypes";

/**
 * Handle `artifact_event` transport frames. The only artifact event today is
 * `show_file` (emitted by the `gobby-artifacts:show_file` MCP tool), which
 * pushes file contents into the web chat artifacts panel.
 */
export function handleArtifactTransportEvent(
  data: Record<string, unknown>,
  ctx: UseChatTransportParams,
): void {
  if (data.event !== "show_file") {
    console.debug("Ignoring artifact transport event", {
      data,
      event: data.event,
      handlerRef: ctx.onArtifactEventRef,
      reason: "unsupported_event",
    });
    return;
  }

  if (typeof data.artifact_type !== "string" || typeof data.content !== "string") {
    console.debug("Ignoring artifact transport event", {
      data,
      event: data.event,
      handlerRef: ctx.onArtifactEventRef,
      reason: "invalid_show_file_payload",
    });
    return;
  }

  ctx.onArtifactEventRef.current?.(
    data.artifact_type,
    data.content,
    typeof data.language === "string" ? data.language : undefined,
    typeof data.title === "string" ? data.title : undefined,
  );
}
