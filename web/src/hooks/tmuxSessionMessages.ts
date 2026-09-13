/**
 * Every outbound terminal frame the web client sends, built in one place so a
 * wire shape is read off a single definition rather than off a JSON literal
 * buried in a callback. Shapes are pinned by `tests/fixtures/terminal_ws_golden/`.
 */

export function terminalListMessage(
  requestId: string,
  projectId: string | null,
  cursor?: string,
): string {
  // The daemon lists the whole machine unless a project is named; naming the
  // picker's project keeps other projects' terminals out of the list while
  // still including terminals that belong to no project.
  const request: Record<string, unknown> = {
    type: "terminal_list",
    request_id: requestId,
  };
  if (projectId !== null) request.project_id = projectId;
  if (cursor !== undefined) request.cursor = cursor;
  return JSON.stringify(request);
}

export function terminalAttachMessage(
  requestId: string,
  terminalId: string,
): string {
  return JSON.stringify({
    type: "terminal_attach",
    request_id: requestId,
    terminal_id: terminalId,
    frame_delivery: "proxy",
    viewer: "web",
  });
}

export function terminalDetachMessage(
  requestId: string,
  terminalId: string | undefined,
  attachmentId: string,
): string {
  return JSON.stringify({
    type: "terminal_detach",
    request_id: requestId,
    terminal_id: terminalId,
    attachment_id: attachmentId,
  });
}

export function terminalResizeMessage(
  terminalId: string | undefined,
  attachmentId: string,
  rows: number,
  cols: number,
): string {
  return JSON.stringify({
    type: "terminal_resize",
    terminal_id: terminalId,
    attachment_id: attachmentId,
    rows,
    cols,
  });
}

export function terminalCreateMessage(
  requestId: string,
  projectId: string | null,
  cwd: string | undefined,
  command: string | undefined,
): string {
  return JSON.stringify({
    type: "terminal_create",
    request_id: requestId,
    ...(projectId !== null ? { project_id: projectId } : {}),
    rows: 24,
    cols: 80,
    cwd,
    command: command ? [command] : ["zsh"],
  });
}

export function terminalKillMessage(
  requestId: string,
  terminalId: string,
): string {
  return JSON.stringify({
    type: "terminal_kill",
    request_id: requestId,
    terminal_id: terminalId,
  });
}

/**
 * The daemon keys this frame on `attachment_id` and returns early without one,
 * so a viewport can only be set for a live attachment whose grid has been
 * measured. `kind` separates the two reasons to send it: the first viewport an
 * attachment is told, and a client-driven redraw after the stream was
 * abandoned over budget.
 */
export function terminalSetViewportMessage(
  requestId: string,
  terminalId: string,
  attachmentId: string,
  rows: number,
  cols: number,
  kind: "viewport" | "refresh" = "viewport",
): string {
  return JSON.stringify({
    type: "terminal_set_viewport",
    request_id: requestId,
    terminal_id: terminalId,
    attachment_id: attachmentId,
    rows,
    cols,
    kind,
  });
}

export function terminalTakeControlMessage(
  terminalId: string,
  attachmentId: string,
  takeover: boolean,
): string {
  return JSON.stringify({
    type: "terminal_take_control",
    terminal_id: terminalId,
    attachment_id: attachmentId,
    takeover,
  });
}

export function terminalReleaseControlMessage(
  terminalId: string,
  attachmentId: string,
): string {
  return JSON.stringify({
    type: "terminal_release_control",
    terminal_id: terminalId,
    attachment_id: attachmentId,
  });
}

/**
 * Input and paste differ only in the verb and the payload field, and a retry
 * has to rebuild the identical frame, so both go through one builder.
 */
export function terminalWriteMessage(
  kind: "input" | "paste",
  terminalId: string,
  attachmentId: string,
  clientWriteSeq: number,
  payload: string,
): string {
  return JSON.stringify(
    kind === "input"
      ? {
          type: "terminal_input",
          terminal_id: terminalId,
          attachment_id: attachmentId,
          client_write_seq: clientWriteSeq,
          data: payload,
        }
      : {
          type: "terminal_paste",
          terminal_id: terminalId,
          attachment_id: attachmentId,
          client_write_seq: clientWriteSeq,
          text: payload,
        },
  );
}
