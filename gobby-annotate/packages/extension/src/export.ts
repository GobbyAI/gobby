import { encodeBundle, MAX_BYTES, type Bundle } from "@gobby/annotate-core";
import type { Batch, Drafts } from "./drafts";
import { base64 } from "./screenshots";

export async function exportSnapshot(
  bundle: Bundle,
  deliver: (bytes: Uint8Array, name: string) => Promise<string>,
): Promise<string> {
  const bytes = encodeBundle(bundle);
  if (bytes.length > MAX_BYTES)
    throw new Error(
      "Export exceeds 128 MiB; delete annotations or start another batch",
    );
  return deliver(bytes, `gobby-annotate-${bundle.manifest.exportId}.zip`);
}
async function sendNative(message: object, failure: string) {
  const result: unknown = await chrome.runtime.sendNativeMessage(
    "ai.gobby.annotate",
    message,
  );
  if (
    !result ||
    typeof result !== "object" ||
    !("ok" in result) ||
    result.ok !== true
  )
    throw new Error(failure);
}
/** Removes staged ZIPs from the iPhone App Group; an already-missing ZIP counts as removed. */
export async function unstageSafari(exportIds: string[]): Promise<void> {
  for (const transferId of exportIds)
    await sendNative(
      { action: "delete", transferId },
      "Safari could not delete the staged export. Open the Gobby Annotate app and retry.",
    );
}
/**
 * Deletes batches from both stores. Staged ZIPs go first, so a native failure
 * leaves the batch listed for a retry instead of orphaning its App Group copy.
 * `when` is rechecked against the stored batch, so a batch edited since the
 * caller listed it is skipped.
 */
export async function deleteBatches(
  drafts: Drafts,
  batchIds: string[],
  {
    when = () => true,
    unstage = unstageSafari,
  }: {
    when?: (batch: Batch) => boolean;
    unstage?: (exportIds: string[]) => Promise<void>;
  } = {},
): Promise<number> {
  let removed = 0;
  for (const batchId of batchIds) {
    const batch = await drafts.get(batchId);
    if (!batch || !when(batch)) continue;
    const staged = (batch.exports ?? []).filter((e) => e.staged);
    if (staged.length) await unstage(staged.map((e) => e.id));
    await drafts.remove(batchId);
    removed++;
  }
  return removed;
}
export async function stageSafari(
  bytes: Uint8Array,
  name: string,
): Promise<string> {
  const transferId = crypto.randomUUID(),
    chunkSize = 192 * 1024;
  const send = (message: object) =>
    sendNative(
      message,
      "Safari could not stage export. Open the Gobby Annotate app and retry.",
    );
  await send({ action: "begin", transferId, name, size: bytes.length });
  try {
    for (let offset = 0; offset < bytes.length; offset += chunkSize)
      await send({
        action: "chunk",
        transferId,
        offset,
        data: base64(bytes.subarray(offset, offset + chunkSize)),
      });
    await send({ action: "finish", transferId });
    return "Staged in Gobby Annotate. Open the app to Save or Share to your coding computer.";
  } catch (error) {
    await send({ action: "cancel", transferId }).catch(() => {});
    throw error;
  }
}
