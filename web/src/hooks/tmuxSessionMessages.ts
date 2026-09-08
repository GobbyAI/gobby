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
