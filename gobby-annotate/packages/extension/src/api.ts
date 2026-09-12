export async function rpc<T>(message: object): Promise<T> {
  const result: { ok: boolean; value: T; error: string } =
    await chrome.runtime.sendMessage(message);
  if (!result?.ok)
    throw new Error(
      result?.error ?? "Extension unavailable. Reactivate it and retry.",
    );
  return result.value;
}
