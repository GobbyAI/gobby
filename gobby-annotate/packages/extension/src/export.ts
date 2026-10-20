import { encodeBundle, MAX_BYTES, type Bundle } from "@gobby/annotate-core";
import { isExported, type Batch, type Drafts } from "./drafts";
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
async function nativeRequest(
  message: object,
  failure: string,
): Promise<Record<string, unknown>> {
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
  return result as Record<string, unknown>;
}
async function sendNative(message: object, failure: string) {
  await nativeRequest(message, failure);
}
/**
 * Removes staged ZIPs from the iPhone App Group; an already-missing ZIP counts as removed. The
 * app's deletion record for a ZIP stays until acknowledgeSafari.
 */
export async function unstageSafari(exportIds: string[]): Promise<void> {
  for (const transferId of exportIds)
    await sendNative(
      { action: "delete", transferId },
      "Safari could not delete the staged export. Open the Gobby Annotate app and retry.",
    );
}
/** Clears the app's deletion records. Call only after their batches are removed or kept. */
async function acknowledgeSafari(exportIds: string[]): Promise<void> {
  for (const transferId of exportIds)
    await sendNative(
      { action: "acknowledge", transferId },
      "Safari could not record the app's deletion. It retries the next time Gobby Annotate lists batches.",
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
/**
 * Applies deletions the app made to App Group ZIPs (#23729 scope A). An exported batch whose
 * staged ZIP the app deleted goes with its other staged ZIPs; a draft edited since its export
 * keeps its notes. Every reported ZIP is then unstaged, including one held by an edited draft or
 * by no batch, and only after that is its record acknowledged. Any earlier failure leaves the
 * record for the next list.
 */
export async function applyAppDeletions(drafts: Drafts): Promise<void> {
  const reply = await nativeRequest(
    { action: "deleted" },
    "Safari could not read deletions from the Gobby Annotate app. Open the app and retry.",
  );
  const deleted = Array.isArray(reply.deleted)
    ? reply.deleted.filter((id): id is string => typeof id === "string")
    : [];
  if (!deleted.length) return;
  const exported = (await drafts.list())
    .filter(
      (batch) =>
        isExported(batch) &&
        (batch.exports ?? []).some((e) => e.staged && deleted.includes(e.id)),
    )
    .map((batch) => batch.id);
  await deleteBatches(drafts, exported, { when: isExported });
  await unstageSafari(deleted);
  await acknowledgeSafari(deleted);
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
