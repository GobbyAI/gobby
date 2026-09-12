import { encodeBundle, MAX_BYTES, type Bundle } from "@gobby/annotate-core";
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
export async function stageSafari(
  bytes: Uint8Array,
  name: string,
): Promise<string> {
  const transferId = crypto.randomUUID(),
    chunkSize = 192 * 1024;
  const send = async (message: object) => {
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
      throw new Error(
        "Safari could not stage export. Open the Gobby Annotate app and retry.",
      );
  };
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
