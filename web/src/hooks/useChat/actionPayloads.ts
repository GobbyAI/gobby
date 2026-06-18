import type { ContentBlock, QueuedFile } from "../../types/chat";

function hasUploadedAttachment(
  file: QueuedFile,
): file is QueuedFile & { attachment: NonNullable<QueuedFile["attachment"]> } {
  return file.attachment !== null;
}

export function attachmentPayload(files?: QueuedFile[]): Array<{ id: string }> {
  return (files ?? [])
    .filter(hasUploadedAttachment)
    .map((qf) => ({ id: qf.attachment.id }));
}

export function userContentBlocks(
  content: string,
  files?: QueuedFile[],
): ContentBlock[] | undefined {
  const blocks: ContentBlock[] = [];
  if (content.trim()) blocks.push({ type: "text", content });
  for (const qf of files ?? []) {
    if (qf.attachment) {
      blocks.push({ type: "attachment", attachment: qf.attachment });
    }
  }
  return blocks.length > 0 ? blocks : undefined;
}
